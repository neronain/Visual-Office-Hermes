"""Fold a stream of Hermes events into the state of an office.

The server keeps no database. It replays an append-only event log into a single
in-memory snapshot, which is what the UI renders and what ``/api/state``
returns. That makes restarts cheap and makes the log the only source of truth.

The one thing this model does that a plain activity feed does not: it carries
the *model* on every worker. Hermes' ``post_api_request`` hook hands us model,
provider and base URL on every call, so a character in the office can be labelled
with the brain behind it and the tokens it has spent.

**Facts are stored; what you see is computed.** Events write only things that
are true and stay true — when somebody arrived, whether they are mid-thought,
which tool is open, whether an approval is pending, when they finished. The
activity a character shows is derived from those facts at read time, by one
ordered set of rules in :func:`_display`.

That split is the whole design, and it is worth stating why. The first version
mutated ``activity`` and ``status`` directly on every event, which made what you
saw depend on the order events happened to arrive in and on nothing ever being
missed. Three bugs came out of that in one week: a character walked out of the
room every time it finished answering, a session that resumed under its old id
stayed flagged as gone, and a desk deleted from the roster kept its occupant.
None of them are reachable now — there is no ``activity`` field to leave stale,
because there is no ``activity`` field at all until somebody asks.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Optional

# Tool name -> what the character is visibly doing. Order matters: the first
# fragment that appears in the tool name wins.
_ACTIVITY_BY_FRAGMENT: tuple[tuple[str, str], ...] = (
    ("write_file", "typing"),
    ("edit", "typing"),
    ("patch", "typing"),
    ("apply", "typing"),
    ("create", "typing"),
    ("read_file", "reading"),
    ("read", "reading"),
    ("search", "reading"),
    ("grep", "reading"),
    ("glob", "reading"),
    ("list", "reading"),
    ("view", "reading"),
    ("browser", "browsing"),
    ("web", "browsing"),
    ("fetch", "browsing"),
    ("shell", "running"),
    ("terminal", "running"),
    ("bash", "running"),
    ("exec", "running"),
    ("code_execution", "running"),
    ("delegate", "pointing"),
    ("office_delegate", "pointing"),
    ("memory", "filing"),
    ("todo", "filing"),
    ("kanban", "filing"),
    ("image", "drawing"),
    ("vision", "drawing"),
)

# Events that mean somebody left. Everything else means they are here.
DEPARTURES = frozenset({"session_end", "session_finalize", "session_reset", "subagent_stop"})

IDLE_AFTER_SECONDS = 90.0
GONE_AFTER_SECONDS = 40.0    # long enough to watch someone walk out, short enough to empty the room
# A session that has said nothing for this long is presumed over. Hermes fires
# on_session_finalize on a clean exit, but a killed process never gets to.
SILENT_AFTER_SECONDS = 1800.0

# What the agents actually said, kept so the office can answer "did it work?"
# without sending the reader to the chat app. Memory only — never written to
# events.jsonl, so a restart forgets it and nothing lands on disk.
TRANSCRIPT_MAX = 60


def activity_for_tool(tool_name: Optional[str]) -> str:
    name = (tool_name or "").lower()
    if not name:
        return "working"
    for fragment, activity in _ACTIVITY_BY_FRAGMENT:
        if fragment in name:
            return activity
    return "working"


# How long after arriving a character is still shown walking in. Short: it is an
# animation cue, not a state anybody needs to read.
ARRIVING_SECONDS = 4.0


def _display(worker: dict[str, Any], now: float) -> tuple[str, str, bool]:
    """``(activity, status, needs_input)`` for one worker, right now.

    This is the only place a display value is decided, and the order below is
    the whole rule. Read it top to bottom; the first line that matches wins.

    ==========================  =============  ==========================================
    when                        activity       why it outranks what follows
    ==========================  =============  ==========================================
    finished                    leaving        they are on their way out; nothing else matters
    approval pending            waiting        the only state where nothing moves without a person
    a tool is open              per the tool   the most specific thing we know they are doing
    mid-thought                 thinking       they are working, we just cannot see at what
    just arrived, nothing yet   arriving       still walking in
    silent for a while          idle           no evidence of work for long enough to believe it
    otherwise                   working        seen recently, nothing more specific
    ==========================  =============  ==========================================

    Nothing here is remembered. Two calls a second apart can disagree, and that
    is the point: a character goes idle because time passed, not because some
    event arrived to say so — and no event can leave a stale label behind.
    """
    if worker["finished_at"] is not None:
        return "leaving", (worker["outcome"] or "closed"), False
    if worker["awaiting_approval"]:
        return "waiting", "waiting", True
    if worker["tool"]:
        return activity_for_tool(worker["tool"]), "running", False
    if worker["thinking_since"] is not None:
        return "thinking", "running", False
    # "Arriving" has to mean *and nothing has happened since*, not merely that
    # the clock is young: a session that starts and finishes a turn inside the
    # window is not still walking in. updated_at moves on every event, so this
    # holds only while the arrival is the last thing we know about.
    if (
        worker["updated_at"] <= worker["started_at"]
        and (now - worker["started_at"]) < ARRIVING_SECONDS
    ):
        return "arriving", "running", False
    if (now - worker["updated_at"]) > IDLE_AFTER_SECONDS:
        return "idle", "waiting", False
    return "working", "running", False


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tokens(usage: Any) -> tuple[int, int]:
    """Pull (input, output) out of a usage dict in whichever dialect it arrives."""
    if not isinstance(usage, dict):
        return 0, 0
    incoming = (
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("input")
        or 0
    )
    outgoing = (
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("output")
        or 0
    )
    return int(_num(incoming)), int(_num(outgoing))


class Office:
    """Thread-safe office snapshot built by folding events."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.seq = 0
        self.started_at = time.time()
        self.office_name = "Visual Office"
        self.gateway_base_url = ""
        self.roster_source = ""
        self.roster_problems: list[str] = []
        self.roster_at = 0.0
        self.available_models: list[dict[str, Any]] = []
        self.agent_model = ""
        self.main_model = ""
        self.desks: dict[str, dict[str, Any]] = {}
        self.desk_order: list[str] = []
        self.workers: dict[str, dict[str, Any]] = {}
        # subagent_id -> session_id, and the desk assignment that may arrive
        # before or after the subagent_start hook. Both orders happen.
        self.subagent_sessions: dict[str, str] = {}
        self.pending_desks: dict[str, dict[str, Any]] = {}
        # Running totals, kept apart from the worker list. A character leaves the
        # floor after a minute; what it spent should not leave with it.
        self.transcript: list[dict[str, Any]] = []
        self.totals = {"calls": 0, "tokens_in": 0, "tokens_out": 0}
        self.by_origin: dict[str, dict[str, int]] = {}
        self.by_model: dict[str, dict[str, int]] = {}

    # -- helpers ------------------------------------------------------------

    def _worker(self, session_id: str, now: float) -> dict[str, Any]:
        """The facts we hold about one character. Nothing here is a display value.

        Every field is either identity (who this is, which model, which desk),
        a counter, or a timestamp saying when something became true. What the
        character is *doing* is not stored — :func:`_display` works it out.
        """
        worker = self.workers.get(session_id)
        if worker is None:
            worker = {
                # identity
                "id": session_id,
                "kind": "session",
                "session_id": session_id,
                "subagent_id": None,
                "parent_session_id": None,
                "desk": None,
                "desk_label": None,
                "origin": "unknown",
                "label": session_id[:8],
                "platform": "",
                "model": "",
                "provider": "",
                "base_url": "",
                "goal": None,
                # when things became true
                "started_at": now,
                "updated_at": now,
                "thinking_since": None,   # mid-thought until the turn or thinking_done
                "tool": None,             # the tool currently open, by name
                "awaiting_approval": False,
                "awaiting_since": None,   # ตั้งแต่เมื่อไร — คนอ่านอยากรู้ว่ารอมานานแค่ไหน
                "awaiting_text": "",      # รออนุมัติ *อะไร* · Hermes redact มาให้แล้ว
                "finished_at": None,      # a real departure, not the end of a turn
                "outcome": "",            # completed / failed / interrupted / closed
                # counters
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "last_duration": None,
            }
            self.workers[session_id] = worker
        return worker

    def _apply_desk(self, worker: dict[str, Any], assignment: dict[str, Any]) -> None:
        worker["desk"] = assignment.get("desk")
        worker["desk_label"] = assignment.get("desk_label")
        worker["origin"] = assignment.get("origin") or worker.get("origin") or "unknown"
        if assignment.get("model"):
            worker["model"] = assignment["model"]
        if assignment.get("provider"):
            worker["provider"] = assignment["provider"]
        if assignment.get("goal") and not worker.get("goal"):
            worker["goal"] = assignment["goal"]
        if assignment.get("desk_label"):
            worker["label"] = assignment["desk_label"]

    def _desk_totals(self, desk_id: Optional[str], tokens_in: int, tokens_out: int) -> None:
        if not desk_id:
            return
        desk = self.desks.get(desk_id)
        if desk is None:
            return
        desk["calls"] += 1
        desk["tokens_in"] += tokens_in
        desk["tokens_out"] += tokens_out

    # -- fold ---------------------------------------------------------------

    def apply(self, event: dict[str, Any]) -> None:
        """Fold one event into the snapshot. Unknown events are ignored."""
        with self._lock:
            self._apply_locked(event)

    def apply_many(self, events: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            for event in events:
                self._apply_locked(event)

    def _apply_locked(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event") or "").strip()
        if not kind:
            return
        now = _num(event.get("at")) or time.time()
        self.seq += 1

        if kind == "roster":
            self._apply_roster(event.get("roster") or {}, now)
            return

        if kind == "desk_assign":
            subagent_id = str(event.get("subagent_id") or "")
            if not subagent_id:
                return
            assignment = {
                "desk": event.get("desk"),
                "desk_label": event.get("desk_label"),
                "origin": event.get("origin"),
                "model": event.get("model"),
                "provider": event.get("provider"),
                "goal": event.get("goal"),
            }
            session_id = self.subagent_sessions.get(subagent_id)
            if session_id and session_id in self.workers:
                self._apply_desk(self.workers[session_id], assignment)
            else:
                self.pending_desks[subagent_id] = assignment
            return

        session_id = str(event.get("session_id") or "")
        if not session_id:
            return
        worker = self._worker(session_id, now)
        if kind in ("reply", "approval_wait", "approval_done"):
            self._remember_said(kind, event, worker, now)
        worker["updated_at"] = now

        # A session can come back after it was finalized: the gateway resumes an
        # interrupted session under the same id after a restart. Anything that is
        # not itself a departure is proof it is still here.
        if kind not in DEPARTURES and worker["finished_at"] is not None:
            worker["finished_at"] = None
            worker["outcome"] = ""

        if kind == "session_start":
            worker["platform"] = str(event.get("platform") or worker["platform"])
            worker["started_at"] = now

        elif kind == "subagent_start":
            subagent_id = str(event.get("subagent_id") or "")
            worker["kind"] = "subagent"
            worker["subagent_id"] = subagent_id or worker["subagent_id"]
            worker["parent_session_id"] = event.get("parent_session_id")
            worker["goal"] = event.get("goal") or worker["goal"]
            worker["started_at"] = now
            if subagent_id:
                self.subagent_sessions[subagent_id] = session_id
                pending = self.pending_desks.pop(subagent_id, None)
                if pending:
                    self._apply_desk(worker, pending)

        elif kind == "subagent_stop":
            worker["outcome"] = str(event.get("status") or "completed")
            worker["finished_at"] = now
            worker["thinking_since"] = None
            worker["tool"] = None
            if event.get("duration_ms") is not None:
                worker["last_duration"] = _num(event.get("duration_ms")) / 1000.0

        elif kind == "session_end":
            # Despite the name, Hermes fires this at the end of every *turn*:
            # "Fired at the very end of every run_conversation call" (its own
            # comment). Real session teardown is on_session_finalize. So this
            # ends the turn — it does not end the person.
            worker["thinking_since"] = None
            worker["tool"] = None
            worker["awaiting_approval"] = False
            if event.get("failed"):
                worker["outcome"] = "failed"
            elif event.get("interrupted"):
                worker["outcome"] = "interrupted"
            if event.get("model"):
                worker["model"] = event["model"]

        elif kind in ("session_finalize", "session_reset"):
            worker["outcome"] = "closed"
            worker["finished_at"] = now
            worker["thinking_since"] = None
            worker["tool"] = None

        elif kind == "thinking":
            worker["thinking_since"] = now
            worker["awaiting_approval"] = False
            if event.get("model"):
                worker["model"] = event["model"]
            if event.get("platform"):
                worker["platform"] = event["platform"]

        elif kind == "thinking_done":
            worker["thinking_since"] = None

        elif kind == "tool_start":
            worker["tool"] = event.get("tool_name")

        elif kind == "tool_end":
            worker["tool"] = None

        elif kind == "reply":
            # Nothing about the room changes; the words were already filed above.
            pass

        elif kind == "approval_wait":
            worker["awaiting_approval"] = True
            worker["awaiting_since"] = now
            worker["awaiting_text"] = (
                str(event.get("description") or "").strip()
                or str(event.get("command") or "").strip()
            )

        elif kind == "approval_done":
            worker["awaiting_approval"] = False
            worker["awaiting_since"] = None
            worker["awaiting_text"] = ""

        elif kind == "api_request":
            tokens_in, tokens_out = _tokens(event.get("usage"))
            worker["calls"] += 1
            worker["tokens_in"] += tokens_in
            worker["tokens_out"] += tokens_out
            worker["last_duration"] = _num(event.get("api_duration")) or worker["last_duration"]
            for field in ("model", "provider", "base_url", "platform"):
                if event.get(field):
                    worker[field] = event[field]
            self._desk_totals(worker.get("desk"), tokens_in, tokens_out)

            self.totals["calls"] += 1
            self.totals["tokens_in"] += tokens_in
            self.totals["tokens_out"] += tokens_out
            for bucket, key in (
                (self.by_origin, worker.get("origin") or "unknown"),
                (self.by_model, worker.get("model") or "unknown"),
            ):
                row = bucket.setdefault(key, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
                row["calls"] += 1
                row["tokens_in"] += tokens_in
                row["tokens_out"] += tokens_out

    def _remember_said(
        self, kind: str, event: dict[str, Any], worker: dict[str, Any], now: float
    ) -> None:
        if kind == "reply":
            text = str(event.get("text") or "").strip()
            if not text:
                return
            entry = {"kind": "reply", "text": text, "truncated": bool(event.get("truncated"))}
        elif kind == "approval_wait":
            command = str(event.get("command") or "").strip()
            description = str(event.get("description") or "").strip()
            if not command and not description:
                return
            entry = {"kind": "approval", "text": description or command, "command": command}
        else:
            verdict = str(event.get("verdict") or "").strip()
            if not verdict:
                return
            entry = {"kind": "verdict", "text": verdict}

        entry.update(
            at=now,
            session_id=worker["id"],
            platform=worker.get("platform", ""),
            desk=worker.get("desk"),
            desk_label=worker.get("desk_label"),
            model=str(event.get("model") or worker.get("model") or ""),
        )
        self.transcript.append(entry)
        del self.transcript[:-TRANSCRIPT_MAX]

    def said(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.transcript[-limit:][::-1]]

    def forget_said(self) -> int:
        """ทิ้งคำตอบที่เก็บไว้ทั้งหมด แล้วบอกว่าทิ้งไปกี่รายการ

        แผงนี้เก็บเฉพาะในหน่วยความจำอยู่แล้ว การล้างจึงไม่แตะอะไรบนดิสก์ และไม่กระทบ
        ตัวเลขที่นับสะสมไว้ — คนกดล้างอยากได้หน้าจอที่โล่ง ไม่ได้อยากลบประวัติการใช้งาน
        """
        with self._lock:
            count = len(self.transcript)
            self.transcript.clear()
            return count

    def _apply_roster(self, roster: dict[str, Any], announced_at: float) -> None:
        # The event's own timestamp, not wall-clock: replaying the log on start
        # must not make an old announcement look like it just happened, or the
        # editor stops noticing that the file has moved on without it.
        self.roster_at = announced_at
        self.office_name = str(roster.get("office_name") or self.office_name)
        models = roster.get("available_models")
        if isinstance(models, list):
            self.available_models = models
        self.agent_model = str(roster.get("agent_model") or self.agent_model)
        self.main_model = str(roster.get("main_model") or "")
        self.gateway_base_url = str(roster.get("gateway_base_url") or self.gateway_base_url)
        self.roster_source = str(roster.get("source") or self.roster_source)
        self.roster_problems = [str(p) for p in (roster.get("problems") or [])]

        order: list[str] = []
        for raw in roster.get("desks") or []:
            if not isinstance(raw, dict):
                continue
            desk_id = str(raw.get("id") or "").strip()
            if not desk_id:
                continue
            order.append(desk_id)
            desk = self.desks.get(desk_id)
            if desk is None:
                desk = {"calls": 0, "tokens_in": 0, "tokens_out": 0}
                self.desks[desk_id] = desk
            desk.update(
                {
                    "id": desk_id,
                    "label": str(raw.get("label") or desk_id),
                    "model": str(raw.get("model") or ""),
                    "origin": str(raw.get("origin") or "unknown"),
                    "provider": str(raw.get("provider") or ""),
                    "note": str(raw.get("note") or ""),
                    "role": str(raw.get("role") or "leaf"),
                    "toolsets": list(raw.get("toolsets") or []),
                    # ว่าง = ผ่าน gateway · หน้าจัดการโต๊ะอ่านค่านี้เพื่อรู้ว่าโต๊ะนี้ต่อทางไหน
                    "base_url": str(raw.get("base_url") or ""),
                    "api_key_env": str(raw.get("api_key_env") or ""),
                }
            )
        if order:
            self.desk_order = order
            # โต๊ะที่หายไปจากรายชื่อต้องหายไปจากห้องด้วย · self.desks เก็บสะสมไว้เรื่อย ๆ
            # โต๊ะที่ถูกลบจึงค้างอยู่ในหน่วยความจำตลอดอายุโปรเซส และโผล่กลับมาทันทีที่มี
            # ใครมาอ่าน snapshot · ยอดรวมไม่หายไปด้วยเพราะมันเก็บแยกใน totals/by_model
            for gone in [d for d in self.desks if d not in set(order)]:
                del self.desks[gone]

    # -- snapshot -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            workers = []
            for worker in self.workers.values():
                ended = worker["finished_at"]
                if ended is None and (now - worker["updated_at"]) > SILENT_AFTER_SECONDS:
                    # Never finalized, never heard from — the process is gone.
                    # Hermes fires on_session_finalize on a clean exit; a killed
                    # process never gets the chance, so silence has to count.
                    ended = worker["updated_at"]
                if ended and (now - ended) > GONE_AFTER_SECONDS:
                    continue

                view = dict(worker)
                # Facts stay as they are; the display values are worked out here
                # and nowhere else. A worker whose silence just made it "gone"
                # gets that treatment too, without its stored facts being touched.
                if ended is not worker["finished_at"]:
                    activity, status, needs_input = "leaving", "closed", False
                else:
                    activity, status, needs_input = _display(worker, now)
                view["activity"] = activity
                view["status"] = status
                view["needs_input"] = needs_input
                view["ended_at"] = ended
                view["gone"] = bool(ended)
                workers.append(view)

            workers.sort(key=lambda w: (w["kind"] != "session", w["started_at"]))

            desks = [
                dict(self.desks[desk_id])
                for desk_id in self.desk_order
                if desk_id in self.desks
            ]
            for desk in desks:
                desk["seated"] = [
                    w["id"] for w in workers if w.get("desk") == desk["id"] and not w["gone"]
                ]

            return {
                "seq": self.seq,
                "now": now,
                "office": {
                    "name": self.office_name,
                    "gateway_base_url": self.gateway_base_url,
                    "roster_source": self.roster_source,
                    "problems": list(self.roster_problems),
                    "uptime_seconds": round(now - self.started_at, 1),
                },
                "desks": desks,
                "workers": workers,
                "totals": dict(self.totals),
                "by_origin": {k: dict(v) for k, v in self.by_origin.items()},
                "by_model": {k: dict(v) for k, v in self.by_model.items()},
                "waiting": sum(1 for w in workers if w.get("needs_input")),
                # แยกออกมาให้เห็นชัด ไม่ใช่แค่ตัวเลข · นี่คือสถานะเดียวที่ไม่มีอะไร
                # เดินต่อจนกว่าคนจะลงมือ คนเปิดหน้าจอมาควรเห็นก่อนอย่างอื่น
                "needs_you": [
                    {
                        "id": w["id"],
                        "desk": w.get("desk"),
                        "desk_label": w.get("desk_label"),
                        "platform": w.get("platform", ""),
                        "model": w.get("model", ""),
                        "text": w.get("awaiting_text", ""),
                        "waiting_seconds": round(now - w["awaiting_since"], 1)
                        if w.get("awaiting_since")
                        else None,
                    }
                    for w in workers
                    if w.get("needs_input")
                ],
            }
