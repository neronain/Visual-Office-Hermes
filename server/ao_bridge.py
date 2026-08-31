#!/usr/bin/env python3
"""Draw Agent Orchestrator's workers in the pixel office.

`Agent Orchestrator <https://github.com/Untrivial-ai/agent-orchestrator>`_ runs a
fleet of coding agents, one git worktree each, and keeps a Go daemon watching
them. It answers the same questions this office asks — who is working, on what,
with which agent — over a REST API on loopback. So the room can draw AO's
workers without AO knowing the room exists.

Nothing here touches AO. It reads, and it posts events to the office. Stop this
bridge and AO carries on exactly as before.

    ./ao_bridge.py                 # find the daemon, find the office, run
    ./ao_bridge.py --once --dump   # print what AO actually returns, then exit

**What is verified and what is inferred.** The endpoints, the discovery file and
the envelope shapes below were checked against a running daemon (v0.10.3). The
field names *inside* a populated session were not: reading them requires
spawning a real coding agent, which spends real money on somebody's account.
They are read leniently, and ``--dump`` prints the first session verbatim — one
run against a live AO says whether the guesses hold, and `_pick` is where to fix
them if they do not.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

POLL_SECONDS = 3.0
HTTP_TIMEOUT = 8.0

# AO's own words for what an agent is doing, from its architecture notes:
#
#   active        — working
#   idle          — nothing happening
#   waiting_input — at an empty prompt, awaiting its next instruction
#   blocked       — stopped on a pending permission/approval decision
#   exited        — over
#
# AO's display precedence folds waiting_input and blocked together into
# "needs_input", and that is right for this room too: both mean nothing moves
# until a person acts, which is the one thing the office puts at the top.
NEEDS_YOU = {"waiting_input", "blocked"}
FINISHED = {"exited"}


# ---------------------------------------------------------------- discovery


def daemon_base() -> Optional[str]:
    """Where AO's daemon is, from the file it writes when it starts.

    Same handshake this office uses for itself (``server.json``): the daemon
    picks a port and writes it down, and everyone else reads rather than guesses.
    """
    override = os.environ.get("AO_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")

    run_file = os.environ.get("AO_RUN_FILE", "").strip()
    path = pathlib.Path(run_file) if run_file else pathlib.Path.home() / ".ao/running.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    port = data.get("port")
    return f"http://127.0.0.1:{int(port)}" if port else None


def office_base() -> tuple[Optional[str], str]:
    """``(base_url, token)`` for the office server, or ``(None, "")``."""
    url = os.environ.get("VISUAL_OFFICE_URL", "").strip()
    if url:
        return url.rstrip("/"), os.environ.get("VISUAL_OFFICE_TOKEN", "").strip()

    home = os.environ.get("HERMES_HOME", "").strip()
    base = pathlib.Path(home) if home else pathlib.Path.home() / ".hermes"
    try:
        data = json.loads((base / "visual-office/server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    where = data.get("url") or f"http://127.0.0.1:{data.get('port')}"
    return str(where).rstrip("/"), str(data.get("token") or "")


# ---------------------------------------------------------------- http


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _post(url: str, token: str, payload: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT):
        pass


# ---------------------------------------------------------------- reading AO


def _pick(row: dict, *names: str, default: Any = "") -> Any:
    """First of ``names`` that the row actually carries.

    Written to tolerate not knowing: AO is under active development and this
    bridge should bend rather than break when a field is renamed. ``--dump``
    shows what a real session looks like; add the name here when it differs.
    """
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
        camel = name.split("_")[0] + "".join(p.title() for p in name.split("_")[1:])
        if camel in row and row[camel] not in (None, ""):
            return row[camel]
    return default


def sessions(base: str) -> list[dict]:
    body = _get(f"{base}/api/v1/sessions")
    rows = body.get("sessions") if isinstance(body, dict) else body
    return [r for r in (rows or []) if isinstance(r, dict)]


def desks_from(rows: Iterable[dict]) -> list[dict]:
    """One desk per agent+model pair actually in use.

    AO's axis is the task; this room's axis is the brain doing it. Grouping by
    agent and model is what turns one into the other — two workers on the same
    agent and model share a desk the way two turns of one session do.
    """
    seen: dict[str, dict] = {}
    for row in rows:
        agent = str(_pick(row, "agent", "harness", "agent_id", default="agent"))
        model = str(_pick(row, "model", "model_id", default=""))
        desk_id = agent if not model else f"{agent}:{model}"
        desk_id = desk_id.lower().replace(" ", "-")[:32]
        seen.setdefault(desk_id, {
            "id": desk_id,
            "label": agent,
            "model": model or agent,
            "origin": "local",
            "provider": "agent-orchestrator",
            "note": f"AO · {agent}",
            "toolsets": [],
            "role": "leaf",
        })
    return list(seen.values())


def events_for(row: dict, now: float) -> list[dict]:
    """The office events that describe one AO session as it stands."""
    sid = str(_pick(row, "id", "session_id", default=""))
    if not sid:
        return []
    who = f"ao:{sid}"
    agent = str(_pick(row, "agent", "harness", "agent_id", default="agent"))
    model = str(_pick(row, "model", "model_id", default=""))
    desk_id = (agent if not model else f"{agent}:{model}").lower().replace(" ", "-")[:32]
    state = str(_pick(row, "activity_state", "activityState", "state", default="")).lower()
    title = str(_pick(row, "title", "task", "name", "prompt", default=""))

    out: list[dict] = [
        {"event": "subagent_start", "at": now, "session_id": who, "subagent_id": who,
         "goal": title, "role": "leaf"},
        {"event": "desk_assign", "at": now, "subagent_id": who, "desk": desk_id,
         "desk_label": agent, "origin": "local", "model": model or agent,
         "provider": "agent-orchestrator", "goal": title},
    ]

    if state in FINISHED:
        out.append({"event": "session_finalize", "at": now, "session_id": who})
    elif state in NEEDS_YOU:
        # Both of AO's blocked-ish states mean the same thing to a person looking
        # at the room: this one is not going to move on its own.
        out.append({"event": "approval_wait", "at": now, "session_id": who,
                    "description": title or f"{agent} รอคำสั่งถัดไป"})
    elif state == "active":
        out.append({"event": "thinking", "at": now, "session_id": who,
                    "model": model or agent, "platform": "ao"})
    else:
        out.append({"event": "thinking_done", "at": now, "session_id": who,
                    "model": model or agent})
    return out


# ---------------------------------------------------------------- loop


def run(once: bool = False, dump: bool = False) -> int:
    ao = daemon_base()
    if not ao:
        print("หา AO daemon ไม่เจอ — สั่ง `ao daemon` ก่อน หรือชี้ด้วย AO_BASE_URL", file=sys.stderr)
        return 1
    office, token = office_base()
    if not office and not dump:
        print("หาเซิร์ฟเวอร์ห้องไม่เจอ — เปิด Visual Office ก่อน", file=sys.stderr)
        return 1

    print(f"AO    {ao}")
    print(f"ห้อง  {office or '(ไม่ได้ต่อ — โหมด dump)'}")

    announced: Optional[str] = None
    while True:
        try:
            rows = sessions(ao)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"อ่าน AO ไม่ได้: {exc}", file=sys.stderr)
            if once:
                return 1
            time.sleep(POLL_SECONDS)
            continue

        if dump:
            print(json.dumps(rows[0] if rows else {"note": "AO ยังไม่มี session"},
                             ensure_ascii=False, indent=2))
            if once:
                return 0

        now = time.time()
        if office:
            roster = {"office_name": "Visual Office", "gateway_base_url": ao,
                      "desks": desks_from(rows), "source": f"{ao}/api/v1/sessions"}
            signature = json.dumps(roster["desks"], sort_keys=True)
            try:
                if signature != announced:
                    _post(f"{office}/api/events", token,
                          {"event": "roster", "at": now, "roster": roster})
                    announced = signature
                for row in rows:
                    for event in events_for(row, now):
                        _post(f"{office}/api/events", token, event)
            except (urllib.error.URLError, OSError) as exc:
                print(f"ส่งเข้าห้องไม่ได้: {exc}", file=sys.stderr)

        if once:
            return 0
        time.sleep(POLL_SECONDS)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="รอบเดียวแล้วออก")
    parser.add_argument("--dump", action="store_true",
                        help="พิมพ์ session แรกที่ AO คืนมาดิบ ๆ (ใช้ตรวจชื่อฟิลด์)")
    args = parser.parse_args(argv)
    try:
        return run(once=args.once, dump=args.dump)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
