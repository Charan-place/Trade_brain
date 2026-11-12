"""RemoteBroker: the Broker interface over signed HTTPS to the static-IP order server (used on Modal / locally).

TLS is pinned: the order server's own self-signed certificate (TBRAIN_ORDER_SERVER_CERT, PEM) is the only
certificate trusted. Any network problem becomes a BrokerError, so the trade book simply retries next cycle.
"""
from __future__ import annotations

import json
import ssl
import time

from tbrain.execution.broker import BrokerError, Order
from tbrain.orderserver import signing


class RemoteBroker:
    name = "groww"
    remote = True                    # tells the execution hub to run real orders on its own thread

    def __init__(self, url: str, secret: str, cert_pem: str | None = None, timeout: float = 10.0, client=None):
        import httpx
        self.url, self.secret = url.rstrip("/"), secret
        if client is None:
            verify = True
            if cert_pem:
                verify = ssl.create_default_context(cadata=cert_pem)
                verify.check_hostname = False            # cert is pinned; the VM is addressed by IP
            client = httpx.Client(timeout=timeout, verify=verify)
        self.http = client
        self._conn = (0.0, False)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        raw = json.dumps(body or {}, separators=(",", ":")).encode() if method == "POST" else b""
        h = signing.headers(self.secret, method, path, raw)
        try:
            r = self.http.request(method, self.url + path, content=raw if method == "POST" else None, headers=h)
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"order server unreachable: {type(e).__name__}") from None
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code >= 400:
            raise BrokerError(j.get("error") or f"order server HTTP {r.status_code}")
        return j

    @staticmethod
    def _order(j: dict) -> Order:
        return Order(**j["order"])

    # ---- session
    @property
    def connected(self) -> bool:
        t, v = self._conn
        if time.monotonic() - t < 5:
            return v
        try:
            v = bool(self._call("GET", "/status").get("connected"))
        except BrokerError:
            v = False
        self._conn = (time.monotonic(), v)
        return v

    def connect(self) -> dict:
        r = self._call("POST", "/session/connect")
        self._conn = (time.monotonic(), bool(r.get("connected")))
        return r

    def disconnect(self) -> None:
        self._call("POST", "/session/disconnect")
        self._conn = (time.monotonic(), False)

    # ---- broker interface
    def place(self, **kw) -> Order:
        return self._order(self._call("POST", "/broker/place", kw))

    def cancel(self, order_id: str) -> Order:
        return self._order(self._call("POST", "/broker/cancel", {"order_id": order_id}))

    def modify(self, order_id: str, *, price=None, trigger=None) -> Order:
        return self._order(self._call("POST", "/broker/modify", {"order_id": order_id, "price": price, "trigger": trigger}))

    def get(self, order_id: str) -> Order:
        return self._order(self._call("POST", "/broker/get", {"order_id": order_id}))

    def position(self, instrument: str) -> int:
        return int(self._call("POST", "/broker/position", {"instrument": instrument})["qty"])

    def funds(self):
        return self._call("POST", "/broker/funds")["funds"]

    def quote(self, instrument: str):
        return self._call("POST", "/broker/quote", {"instrument": instrument})["quote"]

    def supports(self, instrument: str) -> bool:
        try:
            return bool(self._call("POST", "/broker/supports", {"instrument": instrument})["ok"])
        except BrokerError:
            return False

    def matches(self, instrument: str, call: dict):
        keep = {k: call.get(k) for k in ("strike", "kind", "expiry", "lot")}
        try:
            r = self._call("POST", "/broker/matches", {"instrument": instrument, "call": keep})
        except BrokerError as e:
            return False, str(e)
        return bool(r.get("ok")), r.get("why")
