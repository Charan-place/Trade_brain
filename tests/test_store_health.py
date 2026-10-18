import pyarrow.parquet as pq

from tbrain.brokers.upstox.normalize import empty_row
from tbrain.collector.health import Health
from tbrain.collector.store import CHAIN_SCHEMA, TICK_SCHEMA, ParquetStore


def test_tick_roundtrip(tmp_path):
    st = ParquetStore(tmp_path, TICK_SCHEMA, flush_s=0)
    r = empty_row(); r.update(recv_ns=1, instrument_key="K", src="market", ltp=1.5, vtt=10, bq1=650)
    st.add([r, dict(r, recv_ns=2)])
    p = st.flush()
    t = pq.read_table(p)
    assert t.num_rows == 2 and t.schema.equals(TICK_SCHEMA) and t["bq1"].to_pylist() == [650, 650]
    assert not list(tmp_path.rglob("*.tmp"))


def test_chain_coercion(tmp_path):
    st = ParquetStore(tmp_path, CHAIN_SCHEMA, flush_s=0)
    st.add([{"recv_ns": 1, "underlying": "NIFTY", "expiry": "2026-10-20", "strike": 25000, "oi": "123", "side": "CE", "pcr": None}])
    t = pq.read_table(st.flush())
    assert t["strike"].to_pylist() == [25000.0] and t["oi"].to_pylist() == [123.0]


def test_health_gaps_only_inside_market_hours():
    h = Health()
    s = 1_000_000_000
    h.observe([{"instrument_key": "A"}], 1 * s, in_hours=True)
    h.observe([{"instrument_key": "A"}], 5 * s, in_hours=True)     # 4s: fine
    h.observe([{"instrument_key": "B"}], 25 * s, in_hours=True)    # 20s gap
    h.observe([{"instrument_key": "B"}], 9999 * s, in_hours=False)  # after close: ignored
    h.observe([{"instrument_key": "B"}], 99999 * s, in_hours=True)  # next open: ignored (prev was out of hours)
    sm = h.summary()
    assert sm["gaps_over_10s"] == 1 and sm["max_gap_s"] == 20.0 and sm["keys_seen"] == 2
