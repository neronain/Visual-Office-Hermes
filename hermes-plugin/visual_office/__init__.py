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

A desk that carries its own ``base_url`` is handled by :mod:`.direct` instead:
it talks to that endpoint over plain OpenAI-compatible HTTP, so it answers even
when the gateway is down. It gives up tools and the agent loop to do that. Both
kinds share one id space, so nothing outside has to know which is which.

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

from . import direct
from . import gateway as gw
from .desks import Roster, desks_path, load_roster
from .sink import Sink

logger = logging.getLogger(__name__)

PLUGIN_ID = "visual_office"
DEFAULT_WAIT_SECONDS = 600.0

_SINK = Sink()
_ROSTER: Optional[Roster] = None
_ROSTER_LOCK = threading.Lock()
_ROSTER_STAMP: Optional[tuple] = None
_TOOL_SIGNATURE: Optional[tuple] = None
_HANDLES: dict[str, Any] = {}
_HANDLES_LOCK = threading.Lock()
_REPORT_LOCK = threading.Lock()
_CTX: Any = None

# Live gateway sessions we could inject a message into, newest first. Captured
# from the session context during hooks — the office page has no other way to
# learn a session key, and injecting needs one.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()
_COMMAND_THREAD: Optional[threading.Thread] = None
COMMAND_POLL_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


def _roster_stamp() -> Optional[tuple]:
    """Cheap change detector for the roster file — mtime and size."""
    try:
        stat = desks_path().stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _roster(refresh: bool = False) -> Roster:
    """The desk roster, re-read whenever the file on disk has changed.

    Editing desks has to take effect in a running Hermes, not only after a
    restart — the desk editor writes the file and the next turn must see it.
    """
    global _ROSTER, _ROSTER_STAMP
    with _ROSTER_LOCK:
        stamp = _roster_stamp()
        if _ROSTER is None or refresh or stamp != _ROSTER_STAMP:
            _ROSTER = load_roster()
            _ROSTER_STAMP = stamp
            for problem in _ROSTER.problems:
                logger.warning("visual_office roster: %s", problem)
        return _ROSTER


def _sync_tool(roster: Roster) -> None:
    """Rebuild the tool schema when the desks change.

    The desk list is an enum in the schema, so a desk added while Hermes is
    running is invisible to the model until the tool is registered again. The
    signature check keeps this a no-op on the overwhelming majority of calls.
    """
    global _TOOL_SIGNATURE
    if _CTX is None:
        return
    signature = tuple(
        (d.id, d.label, d.model, d.origin, d.note, d.role, d.toolsets)
        for d in roster.desks
    )
    if signature == _TOOL_SIGNATURE:
        return
    try:
        _CTX.register_tool(
            name="office_delegate",
            toolset="delegation",
            schema=_build_schema(roster),
            handler=handle_office_delegate,
            description="Delegate to a desk pinned to its own model.",
            emoji="🪑",
        )
    except Exception as exc:
        logger.warning("visual_office could not refresh office_delegate: %s", exc)
        return
    _TOOL_SIGNATURE = signature
    # Tell the office too, so its editor is looking at the same list Hermes is.
    _SINK.emit({"event": "roster", "at": time.time(), "roster": roster.to_dict()})
    logger.info("visual_office: desk roster changed — %d desk(s)", len(roster.desks))


def _refresh() -> Roster:
    roster = _roster()
    _sync_tool(roster)
    return roster


def _announce_roster() -> None:
    """Tell the office which desks exist, and what the gateway says they can do.

    The main model is reported, never written. Which model the top-level agent
    runs on is the owner's decision and theirs alone — a plugin that quietly
    re-asserted it from a second file would change the agent's brain as a side
    effect of saving a desk.
    """
    roster = _refresh()
    payload = roster.to_dict()
    payload["available_models"] = gw.catalog()
    payload["agent_model"] = gw.agent_model()[0]
    _SINK.emit({"event": "roster", "at": time.time(), "roster": payload})


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
    _remember_session(kw.get("session_id"), kw.get("platform"))
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
    # Also the cheapest reliable point to notice an edited desk roster: tool
    # definitions are rebuilt per call, so a desk added a second ago is usable
    # on the very next message rather than after a restart.
    _refresh()
    _remember_session(kw.get("session_id"), kw.get("platform"))
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


REPLY_MAX_CHARS = 4000


def _assistant_text(message: Any) -> str:
    """The assistant's own words, if this call produced any."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # Some providers hand back content blocks rather than a plain string.
        parts = []
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


def on_post_api_request(**kw: Any) -> None:
    """The model dimension. This is the hook the whole project exists for."""
    usage = kw.get("usage")

    # The reply itself, so the office can show what came back instead of
    # sending the reader to the chat app. Most calls in an agent loop return
    # tool calls and no prose — those are not worth showing.
    text = _assistant_text(kw.get("assistant_message"))
    if text:
        _emit(
            "reply",
            session_id=kw.get("session_id"),
            platform=kw.get("platform"),
            model=kw.get("model"),
            text=text[:REPLY_MAX_CHARS],
            truncated=len(text) > REPLY_MAX_CHARS,
            tool_calls=kw.get("assistant_tool_call_count"),
        )

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
    # Approval hooks carry session_key, not session_id. The command and its
    # description arrive already redacted by Hermes (redact_sensitive_text with
    # force=True), so they are safe to put on a screen.
    _emit(
        "approval_wait",
        session_id=kw.get("session_key") or kw.get("session_id"),
        command=str(kw.get("command") or "")[:400] or None,
        description=str(kw.get("description") or "")[:400] or None,
    )


def on_post_approval_response(**kw: Any) -> None:
    _emit(
        "approval_done",
        session_id=kw.get("session_key") or kw.get("session_id"),
        verdict=str(kw.get("verdict") or "") or None,
    )


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
# Commands from the office page
# ---------------------------------------------------------------------------


def _remember_session(session_id: Any, platform: Any = None) -> None:
    """Note the live session key for this session, if the gateway bound one.

    A subagent launch needs an active parent bound to the calling context, which
    a background thread does not have. So a command from the web page is
    delivered the only way that works from outside a turn: as a message into a
    live session, which Hermes then runs normally.
    """
    sid = str(session_id or "")
    if not sid:
        return
    try:
        from gateway.session_context import get_session_env

        key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception:
        key = ""
    fresh = False
    with _SESSIONS_LOCK:
        entry = _SESSIONS.setdefault(sid, {})
        entry["at"] = time.time()
        if key and entry.get("key") != key:
            entry["key"] = key
            fresh = True
        if platform:
            entry["platform"] = str(platform)
    if fresh:
        _save_sessions()


def _sessions_file():
    return desks_path().with_name("sessions.json")


def _save_sessions() -> None:
    """Remember session keys across a gateway restart.

    Without this, the office cannot send anything until somebody happens to talk
    to Hermes first — which is exactly the moment they were trying to avoid by
    using the office in the first place.
    """
    try:
        with _SESSIONS_LOCK:
            live = {k: v for k, v in _SESSIONS.items() if v.get("key")}
        path = _sessions_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("visual_office could not save sessions: %s", exc)


def _load_sessions() -> None:
    try:
        data = json.loads(_sessions_file().read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    with _SESSIONS_LOCK:
        for sid, entry in data.items():
            if isinstance(entry, dict) and entry.get("key"):
                _SESSIONS.setdefault(sid, entry)


def _newest_session() -> Optional[dict[str, Any]]:
    """The conversation a command should land in.

    First choice is a session this process has actually seen. Failing that, a
    channel the gateway knows about — its key is built by Hermes' own builder,
    so an office that has just restarted can still send.
    """
    with _SESSIONS_LOCK:
        live = [e for e in _SESSIONS.values() if e.get("key")]
    if live:
        return max(live, key=lambda e: e["at"])

    for channel in gw.known_channels():
        key = gw.session_key_for(channel)
        if key:
            return {"key": key, "platform": channel["platform"], "at": 0.0}
    return None


def _command_text(command: dict[str, Any]) -> str:
    text = str(command.get("text") or "").strip()
    desk = str(command.get("desk") or "").strip()
    if not desk:
        return text
    return (
        f"ใช้เครื่องมือ office_delegate ส่งงานนี้ไปที่โต๊ะ {desk} "
        f"แล้วรายงานผลกลับมา · งาน: {text}"
    )


def _run_command(command: dict[str, Any]) -> None:
    command_id = command.get("id")
    result: dict[str, Any] = {"id": command_id}

    if _CTX is None:
        result.update(ok=False, error="ปลั๊กอินยังไม่พร้อม")
    else:
        session = _newest_session()
        if session is None:
            result.update(
                ok=False,
                error="ยังไม่มี session ที่ส่งข้อความเข้าไปได้ — คุยกับ Hermes สักครั้งก่อน "
                "(ผ่าน Telegram หรือ CLI) แล้วลองใหม่",
            )
        else:
            try:
                sent = bool(_CTX.inject_message(_command_text(command), session_key=session["key"]))
            except Exception as exc:  # pragma: no cover - defensive
                sent = False
                result["error"] = str(exc)
            if sent:
                result.update(ok=True, platform=session.get("platform", ""))
            else:
                result.setdefault(
                    "error",
                    "Hermes ปฏิเสธการส่งข้อความ — ส่วนใหญ่คือยังไม่ได้เปิดสิทธิ์ "
                    "plugins.entries.visual_office.allow_gateway_injection: true "
                    "ใน ~/.hermes/config.yaml (ดู log ของ Hermes ประกอบ)",
                )
                result["ok"] = False

    _SINK.post_now("/api/command/result", result)
    logger.info("visual_office command %s -> %s", command_id, result.get("ok"))


def _command_loop() -> None:
    while True:
        time.sleep(COMMAND_POLL_SECONDS)
        try:
            command = _SINK.get_json("/api/command/next")
            if command and command.get("text"):
                _run_command(command)
        except Exception:  # pragma: no cover - a poller must never die
            logger.debug("visual_office command poll failed", exc_info=True)


def _start_command_loop() -> None:
    global _COMMAND_THREAD
    if _COMMAND_THREAD is not None and _COMMAND_THREAD.is_alive():
        return
    _COMMAND_THREAD = threading.Thread(
        target=_command_loop, name="visual-office-commands", daemon=True
    )
    _COMMAND_THREAD.start()


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
    roster = _refresh()
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

    wait_for_it = params.get("wait")
    wait_for_it = True if wait_for_it is None else bool(wait_for_it)
    timeout = float(params.get("timeout_seconds") or _wait_seconds())

    if desk.direct:
        return _spawn_direct(desk, goal, params, wait_for_it, timeout)

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

    if not wait_for_it:
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


def _direct_payload(run: direct.DirectRun, **extra: Any) -> str:
    return json.dumps(
        {
            "success": run.state == direct.SUCCEEDED,
            "subagent_id": run.run_id,
            "desk": run.desk_id,
            "desk_label": run.desk_label,
            "model": run.model,
            "state": run.state,
            "summary": run.summary or None,
            "error": run.error or None,
            "usage": dict(run.usage),
            "duration_seconds": run.duration_seconds,
            "via": run.base_url,
            **extra,
        },
        ensure_ascii=False,
    )


def _spawn_direct(desk, goal: str, params: dict, wait_for_it: bool, timeout: float) -> str:
    """โต๊ะที่ชี้ endpoint ของตัวเอง — ไม่ผ่าน Hermes จึงต้องส่งเหตุการณ์เข้าห้องเอง

    subagent จริงมี hook ของ Hermes คอยส่ง subagent_start/stop และคำตอบให้อยู่แล้ว
    ทางนี้ไม่มีใครส่งให้ ถ้าไม่ทำเองตัวละครจะไม่ขยับและคำตอบจะไม่ขึ้นบนหน้าจอเลย
    """
    context = str(params.get("context") or "") or None
    run = direct.launch(desk, goal, context, timeout)

    _emit(
        "desk_assign",
        subagent_id=run.run_id,
        desk=desk.id,
        desk_label=desk.label,
        origin=desk.origin,
        model=desk.model,
        provider=desk.provider,
        goal=goal,
        via=desk.base_url,
    )
    _emit(
        "subagent_start",
        session_id=run.run_id,
        subagent_id=run.run_id,
        role=desk.role,
        goal=goal,
    )

    if not wait_for_it:
        _watch_direct(run.run_id, timeout)
        return json.dumps(
            {
                "success": True,
                "subagent_id": run.run_id,
                "desk": desk.id,
                "model": desk.model,
                "state": direct.RUNNING,
                "via": desk.base_url,
                "note": "Running in the background. Poll with action='status'.",
            },
            ensure_ascii=False,
        )

    direct.wait(run.run_id, timeout)
    _report_direct(run)
    if not run.terminal:
        return _direct_payload(
            run,
            note=f"Still working after {timeout:.0f}s. Poll with action='status'.",
            success=True,
        )
    return _direct_payload(run)


def _report_direct(run: direct.DirectRun) -> None:
    """ส่งคำตอบและการจบงานเข้าห้อง — ครั้งเดียวต่อหนึ่งงาน

    งานหนึ่งชิ้นมีได้สามคนที่เจอว่ามันจบ: ตัวที่รออยู่, การ poll status, และเธรดที่
    เฝ้าอยู่เบื้องหลัง · ถ้าไม่กันไว้ ห้องจะได้คำตอบเดียวกันซ้ำสองสามรอบ
    """
    with _REPORT_LOCK:
        if not run.terminal or run._reported:
            return
        run._reported = True
    if run.summary:
        _emit(
            "reply",
            session_id=run.run_id,
            subagent_id=run.run_id,
            model=run.model,
            text=run.summary[:REPLY_MAX_CHARS],
        )
    _emit(
        "subagent_stop",
        session_id=run.run_id,
        subagent_id=run.run_id,
        status=run.state,
        summary=run.summary or run.error,
        duration_ms=int((run.duration_seconds or 0) * 1000),
    )


def _watch_direct(run_id: str, timeout: float) -> None:
    """รอผลของงานเบื้องหลังในเธรดแยก เพื่อให้ห้องได้เห็นคำตอบแม้ไม่มีใครมา poll"""
    def _wait_then_report() -> None:
        run = direct.wait(run_id, timeout)
        if run is not None:
            _report_direct(run)

    threading.Thread(
        target=_wait_then_report, name=f"visual-office-watch-{run_id}", daemon=True
    ).start()


def _handle_status(params: dict) -> str:
    subagent_id = str(params.get("subagent_id") or "").strip()
    if not subagent_id:
        return _fail("action='status' needs a subagent_id.")

    run = direct.get(subagent_id)
    if run is not None:
        _report_direct(run)
        return _direct_payload(run, success=True)

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
    roster = _refresh()
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
    for run in direct.running():
        running.append(
            {
                "subagent_id": run.run_id,
                "model": run.model,
                "state": run.state,
                "desk": run.desk_id,
                "via": run.base_url,
            }
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
    reason = str(params.get("message") or "Cancelled from office_delegate.")

    run = direct.get(subagent_id)
    if run is not None:
        # คำขอ HTTP ที่ยิงออกไปแล้วเรียกกลับไม่ได้ — ที่ทำได้คือเลิกรับผลของมัน
        already = run.terminal
        direct.cancel(subagent_id, reason)
        _report_direct(run)
        return json.dumps(
            {
                "success": True,
                "subagent_id": subagent_id,
                "state": run.state,
                "already_terminal": already,
                "note": "The request already in flight is ignored, not stopped.",
            },
            ensure_ascii=False,
        )

    with _HANDLES_LOCK:
        handle = _HANDLES.get(subagent_id)
    if handle is None:
        return _fail(f"Unknown subagent_id {subagent_id!r}.")
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

    # The tool lives in the `delegation` toolset on purpose: it *is* delegation,
    # and that toolset is already enabled wherever delegate_task is, so enabling
    # this plugin does not also require editing platform_toolsets.
    roster = _roster(refresh=True)
    _sync_tool(roster)
    _load_sessions()
    _announce_roster()
    _start_command_loop()
    logger.info(
        "visual_office ready — %d desk(s) from %s", len(roster.desks), roster.source
    )
