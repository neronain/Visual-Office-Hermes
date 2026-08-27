"""visual_office — seat every Hermes session at a desk, and pin every desk to a model.

Two jobs, deliberately kept separate:

**Watching.** Lifecycle hooks are forwarded to the office server so each session
and subagent shows up as a character. Unlike a plain activity feed, every event
carries the model dimension: ``post_api_request`` hands us ``model``,
``provider``, ``base_url`` and ``usage``, so the office can say *which* model a
character is burning and how much.

**Assigning.** ``office_delegate`` hands a task to a *named desk*. Each desk is
bound to a model in the roster, and the child agent is launched pinned to that
model through Hermes' public subagent lifecycle API — not the global
``delegation.model`` that would give every child the same brain.

Nothing here can change what Hermes does. Every hook returns ``None``, every
network call is fire-and-forget, and a stopped office server is invisible to the
agent.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional

from .desks import Roster, load_roster
from .sink import Sink

logger = logging.getLogger(__name__)

PLUGIN_ID = "visual_office"
DEFAULT_WAIT_SECONDS = 600.0

_SINK = Sink()
_ROSTER: Optional[Roster] = None
_ROSTER_LOCK = threading.Lock()
_HANDLES: dict[str, Any] = {}
_HANDLES_LOCK = threading.Lock()
_CTX: Any = None


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


def _roster(refresh: bool = False) -> Roster:
    global _ROSTER
    with _ROSTER_LOCK:
        if _ROSTER is None or refresh:
            _ROSTER = load_roster()
            for problem in _ROSTER.problems:
                logger.warning("visual_office roster: %s", problem)
        return _ROSTER


def _announce_roster() -> None:
    """Tell the office which desks exist. The server owns no roster of its own."""
    roster = _roster()
    _SINK.emit({"event": "roster", "at": time.time(), "roster": roster.to_dict()})


# ---------------------------------------------------------------------------
# Event relay
# ---------------------------------------------------------------------------


def _emit(event: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"event": event, "at": time.time()}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    _SINK.emit(payload)


def on_session_start(**kw: Any) -> None:
    _announce_roster()
    _emit(
        "session_start",
        session_id=kw.get("session_id"),
        platform=kw.get("platform"),
    )


def on_session_end(**kw: Any) -> None:
    _emit(
        "session_end",
        session_id=kw.get("session_id"),
        model=kw.get("model"),
        platform=kw.get("platform"),
        completed=kw.get("completed"),
        failed=kw.get("failed"),
        interrupted=kw.get("interrupted"),
    )


def on_session_finalize(**kw: Any) -> None:
    _emit("session_finalize", session_id=kw.get("session_id"))


def on_session_reset(**kw: Any) -> None:
    _emit("session_reset", session_id=kw.get("session_id"))


def on_pre_llm_call(**kw: Any) -> None:
    # Directive hook — MUST return None so nothing is injected or blocked.
    _emit(
        "thinking",
        session_id=kw.get("session_id"),
        model=kw.get("model"),
        platform=kw.get("platform"),
    )


def on_post_llm_call(**kw: Any) -> None:
    _emit(
        "thinking_done",
        session_id=kw.get("session_id"),
        model=kw.get("model"),
        platform=kw.get("platform"),
    )


def on_pre_tool_call(**kw: Any) -> None:
    # Directive hook — MUST return None so the tool is never blocked.
    _emit(
        "tool_start",
        session_id=kw.get("session_id"),
        tool_name=kw.get("tool_name"),
        tool_call_id=kw.get("tool_call_id"),
    )


def on_post_tool_call(**kw: Any) -> None:
    _emit(
        "tool_end",
        session_id=kw.get("session_id"),
        tool_name=kw.get("tool_name"),
        tool_call_id=kw.get("tool_call_id"),
        status=kw.get("status"),
    )


def on_post_api_request(**kw: Any) -> None:
    """The model dimension. This is the hook the whole project exists for."""
    usage = kw.get("usage")
    _emit(
        "api_request",
        session_id=kw.get("session_id"),
        task_id=kw.get("task_id"),
        platform=kw.get("platform"),
        model=kw.get("model"),
        provider=kw.get("provider"),
        base_url=kw.get("base_url"),
        api_mode=kw.get("api_mode"),
        response_model=kw.get("response_model"),
        api_duration=kw.get("api_duration"),
        finish_reason=kw.get("finish_reason"),
        tool_calls=kw.get("assistant_tool_call_count"),
        usage=usage if isinstance(usage, dict) else None,
    )


def on_pre_approval_request(**kw: Any) -> None:
    # Approval hooks carry session_key, not session_id.
    _emit("approval_wait", session_id=kw.get("session_key") or kw.get("session_id"))


def on_post_approval_response(**kw: Any) -> None:
    _emit("approval_done", session_id=kw.get("session_key") or kw.get("session_id"))


def on_subagent_start(**kw: Any) -> None:
    _emit(
        "subagent_start",
        session_id=kw.get("child_session_id"),
        parent_session_id=kw.get("parent_session_id"),
        subagent_id=kw.get("child_subagent_id"),
        role=kw.get("child_role"),
        goal=kw.get("child_goal"),
    )


def on_subagent_stop(**kw: Any) -> None:
    _emit(
        "subagent_stop",
        session_id=kw.get("child_session_id"),
        parent_session_id=kw.get("parent_session_id"),
        status=kw.get("child_status"),
        summary=kw.get("child_summary"),
        duration_ms=kw.get("duration_ms"),
    )


# ---------------------------------------------------------------------------
# office_delegate
# ---------------------------------------------------------------------------


def _wait_seconds() -> float:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        entry = (((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {})
        return float(entry.get("wait_seconds") or DEFAULT_WAIT_SECONDS)
    except Exception:
        return DEFAULT_WAIT_SECONDS


def _fail(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra}, ensure_ascii=False)


def _handle_spawn(params: dict) -> str:
    roster = _roster()
    desk_id = str(params.get("desk") or "").strip()
    goal = str(params.get("goal") or "").strip()

    if not goal:
        return _fail("office_delegate needs a goal.")
    if not roster.desks:
        return _fail(
            f"No desks are configured. Create a roster at {roster.source} "
            "with at least one desk (id, label, model).",
            problems=list(roster.problems),
        )

    desk = roster.find(desk_id) if desk_id else None
    if desk is None:
        return _fail(
            f"Unknown desk {desk_id!r}." if desk_id else "office_delegate needs a desk.",
            available=[{"id": d.id, "label": d.label, "model": d.model} for d in roster.desks],
        )

    if _CTX is None:
        return _fail("visual_office was not registered with a plugin context.")

    try:
        from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleError
    except ImportError as exc:
        return _fail(f"This Hermes build has no public subagent lifecycle API: {exc}")

    role = str(params.get("role") or desk.role or "leaf").strip().lower()
    if role not in ("leaf", "orchestrator"):
        role = "leaf"

    request = SubagentLaunchRequest(
        goal=goal,
        context=str(params.get("context") or "") or None,
        role=role,
        model=desk.model,
        allowed_toolsets=tuple(desk.toolsets) or None,
        metadata={"desk": desk.id, "desk_label": desk.label, "origin": desk.origin},
    )

    try:
        handle = _CTX.subagent_lifecycle.launch(request)
    except SubagentLifecycleError as exc:
        return _fail(f"Could not seat a worker at desk {desk.id!r}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        return _fail(f"Unexpected failure launching desk {desk.id!r}: {exc}")

    with _HANDLES_LOCK:
        _HANDLES[handle.subagent_id] = handle

    _emit(
        "desk_assign",
        subagent_id=handle.subagent_id,
        parent_session_id=handle.parent_session_id,
        desk=desk.id,
        desk_label=desk.label,
        origin=desk.origin,
        model=handle.model or desk.model,
        provider=handle.provider or desk.provider,
        goal=goal,
    )

    wait = params.get("wait")
    wait = True if wait is None else bool(wait)
    if not wait:
        return json.dumps(
            {
                "success": True,
                "subagent_id": handle.subagent_id,
                "desk": desk.id,
                "model": handle.model or desk.model,
                "state": "RUNNING",
                "note": "Running in the background. Poll with action='status'.",
            },
            ensure_ascii=False,
        )

    timeout = float(params.get("timeout_seconds") or _wait_seconds())
    terminal = _CTX.subagent_lifecycle.wait(handle, timeout_seconds=timeout)
    if not terminal.completed:
        return json.dumps(
            {
                "success": True,
                "subagent_id": handle.subagent_id,
                "desk": desk.id,
                "model": handle.model or desk.model,
                "state": str(terminal.state.value),
                "timed_out": bool(terminal.timed_out),
                "note": f"Still working after {timeout:.0f}s. Poll with action='status'.",
            },
            ensure_ascii=False,
        )

    result = _CTX.subagent_lifecycle.result(handle)
    return json.dumps(
        {
            "success": result.terminal_state.value == "SUCCEEDED",
            "subagent_id": handle.subagent_id,
            "desk": desk.id,
            "desk_label": desk.label,
            "model": handle.model or desk.model,
            "state": result.terminal_state.value,
            "summary": result.summary,
            "error": result.error_message,
            "usage": dict(result.usage_metadata or {}),
            "duration_seconds": round(
                (result.completed_at or 0) - (result.started_at or 0), 2
            )
            if result.started_at and result.completed_at
            else None,
        },
        ensure_ascii=False,
    )


def _handle_status(params: dict) -> str:
    subagent_id = str(params.get("subagent_id") or "").strip()
    if not subagent_id:
        return _fail("action='status' needs a subagent_id.")
    with _HANDLES_LOCK:
        handle = _HANDLES.get(subagent_id)
    if handle is None:
        return _fail(f"Unknown subagent_id {subagent_id!r}.")

    status = _CTX.subagent_lifecycle.status(handle)
    payload: dict[str, Any] = {
        "success": True,
        "subagent_id": subagent_id,
        "state": status.state.value,
        "model": handle.model,
    }
    if status.state.value in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
        result = _CTX.subagent_lifecycle.result(handle)
        payload["summary"] = result.summary
        payload["error"] = result.error_message
    return json.dumps(payload, ensure_ascii=False)


def _handle_list(_params: dict) -> str:
    roster = _roster()
    with _HANDLES_LOCK:
        handles = list(_HANDLES.values())
    running = []
    for handle in handles:
        try:
            state = _CTX.subagent_lifecycle.status(handle).state.value
        except Exception:
            state = "UNKNOWN"
        running.append(
            {"subagent_id": handle.subagent_id, "model": handle.model, "state": state}
        )
    return json.dumps(
        {
            "success": True,
            "office": roster.office_name,
            "desks": [d.to_dict() for d in roster.desks],
            "workers": running,
            "problems": list(roster.problems),
        },
        ensure_ascii=False,
    )


def _handle_cancel(params: dict) -> str:
    subagent_id = str(params.get("subagent_id") or "").strip()
    if not subagent_id:
        return _fail("action='cancel' needs a subagent_id.")
    with _HANDLES_LOCK:
        handle = _HANDLES.get(subagent_id)
    if handle is None:
        return _fail(f"Unknown subagent_id {subagent_id!r}.")
    reason = str(params.get("message") or "Cancelled from office_delegate.")
    outcome = _CTX.subagent_lifecycle.cancel(handle, reason=reason)
    return json.dumps(
        {
            "success": bool(outcome.accepted),
            "subagent_id": subagent_id,
            "state": outcome.state.value,
            "already_terminal": bool(outcome.already_terminal),
        },
        ensure_ascii=False,
    )


def handle_office_delegate(params: dict, **_kwargs: Any) -> str:
    action = str((params or {}).get("action") or "spawn").strip().lower()
    try:
        if action == "spawn":
            return _handle_spawn(params or {})
        if action == "status":
            return _handle_status(params or {})
        if action == "list":
            return _handle_list(params or {})
        if action == "cancel":
            return _handle_cancel(params or {})
        return _fail(f"Unknown action {action!r}. Use spawn, status, list or cancel.")
    except Exception as exc:  # pragma: no cover - never crash the agent turn
        logger.debug("office_delegate failed", exc_info=True)
        return _fail(f"office_delegate failed: {exc}")


def _build_schema(roster: Roster) -> dict:
    if roster.desks:
        lines = "\n".join(
            f"  - {d.id}: {d.label} — model {d.model}"
            + (f" ({d.origin})" if d.origin != "unknown" else "")
            + (f" · {d.note}" if d.note else "")
            for d in roster.desks
        )
        desk_help = f"Which desk does the work. Desks in this office:\n{lines}"
        desk_property: dict[str, Any] = {
            "type": "string",
            "enum": [d.id for d in roster.desks],
            "description": desk_help,
        }
    else:
        desk_property = {
            "type": "string",
            "description": (
                "Which desk does the work. This office has no desks configured yet — "
                f"create a roster at {roster.source}."
            ),
        }

    return {
        "name": "office_delegate",
        "description": (
            "Hand a task to a named desk in the Visual Office. Each desk is pinned to "
            "its own model, so this is how you choose which model does a piece of work "
            "— a coding desk on a local GPU, a vision desk on a cloud model, and so on. "
            "The child runs in an isolated context and returns a summary. "
            "Use action='list' to see the desks, 'status' to poll a background worker, "
            "'cancel' to stop one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "What the desk should accomplish. Be specific and self-contained "
                        "— the worker knows nothing about this conversation."
                    ),
                },
                "desk": desk_property,
                "context": {
                    "type": "string",
                    "description": "Background the worker needs: file paths, errors, constraints.",
                },
                "role": {
                    "type": "string",
                    "enum": ["leaf", "orchestrator"],
                    "description": "'leaf' (default) cannot delegate further; 'orchestrator' can.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Wait for the result (default true). False returns a subagent_id to poll.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait before returning a pollable handle.",
                },
                "action": {
                    "type": "string",
                    "enum": ["spawn", "status", "list", "cancel"],
                    "description": "Default 'spawn'. Omit for normal delegation.",
                },
                "subagent_id": {
                    "type": "string",
                    "description": "Target for action='status' or 'cancel'.",
                },
                "message": {
                    "type": "string",
                    "description": "Reason recorded when cancelling.",
                },
            },
            "required": [],
        },
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    global _CTX
    _CTX = ctx

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_approval_request", on_pre_approval_request)
    ctx.register_hook("post_approval_response", on_post_approval_response)
    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("subagent_stop", on_subagent_stop)

    roster = _roster(refresh=True)
    # The tool lives in the `delegation` toolset on purpose: it *is* delegation,
    # and that toolset is already enabled wherever delegate_task is, so enabling
    # this plugin does not also require editing platform_toolsets.
    ctx.register_tool(
        name="office_delegate",
        toolset="delegation",
        schema=_build_schema(roster),
        handler=handle_office_delegate,
        description="Delegate to a desk pinned to its own model.",
        emoji="🪑",
    )

    _announce_roster()
    logger.info(
        "visual_office ready — %d desk(s) from %s", len(roster.desks), roster.source
    )
