from tbrain.brain.state import BrainState
from tbrain.brokers.upstox import instruments as ins
from tbrain.brokers.upstox.normalize import empty_row
from tbrain.collector.backfill import backfill_today
from tbrain.collector.store import TICK_SCHEMA, ParquetStore
from tbrain.timeutil import now_ist

UNDS = (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO"))


def test_restart_rebuilds_candles_and_vwap(tmp_path, rows, today):
    cs = ins.parse_rows(rows, UNDS)
    fut = ins.nearest_future(cs, "NIFTY", today)
    store = ParquetStore(tmp_path / "ticks", TICK_SCHEMA, flush_s=0)
    t0 = 1_792_400_000_000 - 1_792_400_000_000 % 60_000
    batch = []
    for i in range(30):  # 30 minutes of index + futures updates
        ms = t0 + i * 60_000
        a = empty_row(); a.update(recv_ns=ms * 1_000_000, instrument_key="NSE_INDEX|Nifty 50", ltp=25000.0 + i, ltt_ms=ms)
        f = empty_row(); f.update(recv_ns=ms * 1_000_000 + 1, instrument_key=fut.instrument_key, ltp=25020.0 + i, ltt_ms=ms,
                                  vtt=1000 * (i + 1), atp=25010.0)
        batch += [a, f]
    store.add(batch); store.flush()
    (tmp_path / "ticks" / f"date={now_ist().date()}" / "part-junk.parquet.tmp").write_bytes(b"partial")  # must be ignored
    br = BrainState(cs, {"NIFTY": "NSE_INDEX|Nifty 50"}, None)
    n = backfill_today(br, tmp_path / "ticks", now_ist().date().isoformat(), {"NSE_INDEX|Nifty 50", fut.instrument_key},
                       {"NIFTY": fut.instrument_key}, log=lambda *a: None)
    assert n == 60
    bars = br.candles.series("NSE_INDEX|Nifty 50", 400)
    assert len(bars) == 30 and bars[0][4] == 25000.0 and bars[-1][4] == 25029.0
    fb = br.candles.series(fut.instrument_key, 400)
    assert fb[-1][5] == 1000.0  # volume from vtt deltas
    v = sorted(br.vwap_hist["NIFTY"].items())
    assert len(v) == 30 and abs(v[0][1] - (25010.0 - 20.0)) < 1e-9  # atp - basis


def test_backfill_no_data_is_harmless(tmp_path, rows):
    cs = ins.parse_rows(rows, UNDS)
    br = BrainState(cs, {"NIFTY": "NSE_INDEX|Nifty 50"}, None)
    assert backfill_today(br, tmp_path / "nope", "2026-10-19", {"x"}, {}, log=lambda *a: None) == 0
