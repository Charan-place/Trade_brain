"""End-to-end: fake feed tick -> recorder -> brain (250 ms) -> SSE -> client. No network."""
import json
import socket
import threading
import time
import urllib.request
from datetime import timedelta

from tbrain.analytics import black76 as b
from tbrain.brain.state import BrainState
from tbrain.brokers.upstox import instruments as ins
from tbrain.brokers.upstox.normalize import empty_row
from tbrain.collector import recorder as rec
from tbrain.config import Settings

import os

UNDS = (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO"))
MARKER = 25123.45
SLACK = 3.0 if os.environ.get("CI") else 1.0  # shared CI runners are slower; local budgets stay strict


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FakeFeed:
    marker_at = None

    def __init__(self, token_provider, on_message, on_state=None, **kw):
        self.on_message, self.subs, self._stop = on_message, set(), threading.Event()
        self.reconnects = self.decode_errors = self.callback_errors = 0
        self.fatal, self.last_msg_ns = None, 0

    def subscribe(self, keys, mode="full"):
        new = [k for k in keys if k not in self.subs]; self.subs.update(new); return new

    def subscribed(self):
        return set(self.subs)

    def wait_connected(self, t):
        return True

    def start(self):
        def pump():
            t0 = time.time()
            while not self._stop.is_set():
                spot = 25010.0
                if time.time() - t0 > 1.5:
                    spot = MARKER
                    if FakeFeed.marker_at is None:
                        FakeFeed.marker_at = time.time()
                feeds = {"NSE_INDEX|Nifty 50": {"fullFeed": {"indexFF": {"ltpc": {"ltp": spot, "cp": 24900.0}}}},
                         "BSE_INDEX|SENSEX": {"fullFeed": {"indexFF": {"ltpc": {"ltp": 82010.0, "cp": 81900.0}}}}}
                for k in list(self.subs):
                    if "INDEX" not in k:
                        feeds[k] = {"fullFeed": {"marketFF": {"ltpc": {"ltp": 100.0}, "oi": 10.0}}}
                self.last_msg_ns = time.time_ns()
                self.on_message({"type": "live_feed", "currentTs": str(int(time.time() * 1000) - 40), "feeds": feeds}, time.time_ns())
                time.sleep(0.02)
        threading.Thread(target=pump, daemon=True).start()

    def stop(self):
        self._stop.set()


def test_tick_to_screen_latency(tmp_path, monkeypatch, rows, today):
    cs = ins.parse_rows(rows, UNDS)
    monkeypatch.setattr(rec.ins, "load_contracts", lambda *a, **k: cs)
    monkeypatch.setattr(rec, "ist_date", lambda: today)
    monkeypatch.setattr(rec, "LiveFeed", FakeFeed)
    monkeypatch.setattr(rec, "fetch_option_chain", lambda *a: {"data": []})
    FakeFeed.marker_at = None
    port = _free_port()
    s = Settings(data_dir=tmp_path, flush_s=1, n_strikes=10, n_expiries=2, chain_poll_s=999)
    r = rec.Recorder(s, lambda: "tok", log=lambda *a: None, ui_port=port)
    th = threading.Thread(target=lambda: r.run(duration_s=4, respect_hours=False), daemon=True)
    th.start()
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stream", timeout=5); break
        except OSError:
            time.sleep(0.05)
    frames, seen_at = [], None
    t_end = time.time() + 3
    while time.time() < t_end:
        line = resp.readline().decode().strip()
        if not line.startswith("data: "):
            continue
        st = json.loads(line[6:])
        frames.append(time.time())
        if seen_at is None and st["underlyings"].get("NIFTY", {}).get("spot") == MARKER:
            seen_at = time.time()
            lat = st["latency"]
    th.join(10)
    gaps = [b2 - a2 for a2, b2 in zip(frames, frames[1:])]
    assert len(frames) >= 8, len(frames)
    assert sorted(gaps)[len(gaps) // 2] < 0.35 * SLACK  # ~250 ms cadence
    assert seen_at is not None and FakeFeed.marker_at is not None
    e2e = seen_at - FakeFeed.marker_at
    print(f"\nframes={len(frames)} median_gap={sorted(gaps)[len(gaps)//2]*1000:.0f}ms tick->screen={e2e*1000:.0f}ms "
          f"compute={lat['compute_ms']:.1f}ms upstox_lag_p50={lat['upstox_to_us_ms_p50']:.0f}ms")
    assert e2e < 0.6 * SLACK
    assert 30 <= lat["upstox_to_us_ms_p50"] <= 200     # FakeFeed stamps currentTs 40 ms in the past


def test_full_chain_compute_budget(rows, today):
    """Both underlyings, every leg in the fixture (31 strikes x 2 expiries x CE/PE each): must fit well inside 250 ms."""
    cs = ins.parse_rows(rows, UNDS)
    br = BrainState(cs, {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX"}, None)
    batch = []
    for und, spot in (("NIFTY", 25010.0), ("SENSEX", 82049.0)):
        r = empty_row(); r.update(instrument_key="NSE_INDEX|Nifty 50" if und == "NIFTY" else "BSE_INDEX|SENSEX", ltp=spot)
        batch.append(r)
    for c in cs:
        if c.kind in ("CE", "PE"):
            F = 25010.0 if c.underlying == "NIFTY" else 82049.0
            px = b.price(F, c.strike, 3 / 365, 0.13, 0.065, c.kind)
            r = empty_row(); r.update(instrument_key=c.instrument_key, ltp=px, bp1=px * 0.99, ap1=px * 1.01, oi=100.0)
            batch.append(r)
    br.ingest(batch)
    best = min(br.snapshot({}, "live")["latency"]["compute_ms"] for _ in range(5))
    n_legs = sum(1 for c in cs if c.kind in ("CE", "PE"))
    print(f"\nsnapshot compute for {n_legs} legs: {best:.1f} ms")
    assert best < 150 * SLACK
