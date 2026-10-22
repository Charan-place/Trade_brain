"""Per-user Upstox access tokens, encrypted at rest (Fernet / AES-128-CBC + HMAC) in MongoDB.

The encryption key (TBRAIN_ENCRYPTION_KEY) lives only in the deployment's secrets, never in the database,
so a leaked database dump does not leak anyone's Upstox session. Tokens expire daily anyway.
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

from tbrain.timeutil import IST, now_ist

UPSTOX_RESET = dtime(3, 30)  # Upstox access tokens stop working around 03:30 IST the next day


def session_start(now: datetime | None = None) -> datetime:
    """Start of the current Upstox token 'day' (03:30 IST)."""
    now = now or now_ist()
    start = datetime.combine(now.date(), UPSTOX_RESET, tzinfo=IST)
    return start if now >= start else start - timedelta(days=1)


class TokenStore:
    def __init__(self, db, key: str):
        if not key:
            raise RuntimeError("TBRAIN_ENCRYPTION_KEY is not set")
        from cryptography.fernet import Fernet
        self.f = Fernet(key.encode() if isinstance(key, str) else key)
        self.c = db["upstox_tokens"]

    def save(self, email: str, access_token: str, now: datetime | None = None) -> None:
        now = now or now_ist()
        self.c.update_one({"_id": email.lower()},
                          {"$set": {"enc": self.f.encrypt(access_token.encode()).decode(),
                                    "obtained_at": now.astimezone(timezone.utc)}}, upsert=True)

    def load(self, email: str, now: datetime | None = None) -> str | None:
        """Today's token for this user, or None if missing / from a previous token-day / undecryptable."""
        doc = self.c.find_one({"_id": email.lower()})
        if not doc:
            return None
        got = doc.get("obtained_at")
        if got is None:
            return None
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        if got < session_start(now):
            return None
        try:
            return self.f.decrypt(doc["enc"].encode()).decode()
        except Exception:  # noqa: BLE001 - wrong/rotated key: treat as not connected
            return None

    def status(self, email: str, now: datetime | None = None) -> dict:
        doc = self.c.find_one({"_id": email.lower()}, {"obtained_at": 1})
        got = doc.get("obtained_at") if doc else None
        return {"connected_today": self.load(email, now) is not None,
                "obtained_at": got.isoformat() if got else None}
