"""One trading day of the brain in the hosted (Modal) deployment. Kept free of Modal imports so it is testable."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from tbrain.timeutil import MARKET_CLOSE, at_ist, now_ist

LEASE_TTL = timedelta(seconds=120)
LEASE_HEARTBEAT_S = 15
LEASE_WAIT_S = 135


class _RecorderLease:
    """Mongo-backed atomic lease; live-state snapshots are presentation data, not a lock."""

    def __init__(self, collection, key: str, log=print):
        self.collection, self.key, self.log = collection, key, log
        self.lease_id = uuid.uuid4().hex
        self.stop = threading.Event()
        self.thread = None
        self.on_lost = None

    def acquire(self, wait_s: float = LEASE_WAIT_S) -> bool:
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        deadline = time.monotonic() + wait_s
        while True:
            now = datetime.now(timezone.utc)
            try:
                doc = self.collection.find_one_and_update(
                    {"_id": self.key, "$or": [{"lease_until": {"$lte": now}}, {"lease_id": self.lease_id}]},
                    {"$set": {"lease_id": self.lease_id, "lease_until": now + LEASE_TTL,
                              "heartbeat_at": now}},
                    upsert=True, return_document=ReturnDocument.AFTER)
                if doc and doc.get("lease_id") == self.lease_id:
                    self.thread = threading.Thread(target=self._heartbeat, daemon=True, name="recorder-lease")
                    self.thread.start()
                    return True
            except DuplicateKeyError:
                # Another live (or just-preempted) invocation owns the document. Wait for its lease
                # to expire instead of returning success to Modal and suppressing its retry.
                pass
            if time.monotonic() >= deadline:
                return False
            self.stop.wait(LEASE_HEARTBEAT_S)

    def _heartbeat(self) -> None:
        while not self.stop.wait(LEASE_HEARTBEAT_S):
            now = datetime.now(timezone.utc)
            try:
                result = self.collection.update_one(
                    {"_id": self.key, "lease_id": self.lease_id},
                    {"$set": {"lease_until": now + LEASE_TTL, "heartbeat_at": now}})
                if not result.matched_count:
                    self.log("[hosted] recorder lease was lost")
                    if self.on_lost:
                        self.on_lost()
                    return
            except Exception as e:  # noqa: BLE001 - a transient database hiccup should not kill the recorder
                self.log(f"[hosted] recorder lease heartbeat failed: {type(e).__name__}")

    def release(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        try:
            self.collection.update_one({"_id": self.key, "lease_id": self.lease_id},
                                       {"$set": {"lease_until": datetime.now(timezone.utc)}})
        except Exception as e:  # noqa: BLE001
            self.log(f"[hosted] recorder lease release failed: {type(e).__name__}")


def run_hosted_day(s, state_dict, log=print, recorder_cls=None, tokens=None, now=None) -> str:
    """Start today's recorder for the owner, unless it should not run. Returns why it stopped."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return "weekend"
    if now >= at_ist(now.date(), MARKET_CLOSE):
        return "market-closed"
    if not s.owner_email:
        raise RuntimeError("TBRAIN_OWNER_EMAIL / TBRAIN_ALLOWED_EMAILS is not set")
    if tokens is None:
        from tbrain.storage.mongo import connect
        from tbrain.web.tokens import TokenStore
        tokens = TokenStore(connect(s.mongodb_uri, s.mongodb_db), s.encryption_key)
    if not tokens.load(s.owner_email, now=now):
        log("[hosted] Upstox is not connected today - open the app and click 'Connect Upstox'")
        return "no-token"
    lease = _RecorderLease(tokens.c.database["recorder_leases"],
                           f"{s.owner_email}:{now.date().isoformat()}", log=log)
    if not lease.acquire():
        log("[hosted] another recorder is still active - exiting")
        return "already-running"
    try:
        from tbrain.web.state import make_dict_publisher
        if recorder_cls is None:
            from tbrain.collector.recorder import Recorder as recorder_cls
        from tbrain.runtime_settings import DictSettings
        r = recorder_cls(s, lambda: tokens.load(s.owner_email, now=now) or "", write=True, ui_port=None,
                         publish=make_dict_publisher(state_dict, log=log), log=log,
                         settings_store=DictSettings(state_dict))
        lease.on_lost = lambda: getattr(r, "_stop", threading.Event()).set()
        r.run()
        return "done"
    finally:
        lease.release()
