from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as pb

from tbrain.brokers.upstox.feed import decode_frame
from tbrain.brokers.upstox.normalize import normalize, parse_market_info


def _frame():
    fr = pb.FeedResponse()
    fr.type = pb.Type.Value("live_feed")
    fr.currentTs = 1760850000123
    m = fr.feeds["NSE_FO|1"].fullFeed.marketFF
    m.ltpc.ltp, m.ltpc.ltt, m.ltpc.ltq, m.ltpc.cp = 123.45, 1760850000000, 65, 120.0
    for i in range(5):
        q = m.marketLevel.bidAskQuote.add()
        q.bidQ, q.bidP, q.askQ, q.askP = 100 + i, 123.4 - i * 0.05, 200 + i, 123.5 + i * 0.05
    m.optionGreeks.delta, m.optionGreeks.gamma, m.optionGreeks.theta, m.optionGreeks.vega = 0.52, 0.0011, -12.3, 9.8
    m.atp, m.vtt, m.oi, m.iv, m.tbq, m.tsq = 121.1, 987654, 4321000, 0.142, 55000, 61000
    ix = fr.feeds["NSE_INDEX|Nifty 50"].fullFeed.indexFF
    ix.ltpc.ltp, ix.ltpc.cp = 25012.35, 24990.1
    return fr.SerializeToString()


def test_full_and_index_rows_from_real_proto():
    msg = decode_frame(_frame())
    assert msg["type"] == "live_feed"
    rows = {r["instrument_key"]: r for r in normalize(msg, 42)}
    o = rows["NSE_FO|1"]
    assert o["src"] == "market" and o["recv_ns"] == 42 and o["cur_ts_ms"] == 1760850000123
    assert o["ltp"] == 123.45 and o["ltq"] == 65 and o["ltt_ms"] == 1760850000000 and o["cp"] == 120.0
    assert o["vtt"] == 987654 and isinstance(o["vtt"], int)  # int64 arrives as str from MessageToDict
    assert o["oi"] == 4321000 and o["tbq"] == 55000 and o["tsq"] == 61000
    assert abs(o["iv"] - 0.142) < 1e-9 and abs(o["delta"] - 0.52) < 1e-9 and o["theta"] == -12.3
    assert o["bp1"] == 123.4 and o["bq1"] == 100 and o["ap5"] == 123.7 and o["aq5"] == 204
    assert o["rho"] is None  # zero/absent fields are omitted by proto3 -> None
    i = rows["NSE_INDEX|Nifty 50"]
    assert i["src"] == "index" and i["ltp"] == 25012.35 and i["bp1"] is None


def test_first_level_with_greeks():
    fr = pb.FeedResponse()
    fr.type = pb.Type.Value("live_feed")
    f = fr.feeds["NSE_FO|2"].firstLevelWithGreeks
    f.ltpc.ltp = 10.5
    f.firstDepth.bidP, f.firstDepth.bidQ, f.firstDepth.askP, f.firstDepth.askQ = 10.45, 650, 10.55, 1300
    f.optionGreeks.delta, f.oi, f.vtt, f.iv = -0.2, 1000, 5000, 0.2
    (r,) = normalize(decode_frame(fr.SerializeToString()), 1)
    assert r["src"] == "first_level" and r["bp1"] == 10.45 and r["aq1"] == 1300 and r["delta"] == -0.2 and r["vtt"] == 5000


def test_market_info_and_non_feed_types():
    fr = pb.FeedResponse()
    fr.type = pb.Type.Value("market_info")
    fr.marketInfo.segmentStatus["NSE_FO"] = pb.MarketStatus.Value("NORMAL_OPEN")
    fr.marketInfo.segmentStatus["BSE_FO"] = pb.MarketStatus.Value("NORMAL_CLOSE")
    msg = decode_frame(fr.SerializeToString())
    assert normalize(msg, 1) == []
    assert parse_market_info(msg) == {"NSE_FO": "NORMAL_OPEN", "BSE_FO": "NORMAL_CLOSE"}


def test_initial_feed_has_no_type_key_and_is_parsed():
    """Regression from the first live probe: enum default initial_feed(0) is omitted by MessageToDict."""
    fr = pb.FeedResponse()
    fr.type = pb.Type.Value("initial_feed")
    ix = fr.feeds["NSE_INDEX|Nifty 50"].fullFeed.indexFF
    ix.ltpc.ltp, ix.ltpc.cp = 23146.15, 22997.35
    b = ix.marketOHLC.ohlc.add(); b.interval, b.open, b.close, b.ts = "I1", 23146.15, 23146.15, 1792144800000
    m = fr.feeds["NSE_FO|48704"].fullFeed.marketFF
    m.ltpc.ltp, m.vtt, m.oi, m.rp = 23113.0, 2747095, 17009135.0, 23104.4
    q = m.marketLevel.bidAskQuote.add(); q.bidQ, q.bidP, q.askQ, q.askP = 6305, 23112.0, 455, 23113.0
    msg = decode_frame(fr.SerializeToString())
    assert "type" not in msg  # this is the live behaviour
    rows = {r["instrument_key"]: r for r in normalize(msg, 1)}
    assert rows["NSE_INDEX|Nifty 50"]["ltp"] == 23146.15 and rows["NSE_INDEX|Nifty 50"]["i1_close"] == 23146.15
    f = rows["NSE_FO|48704"]
    assert f["vtt"] == 2747095 and f["bq1"] == 6305 and f["rp"] == 23104.4


def test_live_probe_sample_shape():
    """Exact dict shape captured from the 2026-10-19 live probe (SENSEX future, values abbreviated)."""
    msg = {"feeds": {"BSE_FO|864571": {"fullFeed": {"marketFF": {
        "ltpc": {"ltp": 74650.0, "ltt": "1792145377263", "ltq": "20", "cp": 74114.75},
        "marketLevel": {"bidAskQuote": [{"bidQ": "20", "bidP": 74650.0, "askQ": "20", "askP": 74662.1}]},
        "optionGreeks": {}, "marketOHLC": {"ohlc": [{"interval": "1d", "close": 74650.0, "vol": "33680", "ts": "1792089000000"},
                                                    {"interval": "I1", "open": 74665.0, "high": 74665.0, "low": 74650.0,
                                                     "close": 74650.0, "vol": "80", "ts": "1792145340000"}]},
        "atp": 74565.12, "vtt": "33680", "oi": 94500.0, "tbq": 3340.0, "tsq": 3400.0, "rp": 74647.35}},
        "requestMode": "full_d5"}}, "currentTs": "1792349498485"}
    (r,) = normalize(msg, 7)
    assert r["src"] == "market" and r["ltq"] == 20 and r["vtt"] == 33680 and r["i1_vol"] == 80 and r["i1_open"] == 74665.0
    assert r["delta"] is None and r["cur_ts_ms"] == 1792349498485
