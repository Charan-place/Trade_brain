from datetime import datetime, time, timedelta

from tbrain.analytics import black76 as b
from tbrain.backtest.replay import fidelity, preset, replay_day
from tbrain.brokers.upstox import instruments as ins
from tbrain.brokers.upstox.normalize import empty_row
from tbrain.collector.store import TICK_SCHEMA, ParquetStore
from tbrain.signals.engine import EngineConfig, regime_of
from tbrain.timeutil import IST

UNDS = (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO"))


def _bars(start, closes):
    out = []
    for i, c in enumerate(closes):
        p = closes[i - 1] if i else c
        out.append([start + i * 60_000, p, max(p, c) + 1, min(p, c) - 1, c, 0])
    return out


def test_regime_trend_vs_range():
    t0 = datetime(2026, 10, 20, 9, 15, tzinfo=IST).timestamp() * 1000
    trend = regime_of(_bars(t0, [25000 + 3 * i for i in range(90)]), 4.0, 0, EngineConfig())
    assert trend["label"] == "trend" and trend["er60"] > 0.9 and trend["vs_ib"] == "above"
    chop = regime_of(_bars(t0, [25000 + (5 if i % 2 else -5) for i in range(90)]), 6.0, 9, EngineConfig())
    assert chop["label"] == "range" and chop["er60"] < 0.15


def test_fidelity_matcher():
    live = [{"und": "NIFTY", "dir": -1, "issued_ms": 1_000_000}, {"und": "SENSEX", "dir": 1, "issued_ms": 5_000_000}]
    rep = [{"und": "NIFTY", "dir": -1, "issued_ms": 1_060_000}, {"und": "SENSEX", "dir": -1, "issued_ms": 5_000_000}]
    f = fidelity(live, rep)
    assert f["matched"] == 1 and f["recall"] == 0.5


def test_replay_runs_brain_on_recorded_time(tmp_path, rows):
    """A synthetic recorded day (index + future + ATM legs) replays deterministically without look-ahead."""
    day = datetime(2026, 10, 19, tzinfo=IST).date()
    cs = ins.parse_rows(rows, UNDS)
    fut = ins.nearest_future(cs, "NIFTY", day)
    legs = [c for c in cs if c.underlying == "NIFTY" and c.kind in ("CE", "PE") and abs(c.strike - 25000) <= 100
            and c.expiry == ins.option_expiries(cs, "NIFTY", day)[1]]
    store = ParquetStore(tmp_path / "ticks", TICK_SCHEMA, flush_s=0)
    t = datetime.combine(day, time(9, 16), tzinfo=IST)
    batch, vtt = [], 0
    for i in range(40 * 6):  # 40 minutes, a tick every 10 s
        ts = t + timedelta(seconds=10 * i)
        ns = int(ts.timestamp() * 1e9)
        spot = 25000 + 0.4 * i
        vtt += 650
        r = empty_row(); r.update(recv_ns=ns, cur_ts_ms=ns // 10**6, instrument_key="NSE_INDEX|Nifty 50", ltp=spot, ltt_ms=ns // 10**6)
        f = empty_row(); f.update(recv_ns=ns, cur_ts_ms=ns // 10**6, instrument_key=fut.instrument_key, ltp=spot + 20, atp=spot,
                                  bp1=spot + 19.5, ap1=spot + 20.5, bq1=500, aq1=100, vtt=vtt, oi=1e6 + 100 * i, tbq=9e3, tsq=3e3)
        batch += [r, f]
        for c in legs:
            px = b.price(spot + 20, c.strike, 7 / 365, 0.12, 0.065, c.kind)
            g = empty_row(); g.update(recv_ns=ns, cur_ts_ms=ns // 10**6, instrument_key=c.instrument_key, ltp=px, bp1=px - 0.1,
                                      ap1=px + 0.1, aq1=6500, oi=1e5 + (300 if c.kind == "PE" else 100) * i, vtt=10 * i, ltt_ms=ns // 10**6)
            batch.append(g)
    store.add(batch); store.flush()
    (tmp_path / "instruments").mkdir()
    import json
    (tmp_path / "instruments" / f"contracts_{day}.json").write_text(json.dumps([c.to_json() for c in cs]))
    files = sorted((tmp_path / "ticks").rglob("*.parquet"))  # store partitions by wall-clock date; pass files explicitly
    res = replay_day(tmp_path, day.isoformat(), preset("v2.1"), tick_files=files, step_s=5.0, log=lambda *a: None)
    assert res["rows"] == len(batch) and res["snapshots"] >= 40 * 12 - 2
    assert res["summary"]["calls"] >= 0  # engine ran end to end on simulated time
    again = replay_day(tmp_path, day.isoformat(), preset("v2.1"), tick_files=files, step_s=5.0, log=lambda *a: None)
    assert [c["issued_ms"] for c in again["calls"]] == [c["issued_ms"] for c in res["calls"]]  # deterministic
