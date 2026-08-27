#!/usr/bin/env python3
"""Visual Office server — collects Hermes events and serves the office.

Standard library only, on purpose. The plugin side has to live inside Hermes'
virtualenv and the server side has to run on whatever machine has a screen; a
build step or a node_modules tree would make both harder than the thing is worth.

Routes
  POST /api/events    ingest one event (Bearer token)
  GET  /api/state     the folded office snapshot
  GET  /api/stream    the same snapshot, pushed over SSE
  GET  /healthz       liveness
  GET  /              the office itself

Writes ``<state-dir>/server.json`` on start so the plugin can find the port and
token without being configured. Reads are open (this is a monitor); writes need
the token. The default bind is 127.0.0.1 — pass --host 0.0.0.0 only when you
have decided that everyone who can reach the port may read the goals on screen.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from state import Office  # noqa: E402

MAX_EVENT_BYTES = 256 * 1024
LOG_TRIM_BYTES = 2 * 1024 * 1024
LOG_KEEP_BYTES = 512 * 1024
STREAM_TICK_SECONDS = 0.4
STREAM_PING_SECONDS = 15.0

WEB_DIR = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def default_state_dir() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".hermes"
    return base / "visual-office"


class EventLog:
    """Append-only JSONL log with a size cap. Replayed on start."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def replay(self, office: Office) -> int:
        if not self.path.exists():
            return 0
        count = 0
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        office.apply(json.loads(line))
                        count += 1
                    except Exception:
                        continue
        except OSError:
            return count
        return count

    def append(self, event: dict) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                if self.path.stat().st_size > LOG_TRIM_BYTES:
                    self._trim()
            except OSError:
                pass

    def _trim(self) -> None:
        try:
            data = self.path.read_bytes()[-LOG_KEEP_BYTES:]
            newline = data.find(b"\n")
            if newline != -1:
                data = data[newline + 1 :]
            self.path.write_bytes(data)
        except OSError:
            pass


class Server:
    """Everything the request handler needs, in one place."""

    def __init__(self, token: str, office: Office, log: EventLog, quiet: bool) -> None:
        self.token = token
        self.office = office
        self.log = log
        self.quiet = quiet


class Handler(BaseHTTPRequestHandler):
    server_version = "VisualOffice/0.1"
    app: Server

    # -- plumbing -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if not self.app.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload: dict) -> None:
        self._send(
            code,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _authorized(self) -> bool:
        if not self.app.token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:].strip(), self.app.token)
        return False

    # -- routes -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/events":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "bad or missing bearer token"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_EVENT_BYTES:
            self._json(413, {"error": f"body must be 1..{MAX_EVENT_BYTES} bytes"})
            return

        try:
            event = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._json(400, {"error": f"malformed JSON: {exc}"})
            return
        if not isinstance(event, dict) or not event.get("event"):
            self._json(400, {"error": "event must be an object with an 'event' field"})
            return

        self.app.office.apply(event)
        self.app.log.append(event)
        self._json(202, {"ok": True, "seq": self.app.office.seq})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health"):
            self._json(200, {"status": "ok", "product": "Visual Office", "version": "0.1.0"})
        elif path == "/api/state":
            self._json(200, self.app.office.snapshot())
        elif path == "/api/stream":
            self._stream()
        elif path in ("/", "/index.html"):
            self._static("index.html")
        elif path.lstrip("/") in {p.name for p in WEB_DIR.glob("*") if p.is_file()}:
            self._static(path.lstrip("/"))
        else:
            self._json(404, {"error": "not found"})

    def _static(self, name: str) -> None:
        target = (WEB_DIR / name).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._json(404, {"error": f"{name} is missing from {WEB_DIR}"})
            return
        self._send(
            200,
            target.read_bytes(),
            CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_seq = -1
        last_ping = time.monotonic()
        try:
            while True:
                snapshot = self.app.office.snapshot()
                now = time.monotonic()
                if snapshot["seq"] != last_seq:
                    last_seq = snapshot["seq"]
                    body = json.dumps(snapshot, ensure_ascii=False)
                    self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_ping = now
                elif (now - last_ping) > STREAM_PING_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                time.sleep(STREAM_TICK_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def write_discovery(state_dir: Path, host: str, port: int, token: str, url: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "server.json"
    payload = {
        "url": url,
        "host": host,
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started_at": time.time(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visual Office server for Hermes Agent")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8130, help="bind port (default 8130)")
    parser.add_argument(
        "--advertise",
        default="",
        help="URL the plugin should post to when it differs from the bind address "
        "(e.g. http://192.168.1.10:8130 for a server the agent reaches over the network)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("VISUAL_OFFICE_TOKEN", ""),
        help="shared secret for POST /api/events (generated when omitted)",
    )
    parser.add_argument("--state-dir", default="", help="where server.json and events.jsonl live")
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="do not write server.json (use VISUAL_OFFICE_URL/TOKEN on the agent instead)",
    )
    parser.add_argument("--no-replay", action="store_true", help="start with an empty office")
    parser.add_argument("--quiet", action="store_true", help="do not log every request")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    token = args.token.strip() or secrets.token_urlsafe(24)

    office = Office()
    log = EventLog(state_dir / "events.jsonl")
    replayed = 0 if args.no_replay else log.replay(office)

    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = args.advertise.strip().rstrip("/") or f"http://{display_host}:{args.port}"

    Handler.app = Server(token=token, office=office, log=log, quiet=args.quiet)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    discovery = None
    if not args.no_discovery:
        discovery = write_discovery(state_dir, args.host, args.port, token, url)

    print(f"Visual Office listening on http://{args.host}:{args.port}")
    print(f"  open        {url}")
    print(f"  state dir   {state_dir}")
    print(f"  events      {log.path} ({replayed} replayed)")
    if discovery:
        print(f"  discovery   {discovery}")
    else:
        print(f"  discovery   disabled — set VISUAL_OFFICE_URL={url} and VISUAL_OFFICE_TOKEN=<token>")
    print(f"  token       {token}")
    if args.host in ("0.0.0.0", "::"):
        print("  NOTE: bound to every interface — anyone who can reach this port can read the office.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nVisual Office stopped.")
    finally:
        httpd.server_close()
        if discovery:
            try:
                discovery.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
