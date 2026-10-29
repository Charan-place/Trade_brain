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


def offline_real_status(s, store) -> dict:
    """Real-money card data when the brain is not running (weekend, before 09:00): switch, cap, Groww connection."""
    from tbrain.execution.hub import CONFIRM_PHRASE
    r = store.get_real() if store else {}
    st = {"on": bool(r.get("on")), "why_off": r.get("why_off"), "connected": False, "broker": None, "configured": False,
          "loss_cap": float(r.get("loss_cap") or s.real_daily_loss_cap), "loss_cap_max": float(s.real_loss_cap_max),
          "pnl_today": 0.0, "confirm_each": bool(s.real_confirm_each), "lots": s.real_lots, "max_open": s.real_max_open,
          "confirm_phrase": CONFIRM_PHRASE, "brain_running": False, "note": None}
    try:
        rb = s.order_broker(timeout=3.0)
    except Exception as e:  # noqa: BLE001
        rb, st["note"] = None, f"order server settings are invalid: {e}"[:200]
    if rb is None:
        st["note"] = st["note"] or "order server is not set up yet (TBRAIN_ORDER_SERVER_URL / _TOKEN)"
        return st
    st["configured"], st["broker"] = True, "groww"
    try:
        st["connected"] = bool(rb._call("GET", "/status").get("connected"))  # direct call: keep the error text
    except Exception as e:  # noqa: BLE001
        st["note"] = f"order server not reachable: {e}"[:200]
    return st


def real_action(store, path: str, body: dict, max_cap: float) -> tuple[int, dict]:
    """Owner-only real-money actions, shared by the local server (the web app has its own auth around the same rules)."""
    from tbrain.execution.hub import CONFIRM_PHRASE
    if path == "/api/real":
        if "loss_cap" in body:
            try:
                cap = float(body["loss_cap"])
            except (TypeError, ValueError):
                return 400, {"error": "loss cap must be a number"}
            if not (0 < cap <= max_cap):
                return 400, {"error": f"loss cap must be between 1 and {max_cap:,.0f}"}
            store.set_real(loss_cap=cap)
        if "on" in body:
            if body["on"] is True:
                if str(body.get("confirm", "")).strip().lower() != CONFIRM_PHRASE:
                    return 400, {"error": f"type exactly: {CONFIRM_PHRASE}"}
                store.set_real(on=True, why_off=None)
            elif body["on"] is False:
                store.set_real(on=False, why_off="switched off by you")
            else:
                return 400, {"error": "on must be true or false"}
        return 200, {"real": store.get_real()}
    if path == "/api/real/exit":
        if body.get("all") is True:
            return 200, {"queued": store.push_command("exit_all")}
        if isinstance(body.get("call_id"), str) and body["call_id"]:
            return 200, {"queued": store.push_command("exit", body["call_id"])}
        return 400, {"error": "send {call_id} or {all: true}"}
    if path == "/api/real/connect":
        from tbrain.config import Settings
        rb = Settings.load().order_broker()
        if rb is None:
            return 400, {"error": "order server is not set up yet (TBRAIN_ORDER_SERVER_URL / _TOKEN)"}
        try:
            r = rb.connect()
        except Exception as e:  # noqa: BLE001
            return 502, {"error": str(e)[:300]}
        return 200, {"connected": bool(r.get("connected"))}
    if path == "/api/real/approve":
        if isinstance(body.get("call_id"), str) and body["call_id"]:
            return 200, {"queued": store.push_command("approve", body["call_id"])}
        return 400, {"error": "send {call_id}"}
    return 404, {"error": "not found"}


def make_handler(bc: Broadcaster, settings_store=None, history=None, max_cap: float = 10000.0):
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
                from urllib.parse import parse_qs, urlparse
                book = (parse_qs(urlparse(self.path).query).get("book") or ["signal"])[0]
                try:
                    if book not in ("signal", "paper", "real"):
                        raise ValueError("book must be signal, paper or real")
                    body = json.dumps(history(book) if history else {"days": [], "total": {}}, default=str).encode()
                    self._send(200, body, "application/json")
                except Exception as e:  # noqa: BLE001
                    self._send(503, json.dumps({"error": repr(e)}).encode(), "application/json")
            elif path == "/api/real":
                from tbrain.config import Settings
                body = json.dumps({"real": settings_store.get_real() if settings_store else {},
                                   "status": offline_real_status(Settings.load(), settings_store)}).encode()
                self._send(200, body, "application/json")
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
            if path not in ("/api/settings", "/api/real", "/api/real/exit", "/api/real/approve",
                            "/api/real/connect") or settings_store is None:
                self._send(404, b"not found", "text/plain"); return
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                self._send(415, b"json only", "text/plain"); return
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                self._send(403, b"bad origin", "text/plain"); return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("bad json")
            except (ValueError, OSError):
                self._send(400, b"bad request", "text/plain"); return
            if path == "/api/settings":
                cur = settings_store.update(body)
                self._send(200, json.dumps({"settings": cur, "can_edit": True}).encode(), "application/json"); return
            code, out = real_action(settings_store, path, body, max_cap)
            self._send(code, json.dumps(out).encode(), "application/json")

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
    def __init__(self, bc: Broadcaster, port: int = 8765, settings_store=None, history=None, max_cap: float = 10000.0):
        self.bc, self.port = bc, port
        self.srv = _QuietServer(("127.0.0.1", port), make_handler(bc, settings_store, history, max_cap))
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
        from tbrain.history import from_exec_files, from_files
        ui = UIServer(bc, port, FileSettings(s.data_dir / "live" / "settings.json"),
                      history=lambda book="signal": from_files(s.data_dir) if book == "signal" else from_exec_files(s.data_dir, book),
                      max_cap=s.real_loss_cap_max).start()
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
