"""MongoDB storage: uses mongomock (in-memory) so tests never need a real server."""
import json
import math
import time

import pytest

mongomock = pytest.importorskip("mongomock")

from tbrain.signals.engine import EngineConfig, SignalEngine  # noqa: E402
from tbrain.storage import mongo as M  # noqa: E402
from tests.test_signals import NOW_MS, _ctx, _tracker  # noqa: E402


def _db():
    """In-memory mongomock by default; a real server when TBRAIN_TEST_MONGODB_URI is set (wire-protocol check)."""
    import os
    import uuid
    uri = os.environ.get("TBRAIN_TEST_MONGODB_URI")
    if uri:
        from pymongo import MongoClient
        return MongoClient(uri, serverSelectionTimeoutMS=5000)[f"tbrain_test_{uuid.uuid4().hex[:8]}"]
    return mongomock.MongoClient()["tbrain_test"]


def _wait(sink, cond, timeout=5):
    t = time.time()
    while time.time() - t < timeout:
        sink.flush()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_clean_makes_documents_bson_safe():
    np = pytest.importorskip("numpy")
    d = M.clean({"a.b": 1, "$x": np.float64(2.5), "n": float("nan"), "i": math.inf, "l": (1, np.int64(3)), "o": object()})
    assert d["a_b"] == 1 and d["x"] == 2.5 and d["n"] is None and d["i"] is None and d["l"] == [1, 3]
    assert isinstance(d["o"], str)


def test_engine_calls_flow_into_mongo_with_event_history(tmp_path):
    db = _db()
    sink = M.MongoSink(db, owner="me@example.com", log=lambda *a: None, flush_s=0.05)
    eng = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    eng.on_event = sink.on_call_event
    out = eng.step(NOW_MS, {"NIFTY": _ctx()}, _tracker())
    (c,) = out["calls"]
    assert _wait(sink, lambda: db.calls.find_one({"_id": c["id"]}) is not None)
    doc = db.calls.find_one({"_id": c["id"]})
    assert doc["owner"] == "me@example.com" and doc["day"] == "2026-10-20" and doc["und"] == "NIFTY"
    assert [e["event"] for e in doc["events"]] == ["issue"]
    # the local jsonl is still written (files stay the source of truth)
    assert (tmp_path / "calls.jsonl").exists()
    sink.close()


def test_sink_survives_a_broken_database():
    class Boom:
        def __getitem__(self, k):
            raise RuntimeError("cluster down")
    logs = []
    sink = M.MongoSink(Boom(), log=logs.append, flush_s=0.05)
    sink.on_call_event("issue", {"id": "x1", "issued_ms": NOW_MS})
    sink.on_daily("2026-10-20", {}, {})
    time.sleep(0.2)
    sink.close()
    assert sink.dropped >= 1 and sink.errors >= 1 and logs  # counted + logged, never raised


def test_minute_features_one_doc_per_underlying_per_minute():
    db = _db()
    sink = M.MongoSink(db, owner="me", log=lambda *a: None, flush_s=0.05)
    snap = {"generated_ms": NOW_MS + 5_000,
            "underlyings": {"NIFTY": {"spot": 25100.0, "vwap": 25090.0, "future": {"ltp": 25120.0, "oi": 1e6},
                                      "flow": {"cvd": 10.0}, "candles": [[NOW_MS, 1, 2, 0.5, 1.5, 0]],
                                      "chains": {"2026-10-27": {"atm_iv": 0.12, "pcr_oi": 0.9, "max_pain": 25100.0}}}},
            "signals": {"open_ids": [], "diagnostics": {"NIFTY": {"atr_1m": 9.5, "regime": {"label": "trend"},
                        "sides": {"bullish": {"score": 5, "conditions": [{"key": "vwap", "ok": True}]}}}}}}
    sink.on_snapshot(snap)
    sink.on_snapshot(dict(snap, generated_ms=NOW_MS + 30_000))   # same minute -> ignored
    sink.on_snapshot(dict(snap, generated_ms=NOW_MS + 65_000))   # next minute -> new doc
    assert _wait(sink, lambda: db.features.count_documents({}) == 2)
    d = db.features.find_one({"_id": "2026-10-20|NIFTY|10:30"})
    assert d["chain"]["atm_iv"] == 0.12 and d["regime"]["label"] == "trend" and d["sides"]["bullish"]["clues"]["vwap"] is True
    sink.close()


def test_import_local_is_idempotent(tmp_path):
    eng = SignalEngine(EngineConfig(), store_dir=tmp_path / "signals", log=lambda *a: None)
    (c,) = eng.step(NOW_MS, {"NIFTY": _ctx()}, _tracker())["calls"]
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session_2026-10-20_091500.json").write_text(json.dumps(
        {"started": "2026-10-20T09:15:00+05:30", "health": {"rows": 10}, "subscribed_detail": [{"k": 1}]}))
    db = _db()
    first = M.import_local(db, tmp_path, "me", log=lambda *a: None)
    again = M.import_local(db, tmp_path, "me", log=lambda *a: None)
    assert first == again and first["calls"] == 1 and first["sessions"] == 1 and first["daily"] == 1
    assert db.calls.count_documents({}) == 1 and db.sessions.count_documents({}) == 1
    assert "subscribed_detail" not in db.sessions.find_one()
    assert db.daily.find_one({"_id": "2026-10-20|me"})["summary"]["calls"] == 1


def test_open_sink_returns_none_without_uri():
    from tbrain.config import Settings
    assert M.open_sink(Settings(), log=lambda *a: None) is None
