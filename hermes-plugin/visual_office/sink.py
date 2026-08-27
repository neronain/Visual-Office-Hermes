"""Fire-and-forget event relay to the Visual Office server.

The agent must never wait on the office. Hooks call :meth:`Sink.emit`, which
enqueues and returns; a single daemon thread drains the queue in FIFO order so
``subagent_start`` always reaches the server before the child's own events.
Every failure is swallowed — a stopped office server, a wrong token, or an
unreachable host must not change what Hermes does.

Discovery mirrors the pattern the Pixel Agents bridge established, because it
works: the server writes ``~/.hermes/visual-office/server.json`` on start and we
read ``{port, token}`` from it. ``VISUAL_OFFICE_URL`` / ``VISUAL_OFFICE_TOKEN``
override that for a server on another machine.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CONFIG_TTL_SECONDS = 5.0
_POST_TIMEOUT_SECONDS = 2.0
_QUEUE_MAX = 2000


def server_json_path() -> Path:
    """Where the office server publishes its port and token."""
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".hermes"
    return base / "visual-office" / "server.json"


class Sink:
    """Single-worker POST queue with cached server discovery."""

    def __init__(self, path: str = "/api/events") -> None:
        self._path = path
        self._q: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cfg: Optional[tuple[str, str]] = None
        self._cfg_at = 0.0

    # -- discovery ----------------------------------------------------------

    def resolve(self, force: bool = False) -> Optional[tuple[str, str]]:
        """Return ``(base_url, token)`` for the office, or None when it is down."""
        now = time.monotonic()
        if not force and self._cfg is not None and (now - self._cfg_at) < _CONFIG_TTL_SECONDS:
            return self._cfg

        env_url = os.environ.get("VISUAL_OFFICE_URL", "").strip()
        if env_url:
            self._cfg = (env_url.rstrip("/"), os.environ.get("VISUAL_OFFICE_TOKEN", "").strip())
            self._cfg_at = now
            return self._cfg

        try:
            data = json.loads(server_json_path().read_text(encoding="utf-8"))
            url = str(data.get("url") or f"http://127.0.0.1:{int(data['port'])}")
            self._cfg = (url.rstrip("/"), str(data.get("token", "")))
            self._cfg_at = now
            return self._cfg
        except Exception:
            self._cfg = None
            self._cfg_at = now
            return None

    # -- emit ---------------------------------------------------------------

    def emit(self, payload: dict[str, Any]) -> None:
        """Enqueue one event. Never raises; drops silently when the queue is full."""
        if self.resolve() is None:
            return
        self._ensure_worker()
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            pass

    def get_json(self, path: str, timeout: float = 5.0) -> Optional[dict]:
        """GET one JSON object from the office, or None. Never raises."""
        cfg = self.resolve()
        if cfg is None:
            return None
        url, token = cfg
        try:
            req = urllib.request.Request(
                f"{url}{path}", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 204:
                    return None
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else None
        except Exception as exc:
            logger.debug("visual_office get failed: %s", exc)
            return None

    def post_now(self, path: str, payload: dict[str, Any], timeout: float = 3.0) -> Optional[dict]:
        """Synchronous POST for the rare call that wants an answer (roster sync)."""
        cfg = self.resolve()
        if cfg is None:
            return None
        url, token = cfg
        try:
            req = urllib.request.Request(
                f"{url}{path}",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
        except Exception as exc:
            logger.debug("visual_office sync post failed: %s", exc)
            return None

    # -- worker -------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run, name="visual-office-sink", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            payload = self._q.get()
            if payload is None:
                return
            try:
                self._post(payload)
            except Exception as exc:  # pragma: no cover - best-effort relay
                logger.debug("visual_office post failed: %s", exc)
            finally:
                self._q.task_done()

    def _post(self, payload: dict[str, Any]) -> None:
        cfg = self.resolve()
        if cfg is None:
            return
        url, token = cfg
        req = urllib.request.Request(
            f"{url}{self._path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_SECONDS):
                pass
        except Exception:
            # The server may have restarted on a new port/token. Refresh the
            # discovery cache so the next event reconnects, then drop this one.
            self.resolve(force=True)
            raise
