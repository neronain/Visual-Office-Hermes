"""Desks that talk to their own endpoint instead of going through the gateway.

Hermes' public subagent API takes a model and nothing else: the child inherits
the parent's provider, base URL and key. That is fine while the gateway is up —
one alias per desk is what lets desks sit on different machines at all — but it
means every gateway desk dies together with the gateway.

A desk with its own ``base_url`` skips Hermes and speaks OpenAI-compatible HTTP
to that endpoint directly. It is a plain question-and-answer worker: no tools,
no session, no agent loop. It is also the only kind of desk that still answers
when the gateway is gone, which is the whole reason it exists.

The registry below mirrors just enough of ``subagent_lifecycle`` — launch,
status, wait, result, cancel — that ``office_delegate`` treats both kinds of
desk the same way and callers cannot tell which registry an id came from.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})

# ยาวกว่านี้ไม่ได้ช่วยอะไร — คำตอบที่ยาวกว่านี้ถูกตัดตอนแสดงในห้องอยู่แล้ว
SUMMARY_MAX_CHARS = 8000
CONNECT_TIMEOUT_SECONDS = 15.0


@dataclasses.dataclass
class DirectRun:
    """งานหนึ่งชิ้นที่โต๊ะยิงตรงกำลังทำ (หรือทำเสร็จแล้ว)"""

    run_id: str
    desk_id: str
    desk_label: str
    model: str
    base_url: str
    state: str = RUNNING
    summary: str = ""
    error: str = ""
    usage: dict[str, Any] = dataclasses.field(default_factory=dict)
    started_at: float = 0.0
    completed_at: Optional[float] = None
    _cancelled: bool = False
    # ห้องต้องได้เห็นคำตอบครั้งเดียว ไม่ว่าใครจะเป็นคนมาเจอว่างานจบก่อน —
    # ตัว wait, การ poll status, หรือเธรดที่เฝ้าอยู่เบื้องหลัง
    _reported: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def duration_seconds(self) -> Optional[float]:
        if not self.started_at or self.completed_at is None:
            return None
        return round(self.completed_at - self.started_at, 2)


_RUNS: dict[str, DirectRun] = {}
_LOCK = threading.Lock()
_DONE: dict[str, threading.Event] = {}


def _env_value(name: str) -> str:
    """คีย์จาก environment ก่อน แล้วค่อยจาก ~/.hermes/.env

    Hermes โหลด .env เข้า environment ตอนเริ่ม แต่การอ่านไฟล์เป็นทางสำรองทำให้
    ปลั๊กอินยังทำงานได้ไม่ว่าลำดับการเริ่มจะเป็นอย่างไร และเวลาทดสอบจาก python เปล่า ๆ
    """
    if not name:
        return ""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    from .gateway import config_path

    path = config_path().with_name(".env")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition("=")
            if key.strip() == name:
                return raw.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _post(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _answer_of(body: dict) -> tuple[str, str]:
    """``(answer, error)`` จากคำตอบแบบ OpenAI — reasoning ล้วนก็ยังดีกว่าค่าว่าง"""
    choices = body.get("choices") or []
    if not choices:
        return "", "endpoint returned no choices"
    message = (choices[0] or {}).get("message") or {}
    answer = str(message.get("content") or "").strip()
    if answer:
        return answer[:SUMMARY_MAX_CHARS], ""
    thinking = str(message.get("reasoning_content") or "").strip()
    if thinking:
        return thinking[:SUMMARY_MAX_CHARS], ""
    reason = str((choices[0] or {}).get("finish_reason") or "")
    if reason == "length":
        return "", "endpoint hit its output limit before writing an answer"
    return "", "endpoint returned an empty message"


def _run(run: DirectRun, goal: str, context: Optional[str], api_key: str,
         timeout: float, max_tokens: int) -> None:
    messages: list[dict[str, str]] = []
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": goal})
    payload = {"model": run.model, "messages": messages, "max_tokens": max_tokens}

    try:
        body = _post(f"{run.base_url}/chat/completions", payload, api_key, timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        _finish(run, FAILED, error=f"HTTP {exc.code} from {run.base_url}: {detail or exc.reason}")
        return
    except Exception as exc:
        _finish(run, FAILED, error=f"could not reach {run.base_url}: {exc}")
        return

    answer, problem = _answer_of(body)
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    if problem:
        _finish(run, FAILED, error=problem, usage=usage)
        return
    _finish(run, SUCCEEDED, summary=answer, usage=usage)


def _finish(run: DirectRun, state: str, summary: str = "", error: str = "",
            usage: Optional[dict] = None) -> None:
    with _LOCK:
        # ผู้ใช้กดยกเลิกระหว่างที่คำขอยังค้างอยู่ — ผลที่กลับมาทีหลังไม่ควรลบล้างการยกเลิก
        if run._cancelled and state != CANCELLED:
            done = _DONE.get(run.run_id)
            if done is not None:
                done.set()
            return
        run.state = state
        run.summary = summary
        run.error = error
        run.usage = dict(usage or {})
        run.completed_at = time.time()
        done = _DONE.get(run.run_id)
    if done is not None:
        done.set()


def launch(desk, goal: str, context: Optional[str], timeout: float,
           max_tokens: int = 4096) -> DirectRun:
    """เริ่มงานหนึ่งชิ้นที่โต๊ะยิงตรง แล้วคืน run ทันทีโดยไม่รอคำตอบ"""
    run = DirectRun(
        run_id=f"direct-{uuid.uuid4().hex[:12]}",
        desk_id=desk.id,
        desk_label=desk.label or desk.id,
        model=desk.model,
        base_url=desk.base_url,
        started_at=time.time(),
    )
    api_key = _env_value(desk.api_key_env)
    with _LOCK:
        _RUNS[run.run_id] = run
        _DONE[run.run_id] = threading.Event()
    thread = threading.Thread(
        target=_run,
        args=(run, goal, context, api_key, max(timeout, CONNECT_TIMEOUT_SECONDS), max_tokens),
        name=f"visual-office-direct-{desk.id}",
        daemon=True,
    )
    thread.start()
    return run


def get(run_id: str) -> Optional[DirectRun]:
    with _LOCK:
        return _RUNS.get(run_id)


def wait(run_id: str, timeout_seconds: float) -> Optional[DirectRun]:
    """รอจนจบหรือหมดเวลา — คืน run เสมอ ให้ผู้เรียกดู state เอง"""
    with _LOCK:
        run = _RUNS.get(run_id)
        done = _DONE.get(run_id)
    if run is None:
        return None
    if done is not None:
        done.wait(timeout=timeout_seconds)
    return run


def cancel(run_id: str, reason: str = "") -> Optional[DirectRun]:
    """เลิกสนใจคำตอบของงานนี้

    คำขอ HTTP ที่ยิงออกไปแล้วหยุดกลางคันไม่ได้ — สิ่งที่ทำได้คือไม่รับผลของมัน
    แล้วปล่อยให้ thread จบไปเงียบ ๆ · บอกตามนั้นดีกว่าอ้างว่าหยุดได้
    """
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return None
        if not run.terminal:
            run._cancelled = True
            run.state = CANCELLED
            run.error = reason or "cancelled from the office"
            run.completed_at = time.time()
        done = _DONE.get(run_id)
    if done is not None:
        done.set()
    return run


def running() -> list[DirectRun]:
    with _LOCK:
        return [r for r in _RUNS.values() if not r.terminal]


def forget_finished(keep_seconds: float = 3600.0) -> None:
    """ทิ้งงานที่จบไปนานแล้ว — ทะเบียนนี้อยู่ในหน่วยความจำของโปรเซส Hermes"""
    cutoff = time.time() - keep_seconds
    with _LOCK:
        stale = [
            run_id for run_id, run in _RUNS.items()
            if run.terminal and (run.completed_at or 0) < cutoff
        ]
        for run_id in stale:
            _RUNS.pop(run_id, None)
            _DONE.pop(run_id, None)
