"""End-to-end recorder run with a fake feed + fake REST (no network)."""
import json
import threading
import time

import pyarrow.dataset as ds
import pytest

from tbrain.brokers.upstox import instruments as ins
from tbrain.collector import recorder as rec
from tbrain.config import Settings


class FakeFeed:
    def __init__(self, token_provider, on_message, on_state=None, **kw):
        self.on_message, self.subs, self._stop = on_message, set(), threading.Event()
        self.reconnects = self.decode_errors = self.callback_errors = 0
        self.fatal = None
        self.last_msg_ns = 0

    def subscribe(self, keys, mode="full"):
        new = [k for k in keys if k not in self.subs]; self.subs.update(new); return new

    def subscribed(self):
        return set(self.subs)

    def wait_connected(self, t):
        return True

    def start(self):
        def pump():
            spot = {"NSE_INDEX|Nifty 50": 25012.0, "BSE_INDEX|SENSEX": 82049.0}
            i = 0
            while not self._stop.is_set():
                i += 1
                feeds = {}
                for k in list(self.subs):
                    if k in spot:
                        px = spot[k] + (300 if k.startswith("NSE") and i > 8 else 0)  # nifty jumps -> recenter
                        feeds[k] = {"fullFeed": {"indexFF": {"ltpc": {"ltp": px, "cp": spot[k]}}}}
                    else:
                        feeds[k] = {"fullFeed": {"marketFF": {"ltpc": {"ltp": 10.0, "ltq": "65"}, "vtt": str(i), "oi": 100.0}}}
                self.last_msg_ns = time.time_ns()
                self.on_message({"type": "live_feed", "currentTs": str(int(time.time() * 1000)), "feeds": feeds}, self.last_msg_ns)
                time.sleep(0.1)
        threading.Thread(target=pump, daemon=True).start()

    def stop(self):
        self._stop.set()


def test_offline_session(tmp_path, monkeypatch, rows, today):
    cs = ins.parse_rows(rows, (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO")))
    monkeypatch.setattr(rec.ins, "load_contracts", lambda *a, **k: cs)
    monkeypatch.setattr(rec, "ist_date", lambda: today)
    monkeypatch.setattr(rec, "LiveFeed", FakeFeed)
    monkeypatch.setattr(rec, "RECENTER_CHECK_S", 0.5)
    chain_payload = {"data": [{"expiry": "2026-10-20", "strike_price": 25000, "underlying_spot_price": 25012, "pcr": 0.9,
                               "call_options": {"instrument_key": "NSE_FO|x", "market_data": {"ltp": 1, "oi": 5, "prev_oi": 4},
                                                "option_greeks": {"delta": 0.5, "iv": 14.2}},
                               "put_options": {"instrument_key": "NSE_FO|y", "market_data": {"ltp": 2}}}]}
    monkeypatch.setattr(rec, "fetch_option_chain", lambda *a: chain_payload)
    s = Settings(data_dir=tmp_path, flush_s=1, n_strikes=2, n_expiries=1, chain_poll_s=1)
    r = rec.Recorder(s, lambda: "tok", log=lambda *a: None)
    m = r.run(duration_s=4, respect_hours=False)

    assert set(m["underlyings"]) == {"NIFTY", "SENSEX"}
    assert m["underlyings"]["SENSEX"]["index_key"] == "BSE_INDEX|SENSEX"
    # 2 index + 2 fut + VIX + 10 nifty legs + 10 sensex legs + recentred nifty strikes
    assert m["subscribed"] > 25, m["subscribed"]
    assert m["rows_written"] > 0 and m["chain_rows_written"] >= 2
    t = ds.dataset(str(tmp_path / "ticks"), format="parquet").to_table()
    assert t.num_rows == m["rows_written"] and "NSE_INDEX|Nifty 50" in set(t["instrument_key"].to_pylist())
    assert list((tmp_path / "sessions").glob("session_*.json"))


def test_headless_session_runs_brain_publishes_and_saves_to_mongo(tmp_path, monkeypatch, rows, today):
    """No UI port (hosted mode): the brain loop must still run, feed the publish hook and the Mongo sink."""
    mongomock = pytest.importorskip("mongomock")
    from tbrain.storage import mongo as M
    cs = ins.parse_rows(rows, (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO")))
    monkeypatch.setattr(rec.ins, "load_contracts", lambda *a, **k: cs)
    monkeypatch.setattr(rec, "ist_date", lambda: today)
    monkeypatch.setattr(rec, "LiveFeed", FakeFeed)
    monkeypatch.setattr(rec, "fetch_option_chain", lambda *a: {"data": []})
    db = mongomock.MongoClient()["t"]
    monkeypatch.setattr(M, "open_sink", lambda s, log=print, client=None: M.MongoSink(db, owner="me", log=log, flush_s=0.1))
    snaps = []
    s = Settings(data_dir=tmp_path, flush_s=1, n_strikes=2, n_expiries=1, chain_poll_s=1)
    logs = []
    r = rec.Recorder(s, lambda: "tok", log=logs.append, ui_port=None, publish=snaps.append)
    m = r.run(duration_s=3, respect_hours=False)
    assert len(snaps) >= 5 and snaps[-1]["mode"] == "live", logs[-8:]  # 250 ms brain loop ran without any UI
    assert db.sessions.count_documents({}) == 1 and db.sessions.find_one()["owner"] == "me"
    assert db.features.count_documents({}) >= 1                       # minute features saved
    assert db.daily.count_documents({}) == 1
    assert m["rows_written"] > 0
