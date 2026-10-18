import json
import os
import socket
import threading
import time
import urllib.request
from datetime import datetime, timedelta

from tbrain.analytics import black76 as b
from tbrain.brain.demo import snapshot_from_probe
from tbrain.brain.state import BrainState, write_state
from tbrain.brokers.upstox import instruments as ins
from tbrain.brokers.upstox.normalize import empty_row
from tbrain.config import Settings
from tbrain.timeutil import IST

UNDS = (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO"))


def _row(key, **kw):
    r = empty_row(); r.update(instrument_key=key, recv_ns=time.time_ns(), src="market"); r.update(kw); return r


def _brain(rows_fixture):
    cs = ins.parse_rows(rows_fixture, UNDS)
    return cs, BrainState(cs, {"NIFTY": "NSE_INDEX|Nifty 50", "SENSEX": "BSE_INDEX|SENSEX"}, "NSE_INDEX|India VIX")


def test_snapshot_end_to_end(rows):
    cs, br = _brain(rows)
    exp = ins.option_expiries(cs, "NIFTY", min(c.expiry for c in cs) - timedelta(days=1))[0]
    ltt = int(datetime.combine(exp - timedelta(days=1), datetime.min.time(), tzinfo=IST).replace(hour=15, minute=29).timestamp() * 1000)
    F, T, r = 25010.0, (15.5 + 24 * 1) / 24 / 365 + 1 / 365 / 24 * 0.016, 0.065
    batch = [_row("NSE_INDEX|Nifty 50", ltp=25010.0, cp=24900.0, src="index"),
             _row("NSE_INDEX|India VIX", ltp=14.0, cp=13.5, src="index")]
    for c in cs:
        if c.underlying == "NIFTY" and c.expiry == exp and abs(c.strike - F) <= 200:
            px = b.price(F, c.strike, 1 / 365, 0.13, r, c.kind)
            batch.append(_row(c.instrument_key, ltp=px, bp1=px - 0.05, ap1=px + 0.05, oi=1000.0, vtt=10, ltt_ms=ltt, iv=0.12))
    fut = ins.nearest_future(cs, "NIFTY", exp - timedelta(days=1))
    batch.append(_row(fut.instrument_key, ltp=25030.0, bp1=25029.0, ap1=25031.0, bq1=500, aq1=100, vtt=1000, tbq=9000.0, tsq=3000.0))
    br.ingest(batch)
    br.ingest([_row(fut.instrument_key, ltp=25032.0, vtt=1600)])   # trade above previous mid -> buy
    br.ingest([_row(fut.instrument_key, ltp=25028.0, bp1=25027.0, ap1=25029.0, vtt=1800)])  # below prev mid -> sell
    snap = br.snapshot({"connected": True}, mode="demo")
    n = snap["underlyings"]["NIFTY"]
    ch = n["chains"][exp.isoformat()]
    assert abs(ch["forward"] - F) < 1.0 and abs(ch["atm_iv"] - 0.13) < 0.01  # valued at last trade time, ~1 day left
    assert n["future"]["basis"] == 25028.0 - 25010.0 and n["vix"]["ltp"] == 14.0
    f = n["flow"]
    assert f["cvd"] == 600 - 200 and abs(f["buy_share_5m"] - 0.75) < 1e-9 and f["resting_imbalance"] == 0.5
    assert snap["signals"]["status"] == "observing" and snap["signals"]["calls"] == []
    assert [l["id"] for l in snap["brain"]][0] == "senses" and snap["brain"][0]["status"] == "on"
    json.dumps(snap, default=str)  # serialisable


def test_depth_is_replaced_not_merged(rows):
    cs, br = _brain(rows)
    k = cs[0].instrument_key
    br.ingest([_row(k, bp1=10.0, bq1=5, bp2=9.9, bq2=7)])
    br.ingest([_row(k, bp1=10.1, bq1=3)])
    assert br.latest[k]["bp2"] is None and br.latest[k]["bp1"] == 10.1


def test_demo_from_probe_file(tmp_path, rows):
    cs = ins.parse_rows(rows, UNDS)
    detail = [c.to_json() for c in cs if c.underlying == "SENSEX"][:6]
    probe = {"manifest": {"underlyings": {"SENSEX": {"index_key": "BSE_INDEX|SENSEX"}}, "subscribed_detail": detail,
                          "market_status": {"BSE_FO": "NORMAL_CLOSE"}},
             "raw_samples": [{"feeds": {"BSE_INDEX|SENSEX": {"fullFeed": {"indexFF": {"ltpc": {"ltp": 82010.0, "cp": 81900.0}}}},
                                        detail[0]["instrument_key"]: {"fullFeed": {"marketFF": {"ltpc": {"ltp": 300.0, "ltt": "1792145377263"}, "oi": 5.0}}}}}]}
    p = tmp_path / "probe" / "probe_x.json"; p.parent.mkdir(); p.write_text(json.dumps(probe))
    snap = snapshot_from_probe(p, tmp_path)
    assert snap["mode"] == "demo" and snap["underlyings"]["SENSEX"]["spot"] == 82010.0


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_current_state_live_then_stale(tmp_path):
    from tbrain.ui.server import current_state
    s = Settings(data_dir=tmp_path)
    live = tmp_path / "live" / "state.json"
    write_state(live, {"mode": "live", "underlyings": {}, "brain": [], "signals": {}})
    assert current_state(s)["mode"] == "live"
    old = time.time() - 600
    os.utime(live, (old, old))
    assert current_state(s)["mode"] == "stale"


def test_sse_pushes_every_publish_and_serves_page():
    from tbrain.ui.server import Broadcaster, UIServer
    bc = Broadcaster()
    ui = UIServer(bc, _free_port()).start()
    try:
        page = urllib.request.urlopen(ui.url, timeout=5).read().decode()
        assert "EventSource" in page and "data age" in page
        bc.publish({"n": 0})
        resp = urllib.request.urlopen(ui.url + "api/stream", timeout=5)
        assert resp.headers["Content-Type"] == "text/event-stream"
        got, t0 = [], time.time()

        def pub():
            for i in range(1, 6):
                time.sleep(0.05); bc.publish({"n": i})
        threading.Thread(target=pub, daemon=True).start()
        while len(got) < 6 and time.time() - t0 < 5:
            line = resp.readline().decode().strip()
            if line.startswith("data: "):
                got.append(json.loads(line[6:])["n"])
        assert got[0] == 0 and got[-1] == 5 and got == sorted(got)  # newest always arrives, in order
        assert json.loads(urllib.request.urlopen(ui.url + "api/state", timeout=5).read())["n"] == 5
    finally:
        ui.stop()
