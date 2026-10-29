"""Request signing between the brain (Modal) and the order server (VM).

Every request carries X-TB-Ts (epoch ms), X-TB-Nonce and X-TB-Sig = HMAC-SHA256(secret, ts \\n nonce \\n METHOD \\n path \\n body).
The server rejects stale (> 30 s), replayed (seen nonce) or wrongly signed requests.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

MAX_SKEW_MS = 30_000


def sign(secret: str, method: str, path: str, body: bytes, ts: str, nonce: str) -> str:
    msg = b"\n".join([ts.encode(), nonce.encode(), method.upper().encode(), path.encode(), body or b""])
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def headers(secret: str, method: str, path: str, body: bytes) -> dict:
    ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(12)
    return {"X-TB-Ts": ts, "X-TB-Nonce": nonce, "X-TB-Sig": sign(secret, method, path, body, ts, nonce),
            "Content-Type": "application/json"}


class Verifier:
    def __init__(self, secret: str):
        if not secret or len(secret) < 32:
            raise RuntimeError("TBRAIN_ORDER_SERVER_TOKEN must be at least 32 characters")
        self.secret = secret
        self._seen: dict[str, float] = {}

    def check(self, method: str, path: str, body: bytes, ts: str | None, nonce: str | None, sig: str | None,
              now_ms: float | None = None) -> str | None:
        """None if OK, else the reason it was refused."""
        now_ms = now_ms or time.time() * 1000
        if not (ts and nonce and sig):
            return "missing signature"
        try:
            t = int(ts)
        except ValueError:
            return "bad timestamp"
        if abs(now_ms - t) > MAX_SKEW_MS:
            return "stale request"
        if not hmac.compare_digest(sign(self.secret, method, path, body, ts, nonce), sig):
            return "bad signature"
        for k, v in list(self._seen.items()):
            if now_ms - v > 2 * MAX_SKEW_MS:
                del self._seen[k]
        if nonce in self._seen:
            return "replayed request"
        self._seen[nonce] = now_ms
        return None
