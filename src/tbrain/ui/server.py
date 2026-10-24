"""Brain UI server (127.0.0.1 only).

GET /            -> page
GET /api/state   -> latest snapshot (JSON)
GET /api/stream  -> Server-Sent Events: a new `data:` frame each time a snapshot is published

Two ways to run:
- Embedded in `tbrain record` (default): snapshots come straight from the recorder's memory every 250 ms (push).
- Standalone `tbrain ui`: snapshots come from data/live/state.json, the last session, or a probe replay (1 s).
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from tbrain.brain.demo import latest_probe, snapshot_from_probe
from tbrain.config import Settings

STATIC = Path(__file__).parent / "static"
FRESH_S = 10
HEARTBEAT_S = 15


class Broadcaster:
    """Latest-value channel: publishers overwrite, every subscriber gets the newest frame (slow clients skip frames)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._seq = 0
        self._payload: bytes = b""
        self.closed = False

    def publish(self, state: dict) -> None:
        body = json.dumps(state, default=str, separators=(",", ":")).encode()
        with self._cond:
            self._seq += 1
            self._payload = body
            self._cond.notify_all()

    def latest(self) -> tuple[int, bytes]:
        with self._cond:
            return self._seq, self._payload

    def wait_newer(self, seq: int, timeout: float) -> tuple[int, bytes]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq or self.closed, timeout=timeout)
            return self._seq, self._payload

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def current_state(s: Settings, force_demo: bool = False) -> dict:
    live = s.data_dir / "live" / "state.json"
    if live.exists() and not force_demo:
        try:
            st = json.loads(live.read_text())
            age = time.time() - live.stat().st_mtime
            if age > FRESH_S:
                st["mode"] = "stale"
                st.setdefault("feed", {})["note"] = f"Recorder not running; last state is {age/60:.0f} min old"
            return st
        except (OSError, json.JSONDecodeError):
            pass
    probe = latest_probe(s.data_dir)
    if probe:
        return snapshot_from_probe(probe, s.data_dir, s.risk_free, s.vix_key)
    return {"mode": "empty", "underlyings": {}, "brain": [], "feed": {"note": "No data yet: run `tbrain probe` or `tbrain record`"},
            "signals": {"status": "observing", "calls": [], "reason": "No data yet", "scoreboard": {"calls": 0}}}


def make_handler(bc: Broadcaster, settings_store=None, history=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            seq, payload = bc.latest()
            try:
                if payload:
                    self.wfile.write(b"data: " + payload + b"\n\n"); self.wfile.flush()
                last_beat = time.monotonic()
                while not bc.closed:
                    new_seq, payload = bc.wait_newer(seq, timeout=1.0)
                    if new_seq != seq and payload:
                        seq = new_seq
                        self.wfile.write(b"data: " + payload + b"\n\n"); self.wfile.flush()
                        last_beat = time.monotonic()
                    elif time.monotonic() - last_beat > HEARTBEAT_S:
                        self.wfile.write(b": keep-alive\n\n"); self.wfile.flush()
                        last_beat = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # browser tab closed

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                _, payload = bc.latest()
                self._send(200, payload or b"{}", "application/json")
            elif path == "/api/stream":
                self.close_connection = True
                self._stream()
            elif path == "/api/history":
                try:
                    body = json.dumps(history() if history else {"days": [], "total": {}}, default=str).encode()
                    self._send(200, body, "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(503, json.dumps({"error": repr(e)}).encode(), "application/json")
            elif path == "/api/settings":
                body = json.dumps({"settings": settings_store.get() if settings_store else {},
                                   "can_edit": settings_store is not None}).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            # Dashboard switches. CSRF guard for a localhost server: JSON body only (a cross-site <form> cannot send
            # it without a CORS preflight, which we never answer) and, when present, the Origin must be this server.
            path = self.path.split("?", 1)[0]
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if path != "/api/settings" or settings_store is None:
                self._send(404, b"not found", "text/plain"); return
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._send(415, b"json only", "text/plain"); return
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                self._send(403, b"bad origin", "text/plain"); return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                changes = json.loads(self.rfile.read(n) or b"{}")
                cur = settings_store.update(changes if isinstance(changes, dict) else {})
            except (ValueError, OSError):
                self._send(400, b"bad request", "text/plain"); return
            self._send(200, json.dumps({"settings": cur, "can_edit": True}).encode(), "application/json")

        def log_message(self, *a):
            pass

    return Handler


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # closed browser tabs are normal, not errors
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class UIServer:
    def __init__(self, bc: Broadcaster, port: int = 8765, settings_store=None, history=None):
        self.bc, self.port = bc, port
        self.srv = _QuietServer(("127.0.0.1", port), make_handler(bc, settings_store, history))
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{port}/"
        self._t: threading.Thread | None = None

    def start(self) -> "UIServer":
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True, name="ui-http")
        self._t.start()
        return self

    def stop(self) -> None:
        self.bc.close()
        self.srv.shutdown()
        self.srv.server_close()


def serve(s: Settings, port: int = 8765, force_demo: bool = False, open_browser: bool = True,
          provider: Callable[[], dict] | None = None) -> None:
    """Standalone mode: republish the file/demo state once a second."""
    bc = Broadcaster()
    provider = provider or (lambda: current_state(s, force_demo))
    try:
        from tbrain.runtime_settings import FileSettings
        from tbrain.history import from_files
        ui = UIServer(bc, port, FileSettings(s.data_dir / "live" / "settings.json"),
                      history=lambda: from_files(s.data_dir)).start()
    except OSError as e:
        print(f"Port {port} busy ({e}). If `tbrain record` is running, its UI is already at http://127.0.0.1:{port}/")
        return
    print(f"Brain UI on {ui.url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(ui.url)
    try:
        while True:
            try:
                bc.publish(provider())
            except Exception as e:  # keep serving even if one snapshot fails
                bc.publish({"mode": "error", "feed": {"note": repr(e)}, "underlyings": {}, "brain": [], "signals": {}})
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        ui.stop()
