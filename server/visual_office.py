#!/usr/bin/env python3
"""Visual Office server — collects Hermes events and serves the office.

Standard library only, on purpose. The plugin side has to live inside Hermes'
virtualenv and the server side has to run on whatever machine has a screen; a
build step or a node_modules tree would make both harder than the thing is worth.

Routes
  POST /api/events    ingest one event (Bearer token)
  POST /api/command   queue a task typed into the office page (Bearer token)
  GET  /api/command/next    the plugin pulls one queued task (Bearer token)
  GET  /api/command/log     what was sent and how it went
  GET  /api/said            recent replies and approval questions (memory only)
POST /api/said/clear      empty that list (Bearer token)
  GET  /api/desks     the desk roster; PUT to rewrite it (Bearer token)
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

import roster_file  # noqa: E402
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
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
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
        # The roster this server last wrote. The office snapshot only updates
        # when the plugin announces, which is when Hermes next runs — so between
        # a save and the next turn the snapshot is behind the file, and serving
        # it back to the editor would drop desks the user just added.
        self.written: dict | None = None
        self.written_at = 0.0
        # Commands typed into the office page, waiting for the plugin to pull
        # them. The server cannot talk to Hermes; the plugin polls.
        self.commands: list[dict] = []
        self.command_log: list[dict] = []
        self.command_seq = 0
        self.command_lock = threading.Lock()


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
        extra = dict(extra or {})
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for key, value in extra.items():
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

    def _handle_command_post(self) -> None:
        body = self._read_json()
        if body is None:
            return
        text = str(body.get("text") or "").strip()
        desk = str(body.get("desk") or "").strip()
        if not text:
            self._json(400, {"error": "ต้องมีข้อความสั่งงาน"})
            return
        if len(text) > 4000:
            self._json(400, {"error": "ข้อความยาวเกิน 4000 ตัวอักษร"})
            return

        with self.app.command_lock:
            self.app.command_seq += 1
            command = {
                "id": self.app.command_seq,
                "text": text,
                "desk": desk,
                "queued_at": time.time(),
                "state": "queued",
            }
            self.app.commands.append(command)
            self.app.command_log.append(dict(command))
            del self.app.command_log[:-20]
        self._json(202, {"ok": True, "id": command["id"], "queued": len(self.app.commands)})

    def _handle_command_next(self) -> None:
        with self.app.command_lock:
            command = self.app.commands.pop(0) if self.app.commands else None
            if command:
                command["state"] = "sent"
                for row in self.app.command_log:
                    if row["id"] == command["id"]:
                        row["state"] = "sent"
        if command is None:
            self._send(204, b"", "application/json; charset=utf-8")
            return
        self._json(200, command)

    def _handle_command_result(self) -> None:
        body = self._read_json()
        if body is None:
            return
        with self.app.command_lock:
            for row in self.app.command_log:
                if row["id"] == body.get("id"):
                    row["state"] = "done" if body.get("ok") else "failed"
                    row["error"] = str(body.get("error") or "")
                    row["platform"] = str(body.get("platform") or "")
                    row["done_at"] = time.time()
        self._json(200, {"ok": True})

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/desks":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "bad or missing bearer token"})
            return
        body = self._read_json()
        if body is None:
            return
        try:
            roster = roster_file.normalize(body)
        except roster_file.RosterError as exc:
            self._json(400, {"error": str(exc)})
            return

        target, problem = self._roster_target()
        if target is None:
            self._json(409, {"error": problem})
            return

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # One backup, overwritten each save. Enough to undo the mistake
                # you just made, which is the mistake people actually make.
                target.with_suffix(target.suffix + ".bak").write_bytes(target.read_bytes())
            target.write_text(roster_file.dump(roster, stamp), encoding="utf-8")
        except OSError as exc:
            self._json(500, {"error": f"เขียนไฟล์ไม่ได้: {exc}"})
            return

        self.app.written = roster
        self.app.written_at = time.time()
        self._json(200, {
            "ok": True,
            "path": str(target),
            "desks": len(roster["desks"]),
            "saved_at": stamp,
            "note": "Hermes จะอ่านไฟล์ใหม่เองในข้อความถัดไป ไม่ต้อง restart",
        })

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/api/events", "/api/command", "/api/command/result",
                        "/api/said/clear"):
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "bad or missing bearer token"})
            return
        if path == "/api/said/clear":
            self._json(200, {"ok": True, "cleared": self.app.office.forget_said()})
            return
        if path == "/api/command":
            self._handle_command_post()
            return
        if path == "/api/command/result":
            self._handle_command_result()
            return

        event = self._read_json()
        if event is None:
            return
        if not event.get("event"):
            self._json(400, {"error": "event must be an object with an 'event' field"})
            return

        self.app.office.apply(event)
        # Replies are held in memory only. The event log is the office's state,
        # not a chat archive — putting what people said on disk is a different
        # promise than the one this thing makes.
        if event.get("event") != "reply":
            self.app.log.append(event)
        self._json(202, {"ok": True, "seq": self.app.office.seq})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health"):
            self._json(200, {"status": "ok", "product": "Visual Office", "version": "0.1.0"})
        elif path == "/api/state":
            self._json(200, self.app.office.snapshot())
        elif path == "/api/desks":
            self._json(200, self._desks_payload())
        elif path == "/api/command/next":
            if not self._authorized():
                self._json(401, {"error": "bad or missing bearer token"})
                return
            self._handle_command_next()
        elif path == "/api/said":
            self._json(200, {"said": self.app.office.said()})
        elif path == "/api/command/log":
            with self.app.command_lock:
                self._json(200, {"commands": list(reversed(self.app.command_log))})
        elif path == "/api/token":
            # The desk editor needs the write token. Hand it over only to a
            # client on this machine — anyone else has to have been told it.
            host = (self.client_address or ("",))[0]
            if host in ("127.0.0.1", "::1", "localhost"):
                self._json(200, {"token": self.app.token})
            else:
                self._json(403, {"error": "token is only served to localhost"})
        elif path == "/api/stream":
            self._stream()
        elif path in ("/", "/index.html"):
            self._static("index.html")
        else:
            self._static(path.lstrip("/"))

    def _read_json(self) -> dict | None:
        """Read a JSON object body, or answer the client and return None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_EVENT_BYTES:
            self._json(413, {"error": f"body must be 1..{MAX_EVENT_BYTES} bytes"})
            return None
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self._json(400, {"error": f"malformed JSON: {exc}"})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return body

    def _roster_target(self) -> tuple[Path | None, str]:
        """The desks.yaml this server may write, or why it may not.

        The plugin reports the path it reads. If that path is not on this
        machine, editing here would write a file Hermes never opens — so refuse
        and say where the real one is, rather than saving into the void.
        """
        source = (self.app.office.roster_source or "").strip()
        if source:
            target = Path(source)
            if target.parent.is_dir():
                return target, ""
            return None, (
                f"Hermes อ่านรายชื่อโต๊ะจาก {source} ซึ่งไม่ได้อยู่บนเครื่องนี้ — "
                "แก้จากหน้าเว็บได้เฉพาะตอนที่เซิร์ฟเวอร์ห้องรันเครื่องเดียวกับ Hermes"
            )
        fallback = default_state_dir() / "desks.yaml"
        if fallback.parent.is_dir():
            return fallback, ""
        return None, (
            "ยังไม่เคยได้รับรายชื่อโต๊ะจากปลั๊กอิน และไม่พบโฟลเดอร์ "
            f"{fallback.parent} — เริ่ม session ของ Hermes สักครั้งก่อน"
        )

    def _desks_payload(self) -> dict:
        snapshot = self.app.office.snapshot()
        target, problem = self._roster_target()

        announced = self.app.office.roster_at
        if self.app.written and self.app.written_at > announced:
            office_name = self.app.written["office_name"]
            gateway = self.app.written["gateway_base_url"]
            desks = [dict(d) for d in self.app.written["desks"]]
            known_at = self.app.written_at
        else:
            office_name = snapshot["office"]["name"]
            gateway = snapshot["office"]["gateway_base_url"]
            desks = [
                {
                    "id": d["id"], "label": d["label"], "model": d["model"],
                    "origin": d["origin"], "provider": d.get("provider", ""),
                    "note": d.get("note", ""), "role": d.get("role", "leaf"),
                    "toolsets": list(d.get("toolsets") or []),
                    # ตกสองช่องนี้ = หน้าจัดการโต๊ะเห็นทุกโต๊ะเป็น "ผ่าน gateway" แล้วการ
                    # กดบันทึกครั้งถัดไปก็ลบ endpoint ของโต๊ะที่ยิงตรงทิ้งไปเงียบ ๆ
                    "base_url": d.get("base_url", ""),
                    "api_key_env": d.get("api_key_env", ""),
                }
                for d in snapshot["desks"]
            ]
            known_at = announced

        # Somebody may have edited the file by hand. We cannot read YAML here, so
        # say so rather than quietly overwriting their work on the next save.
        stale = ""
        try:
            if target and target.exists() and target.stat().st_mtime > known_at + 2:
                stale = (
                    f"ไฟล์ {target} ถูกแก้จากที่อื่นหลังจากรายชื่อชุดนี้ — "
                    "เริ่ม session ของ Hermes สักครั้งให้มันประกาศรายชื่อใหม่ก่อนกดบันทึก "
                    "ไม่อย่างนั้นการบันทึกจะทับของที่แก้ไว้"
                )
        except OSError:
            pass

        return {
            "office": {"name": office_name},
            "gateway": {"base_url": gateway},
            "desks": desks,
            "stale": stale,
            # What the gateway says each model can do. Each model is configured
            # separately there — one entry per model, its own endpoint and its own
            # measured capabilities — so the editor shows that rather than making
            # the desks look interchangeable.
            "available_models": self.app.office.available_models,
            "agent_model": self.app.office.agent_model,
            "main_model": self.app.office.main_model,
            "known_models": sorted(
                name for name in snapshot["by_model"] if name and name != "unknown"
            ),
            "path": str(target) if target else "",
            "writable": target is not None,
            "problem": problem,
        }

    def _static(self, name: str) -> None:
        """Serve one file from web/, subdirectories included.

        The art lives under web/assets/**, so this cannot be a flat listing.
        Resolve first and confirm the result is still inside web/ — that check
        is what stops ``../`` from walking out, not any pattern on the name.
        """
        if not name or name.startswith("/"):
            self._json(404, {"error": "not found"})
            return
        target = (WEB_DIR / name).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._json(404, {"error": f"{name} is missing from {WEB_DIR}"})
            return
        cache = "public, max-age=3600" if "/assets/" in f"/{name}" else "no-store"
        self._send(
            200,
            target.read_bytes(),
            CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
            {"Cache-Control": cache},
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


def reuse_token(state_dir: Path) -> str:
    """Keep the same token across restarts.

    A fresh token every start means every browser that is not on this machine
    has to be told the new one again — for a monitor that gets restarted
    whenever it is updated, that is a lot of friction for no gain. The file is
    0600 and the token is still required to write.
    """
    try:
        data = json.loads((state_dir / "server.json").read_text(encoding="utf-8"))
        token = str(data.get("token") or "").strip()
        return token if len(token) >= 16 else ""
    except Exception:
        return ""


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
    token = args.token.strip() or reuse_token(state_dir) or secrets.token_urlsafe(24)

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
        # The discovery file is deliberately left in place: it carries the token
        # that browsers have already been given, and the plugin treats a
        # connection refusal as "office is down" anyway.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
