"""Upstox Market Data Feed V3 client with TLS verification ON.

Why not the SDK's MarketDataStreamerV3 directly: in upstox-python-sdk 2.30.0 its feeder connects with
ssl.CERT_NONE / check_hostname=False and its reconnect gives up after a few tries. We reuse only the SDK's
protobuf schema (MarketDataFeedV3_pb2) and the same endpoint + request format:
  wss://api.upstox.com/v3/feed/market-data-feed, header Authorization: Bearer <token>,
  binary JSON {"guid","method":"sub|unsub|change_mode","data":{"mode","instrumentKeys"}}.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import uuid
from typing import Callable

import websocket
from google.protobuf import json_format

from tbrain.brokers.upstox.http import USER_AGENT
from tbrain.timeutil import in_market_hours

try:
    from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as _pb
except Exception as e:  # pragma: no cover
    raise ImportError("upstox-python-sdk is required for the V3 protobuf schema: uv sync") from e

FEED_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
MODES = ("ltpc", "full", "option_greeks", "full_d30")
CHUNK = 100


def decode_frame(data: bytes) -> dict:
    return json_format.MessageToDict(_pb.FeedResponse.FromString(data))


def build_request(method: str, keys: list[str], mode: str | None = None) -> bytes:
    req = {"guid": uuid.uuid4().hex, "method": method, "data": {"instrumentKeys": keys}}
    if mode:
        req["data"]["mode"] = mode
    return json.dumps(req).encode()


class LiveFeed:
    def __init__(self, token_provider: Callable[[], str], on_message: Callable[[dict, int], None],
                 on_state: Callable[[str, str], None] | None = None, tls_verify: bool = True, stale_s: float = 30.0):
        self._token_provider = token_provider
        self._on_message = on_message
        self._on_state = on_state or (lambda ev, detail: None)
        self._tls_verify = tls_verify
        self._stale_s = stale_s
        self._subs: dict[str, set[str]] = {m: set() for m in MODES}
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.last_msg_ns = 0
        self.opened_ns = 0
        self.reconnects = 0
        self.decode_errors = 0
        self.callback_errors = 0
        self.fatal: str | None = None

    # ---- public -------------------------------------------------------
    def subscribe(self, keys: list[str], mode: str = "full") -> list[str]:
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            new = [k for k in dict.fromkeys(keys) if k not in self._subs[mode]]
            self._subs[mode].update(new)
        if new and self._connected.is_set():
            self._send("sub", new, mode)
        return new

    def subscribed(self) -> set[str]:
        with self._lock:
            return set().union(*self._subs.values())

    def start(self) -> None:
        for target in (self._run_loop, self._watchdog):
            t = threading.Thread(target=target, daemon=True, name=f"feed-{target.__name__}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws:
            ws.close()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    # ---- internals ----------------------------------------------------
    def _send(self, method: str, keys: list[str], mode: str | None) -> None:
        ws = self._ws
        if not ws:
            return
        for i in range(0, len(keys), CHUNK):
            try:
                ws.send(build_request(method, keys[i:i + CHUNK], mode), opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                self._on_state("send_error", str(e))

    def _handle_open(self, ws) -> None:
        self.opened_ns = time.time_ns()
        self._connected.set()
        self._on_state("open", "")
        with self._lock:
            snapshot = {m: sorted(k) for m, k in self._subs.items() if k}
        for mode, keys in snapshot.items():
            self._send("sub", keys, mode)

    def _handle_message(self, ws, data) -> None:
        recv_ns = time.time_ns()
        self.last_msg_ns = recv_ns
        if not isinstance(data, (bytes, bytearray)):
            return
        try:
            msg = decode_frame(bytes(data))
        except Exception:
            self.decode_errors += 1
            return
        try:
            self._on_message(msg, recv_ns)
        except Exception as e:
            self.callback_errors += 1
            self._on_state("callback_error", repr(e))

    def _handle_error(self, ws, err) -> None:
        text = str(err)
        if "401" in text or "403" in text:
            self.fatal = "auth"
            self._on_state("auth_failed", "Token rejected. Run: uv run tbrain login")
            self._stop.set()
        else:
            self._on_state("error", text[:300])

    def _handle_close(self, ws, code, reason) -> None:
        self._connected.clear()
        self._on_state("close", f"{code} {reason or ''}".strip())

    def _run_loop(self) -> None:
        backoff = 1.0
        sslopt = {"cert_reqs": ssl.CERT_REQUIRED} if self._tls_verify else {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        while not self._stop.is_set():
            try:
                token = self._token_provider()
            except Exception as e:
                self.fatal = "auth"
                self._on_state("auth_failed", str(e))
                return
            self._ws = websocket.WebSocketApp(
                FEED_URL, header={"Authorization": f"Bearer {token}", "Accept": "*/*", "User-Agent": USER_AGENT},
                on_open=self._handle_open, on_message=self._handle_message,
                on_error=self._handle_error, on_close=self._handle_close)
            t0 = time.time()
            try:
                self._ws.run_forever(sslopt=sslopt)
            except Exception as e:
                self._on_state("error", repr(e)[:300])
            self._connected.clear()
            if self._stop.is_set() or self.fatal:
                break
            backoff = 1.0 if time.time() - t0 > 60 else min(backoff * 2, 30.0)
            self.reconnects += 1
            self._on_state("reconnecting", f"in {backoff:.0f}s (#{self.reconnects})")
            self._stop.wait(backoff)

    def _watchdog(self) -> None:
        while not self._stop.wait(5):
            if not self._connected.is_set() or not in_market_hours():
                continue
            ref = max(self.last_msg_ns, self.opened_ns)
            if ref and (time.time_ns() - ref) / 1e9 > self._stale_s:
                self._on_state("stale", f"no message for >{self._stale_s}s, forcing reconnect")
                ws = self._ws
                if ws:
                    ws.close()
