"""MongoDB storage: the brain's training data and the hosted app's users.

Design rules
- Optional: with no MONGODB_URI nothing here runs and local files stay the source of truth.
- Never blocks or crashes the brain: writes go into a bounded queue drained by one background thread that
  batches them (bulk_write) roughly once a second. If Mongo is down, ops are retried, then dropped and counted.
- Raw ticks never go to Mongo (5M+ rows/day); they stay in Parquet. Mongo holds compact, labelled data:
    calls      every real call: full inputs at issue + event history + final outcome          (_id = call id)
    shadows    every blocked candidate, tracked to its outcome                                  (_id = call id)
    features   one doc per underlying per minute: price, VWAP, ATR, regime, flow, chain, clues  (_id = day|und|HH:MM)
    sessions   recorder run manifests                                                           (_id = started)
    daily      per-day summary + filter scorecard                                               (_id = day|owner)
    users      Google accounts allowed into the hosted app                                     (_id = email)
    upstox_tokens  per-user Upstox access token, encrypted (Fernet)                            (_id = email)
"""
from __future__ import annotations

import math
import queue
import threading
import time
from datetime import datetime, timezone

from tbrain.timeutil import IST

COLLECTIONS = ("calls", "shadows", "features", "sessions", "daily", "users", "upstox_tokens")


def clean(x):
    """Make any brain object safe for BSON: no '.'/'$' in keys, no NaN/inf, numpy scalars -> python."""
    if isinstance(x, dict):
        return {str(k).replace(".", "_").lstrip("$") or "_": clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):  # numpy scalar
        try:
            x = x.item()
        except (ValueError, TypeError):
            return str(x)
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (str, int, float, bool, datetime)) or x is None:
        return x
    return str(x)


CANDLE_FIELDS = ("t", "o", "h", "l", "c", "v")


def candle_dict(bar) -> dict | None:
    """[t, o, h, l, c, v] -> {"t":..,"o":..,...}: named fields query better and avoid nested arrays."""
    if not isinstance(bar, (list, tuple)):
        return bar
    return dict(zip(CANDLE_FIELDS, bar))


def day_of_ms(ms: float | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=IST).date().isoformat()


def _escape_uri_userinfo(uri: str) -> str:
    """Percent-escape a raw user:pass in a mongodb URI per RFC 3986, so an unescaped '@'/':'/'/' etc.
    in the password (e.g. pasted straight from Atlas) doesn't break pymongo's URI parser. Splits on the
    LAST '@' before the host (a password may itself contain '@'), and unquotes first so an already-escaped
    URI round-trips unchanged instead of being double-encoded."""
    import re
    from urllib.parse import quote_plus, unquote_plus

    m = re.match(r"^(mongodb(?:\+srv)?://)(.+)@([^@]+)$", uri)
    if not m:
        return uri
    scheme, userinfo, rest = m.groups()
    parts = userinfo.split(":", 1)
    if len(parts) == 2:
        user, pwd = parts
        userinfo = f"{quote_plus(unquote_plus(user))}:{quote_plus(unquote_plus(pwd))}"
    else:
        userinfo = quote_plus(unquote_plus(userinfo))
    return f"{scheme}{userinfo}@{rest}"


def connect(uri: str, db: str, client=None):
    """Return a pymongo Database. `client` lets tests pass a mongomock client."""
    if client is None:
        from pymongo import MongoClient
        client = MongoClient(_escape_uri_userinfo(uri), serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
                              appname="tbrain")
    return client[db]


def ensure_indexes(db) -> None:
    from pymongo import ASCENDING, DESCENDING
    db.calls.create_index([("day", ASCENDING), ("und", ASCENDING)])
    db.calls.create_index([("owner", ASCENDING), ("issued_ms", DESCENDING)])
    db.shadows.create_index([("day", ASCENDING), ("und", ASCENDING)])
    db.features.create_index([("day", ASCENDING), ("und", ASCENDING), ("t_ms", ASCENDING)])
    db.sessions.create_index([("day", ASCENDING)])
    db.daily.create_index([("day", DESCENDING)])


def ping(db) -> bool:
    db.client.admin.command("ping")
    return True


# ---------------------------------------------------------------- documents
def call_doc(call: dict, owner: str) -> dict:
    d = clean(call)
    if isinstance(d.get("candles"), list):
        d["candles"] = [candle_dict(b) for b in d["candles"]]
    d["day"] = day_of_ms(call.get("issued_ms"))
    d["owner"] = owner
    d["updated_at"] = datetime.now(timezone.utc)
    return d


def minute_feature_docs(snap: dict, owner: str) -> list[dict]:
    """Compact, model-ready features per underlying from one brain snapshot."""
    out = []
    t_ms = snap.get("generated_ms")
    if not t_ms:
        return out
    minute_ms = int(t_ms // 60000 * 60000)
    stamp = datetime.fromtimestamp(minute_ms / 1000, tz=IST)
    sig = snap.get("signals") or {}
    diags = sig.get("diagnostics") or {}
    for und, u in (snap.get("underlyings") or {}).items():
        if not isinstance(u, dict) or u.get("spot") is None:
            continue
        chains = u.get("chains") or {}
        near = chains[sorted(chains)[0]] if chains else {}
        dg = diags.get(und) or {}
        sides = {}
        for name, sd in (dg.get("sides") or {}).items():
            sides[name] = {"score": sd.get("score"),
                           "clues": {c.get("key"): bool(c.get("ok")) for c in sd.get("conditions") or [] if c.get("key")}}
        candles = u.get("candles") or []
        doc = {
            "_id": f"{stamp.date().isoformat()}|{und}|{stamp:%H:%M}",
            "day": stamp.date().isoformat(), "und": und, "t_ms": minute_ms, "owner": owner,
            "spot": u.get("spot"), "spot_chg_pct": u.get("spot_chg_pct"),
            "vwap": u.get("vwap"), "vwap_index_equiv": u.get("vwap_index_equiv"),
            "future": {k: (u.get("future") or {}).get(k) for k in ("ltp", "basis", "oi", "vtt")},
            "flow": u.get("flow"), "vix": (u.get("vix") or {}).get("ltp"),
            "big_net_5m": (u.get("big") or {}).get("net_5m"),
            "last_candle": candle_dict(candles[-1]) if candles else None,
            "chain": {k: near.get(k) for k in ("expiry", "forward", "atm", "atm_iv", "atm_iv_vendor", "rr25", "straddle",
                                               "pcr_oi", "pcr_vol", "max_pain", "call_wall", "put_wall")},
            "atr_1m": dg.get("atr_1m"), "hh10": dg.get("hh10"), "ll10": dg.get("ll10"),
            "regime": dg.get("regime"), "blocked": dg.get("blocked"), "sides": sides,
            "open_calls": len(sig.get("open_ids") or []),
        }
        out.append(clean(doc))
    return out


def daily_doc(day: str, owner: str, summary: dict, shadow_by_blocker: dict, rules: dict | None) -> dict:
    return clean({"_id": f"{day}|{owner}", "day": day, "owner": owner, "summary": summary,
                  "shadow_by_blocker": shadow_by_blocker, "rules": rules or {},
                  "updated_at": datetime.now(timezone.utc)})


# ---------------------------------------------------------------- live sink
class MongoSink:
    """Fire-and-forget writer used by the live recorder. Thread-safe; never raises into the caller."""

    def __init__(self, db, owner: str = "local", log=print, maxsize: int = 20_000, flush_s: float = 1.0):
        self.db = db
        self.owner = owner or "local"
        self.log = log
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.flush_s = flush_s
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self._last_minute: dict[str, int] = {}
        self._last_err_log = 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="mongo-sink")
        self._t.start()

    # -- producers (cheap, called from hot paths)
    def _put(self, coll: str, _id, doc: dict, push: dict | None = None) -> None:
        try:
            self.q.put_nowait((coll, _id, doc, push))
        except queue.Full:
            self.dropped += 1

    def on_call_event(self, event: str, call: dict) -> None:
        try:
            coll = "shadows" if call.get("shadow") else "calls"
            self._put(coll, call["id"], call_doc(call, self.owner),
                      {"events": {"event": event, "at": datetime.now(timezone.utc)}})
        except Exception as e:  # noqa: BLE001 - storage must never break the brain
            self._err(e)

    def on_snapshot(self, snap: dict) -> None:
        """Store one feature doc per underlying per minute (first snapshot after each minute boundary)."""
        try:
            t = snap.get("generated_ms")
            if not t:
                return
            m = int(t // 60000)
            for d in minute_feature_docs(snap, self.owner):
                if self._last_minute.get(d["und"]) == m:
                    continue
                self._last_minute[d["und"]] = m
                self._put("features", d["_id"], d)
        except Exception as e:  # noqa: BLE001
            self._err(e)

    def on_daily(self, day: str, summary: dict, shadow_by_blocker: dict, rules: dict | None = None) -> None:
        try:
            d = daily_doc(day, self.owner, summary, shadow_by_blocker, rules)
            self._put("daily", d["_id"], d)
        except Exception as e:  # noqa: BLE001
            self._err(e)

    def on_session(self, manifest: dict) -> None:
        try:
            m = {k: v for k, v in manifest.items() if k != "subscribed_detail"}
            m.update(owner=self.owner, day=str(manifest.get("started", ""))[:10])
            self._put("sessions", str(manifest.get("started")), clean(m))
        except Exception as e:  # noqa: BLE001
            self._err(e)

    # -- consumer
    def _run(self) -> None:
        while not self._stop.is_set() or not self.q.empty():
            self._stop.wait(self.flush_s)
            self.flush()

    def flush(self) -> None:
        batch: dict[str, dict] = {}
        pushes: dict[str, list] = {}
        while True:
            try:
                coll, _id, doc, push = self.q.get_nowait()
            except queue.Empty:
                break
            key = f"{coll}\x00{_id}"
            batch[key] = (coll, _id, doc)          # last write of a doc in this batch wins
            if push:
                pushes.setdefault(key, []).append(push)
        if not batch:
            return
        # A few docs per second at most (features 2/min, daily 1/min, call events rare): plain upserts are
        # far below Atlas free-tier limits (100 ops/s) and behave the same on every Mongo-compatible server.
        for key, (coll, _id, doc) in batch.items():
            doc = {k: v for k, v in doc.items() if k != "_id"}
            upd = {"$set": doc}
            if key in pushes:
                upd["$push"] = {"events": {"$each": [p["events"] for p in pushes[key]]}}
            for attempt in range(3):
                try:
                    self.db[coll].update_one({"_id": _id}, upd, upsert=True)
                    self.written += 1
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        self.dropped += 1
                        self._err(e)
                    else:
                        time.sleep(1.5 * (attempt + 1))

    def _err(self, e: Exception) -> None:
        self.errors += 1
        if time.time() - self._last_err_log > 60:
            self._last_err_log = time.time()
            self.log(f"[mongo] write problem (recording continues): {type(e).__name__}: {str(e)[:160]}")

    def stats(self) -> dict:
        return {"written": self.written, "dropped": self.dropped, "errors": self.errors, "queued": self.q.qsize()}

    def close(self, timeout: float = 15) -> None:
        self._stop.set()
        self._t.join(timeout)


def open_sink(s, log=print, client=None) -> MongoSink | None:
    """Sink for the live recorder, or None when MongoDB is not configured / unreachable (recording goes on)."""
    if not s.mongodb_uri and client is None:
        return None
    try:
        db = connect(s.mongodb_uri, s.mongodb_db, client)
        ping(db)
        ensure_indexes(db)
    except Exception as e:  # noqa: BLE001
        log(f"[mongo] not available ({type(e).__name__}: {str(e)[:160]}); recording to local files only")
        return None
    log(f"[mongo] connected to database '{s.mongodb_db}' - saving calls, shadows and minute features")
    return MongoSink(db, owner=s.owner_email or "local", log=log)


# ---------------------------------------------------------------- import past days from local files
def import_local(db, data_dir, owner: str, day: str | None = None, log=print) -> dict:
    """Load data/signals/*.jsonl and data/sessions/*.json into Mongo (idempotent: re-running just overwrites)."""
    import json
    from pathlib import Path

    from tbrain.signals.engine import SignalEngine

    data_dir = Path(data_dir)
    ensure_indexes(db)
    counts = {}
    books: dict[str, dict] = {"calls": {}, "shadows": {}}
    for fname, coll in (("calls.jsonl", "calls"), ("shadow.jsonl", "shadows")):
        p = data_dir / "signals" / fname
        if not p.exists():
            continue
        events: dict[str, list] = {}
        for line in p.read_text().splitlines():
            try:
                rec = json.loads(line)
                c = rec["call"]
            except (json.JSONDecodeError, KeyError):
                continue
            if day and day_of_ms(c.get("issued_ms")) != day:
                continue
            books[coll][c["id"]] = c
            events.setdefault(c["id"], []).append(
                {"event": rec.get("event"), "at": datetime.fromtimestamp(rec.get("ts") or 0, tz=timezone.utc)})
        for cid, c in books[coll].items():
            d = call_doc(c, owner)
            d["events"] = events.get(cid, [])
            d["_id"] = cid
            db[coll].replace_one({"_id": cid}, d, upsert=True)
        counts[coll] = len(books[coll])
    n_sess = 0
    for p in sorted((data_dir / "sessions").glob("session_*.json")):
        try:
            m = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        sday = str(m.get("started", ""))[:10]
        if day and sday != day:
            continue
        m = {k: v for k, v in m.items() if k != "subscribed_detail"}
        m.update(owner=owner, day=sday, source_file=p.name)
        db.sessions.replace_one({"_id": str(m.get("started"))}, clean(m), upsert=True)
        n_sess += 1
    counts["sessions"] = n_sess
    days = sorted({day_of_ms(c.get("issued_ms")) for c in books["calls"].values()} - {None})
    for d in days:
        cs = [c for c in books["calls"].values() if day_of_ms(c.get("issued_ms")) == d]
        sh = [c for c in books["shadows"].values() if day_of_ms(c.get("issued_ms")) == d]
        by = {}
        for s in sh:
            for b in {SignalEngine.blocker_key(x) for x in s.get("blocked_by", [])} or {"other"}:
                by.setdefault(b, []).append(s)
        table = {b: {"n": len(v), "wins": sum(x.get("pnl_per_lot", 0) > 0 for x in v if x.get("status") == "closed"),
                     "sum_r": round(sum(x.get("r_now") or 0 for x in v), 2),
                     "pnl_per_lot": round(sum(x.get("pnl_per_lot") or 0 for x in v))} for b, v in sorted(by.items())}
        dd = daily_doc(d, owner, SignalEngine.summarize(cs), table, None)
        db.daily.replace_one({"_id": dd["_id"]}, dd, upsert=True)
    counts["daily"] = len(days)
    log(f"[mongo] imported {counts}")
    return counts
