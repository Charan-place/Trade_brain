import json
from datetime import datetime

import pytest

from tbrain.analytics import black76 as b
from tbrain.analytics.chain import analyze
from tbrain.brain.features import FeatureTracker
from tbrain.signals.engine import EngineConfig, SignalEngine
from tbrain.timeutil import IST

LOT, R = 65, 0.065
NOW = datetime(2026, 10, 20, 10, 30, tzinfo=IST)  # Tuesday 10:30
NOW_MS = NOW.timestamp() * 1000


def _chain(F, T, call_wall_far=True):
    legs, quotes = [], {}
    for K in range(F // 50 * 50 - 500, F // 50 * 50 + 551, 50):
        for kind in ("CE", "PE"):
            px = b.price(F, K, T, 0.12, R, kind)
            key = f"{kind}{K}"
            oi = 900_000 if (kind == "CE" and K == (F // 50 * 50 + 500 if call_wall_far else F // 50 * 50 + 50)) else 100_000
            legs.append({"key": key, "strike": float(K), "kind": kind, "ltp": px, "bid": px - 0.1, "ask": px + 0.1, "oi": oi})
            quotes[key] = {"bp1": px - 0.1, "ap1": px + 0.1, "aq1": 10 * LOT, "ltp": px}
    ch = analyze(legs, T, R, F)
    ch["expiry"] = "2026-10-27"
    return ch, quotes


def _ctx(spot=25100.0, wall_far=True):
    T = 7 / 365
    ch, quotes = _chain(int(spot), T, wall_far)
    bars = []
    base = NOW_MS - 20 * 60_000
    p = spot - 40
    for i in range(20):
        o = p; c = p + 2 + (10 if i == 19 else 0); bars.append([base + i * 60_000, o, c + 3, o - 3, c, 0]); p = c
    return {"live": True, "feed_age": 0.4, "spot": spot, "fut": spot + 20, "vwap": spot - 10,
            "flow": {"obi": 0.3, "resting_imbalance": 0.2}, "big_net": 6500.0,
            "big_tape": [{"t_ms": NOW_MS - 60_000, "symbol": "NIFTY FUT 27 OCT 26", "side": "BUY", "qty": 3250, "lots": 50,
                          "px": spot + 18, "bullish": True}],
            "bars": bars, "chains": {"2026-10-27": ch}, "lot": LOT, "r": R, "quotes": quotes}


def _tracker():
    tr = FeatureTracker()
    t0 = NOW_MS / 1000 - 1200
    for i in range(1201):
        tr.sample("NIFTY", t0 + i, {"cvd": 100.0 * i, "fut": 25060 + 0.05 * i, "fut_oi": 1e6 + 50 * i,
                                    "pe_oi_near": 5e5 + 300 * i, "ce_oi_near": 5e5 + 100 * i, "straddle": 300.0})
    return tr


def test_bullish_confluence_issues_call_with_ordered_levels(tmp_path):
    eng = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    ctx = _ctx()
    blk = eng.step(NOW_MS, {"NIFTY": ctx}, _tracker())
    d = blk["diagnostics"]["NIFTY"]
    assert d["gates"] == [] and d["sides"]["bullish"]["score"] == 7 and d["sides"]["bullish"]["trigger"]
    (c,) = blk["calls"]
    assert c["action"] == "BUY CE" and c["symbol"].startswith("NIFTY 25100 CE 2026-10-27")
    assert c["u_sl"] < c["u_entry"] < c["u_t1"] < c["u_t2"]
    assert c["p_sl"] < c["entry"] < c["p_t1"] < c["p_t2"] and c["rr_net"] >= 1.5
    assert c["label"].startswith("EXPERIMENTAL") and len(c["reasons"]) == 7
    assert c["evidence"] and "50 lots" in c["evidence"][0] and "NIFTY FUT" in c["evidence"][0]
    lines = (tmp_path / "calls.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["event"] == "issue"


def test_tracking_t1_breakeven_then_t2_and_scoreboard(tmp_path):
    eng = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    tr = _tracker()
    ctx = _ctx()
    (c,) = eng.step(NOW_MS, {"NIFTY": ctx}, tr)["calls"]
    key = c["instrument_key"]
    # price runs to T1: stop moves to breakeven
    ctx2 = dict(ctx, spot=c["u_t1"] + 1)
    ctx2["quotes"] = dict(ctx["quotes"]); ctx2["quotes"][key] = {"bp1": c["p_t1"], "ap1": c["p_t1"] + 0.2, "ltp": c["p_t1"]}
    eng.step(NOW_MS + 300_000, {"NIFTY": ctx2}, tr)
    c = eng.calls[c["id"]]
    assert c["status"] == "t1" and c["t1_hit"] and c["u_sl_active"] == c["u_entry"] and c["pnl_per_lot"] > 0
    # then T2
    ctx3 = dict(ctx2, spot=c["u_t2"] + 1)
    ctx3["quotes"] = dict(ctx2["quotes"]); ctx3["quotes"][key] = {"bp1": c["p_t2"], "ap1": c["p_t2"] + 0.2}
    eng.step(NOW_MS + 600_000, {"NIFTY": ctx3}, tr)
    c = eng.calls[c["id"]]
    assert c["status"] == "closed" and c["outcome"] == "T2" and c["r_now"] > 1.5
    sb = eng.scoreboard()
    assert sb["closed"] == 1 and sb["hit_t1"] == 1.0 and sb["by_outcome"]["T2"] == 1
    # restart: calls reload from disk
    again = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    assert again.calls[c["id"]]["outcome"] == "T2"


def test_stop_loss_path(tmp_path):
    eng = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    tr = _tracker(); ctx = _ctx()
    (c,) = eng.step(NOW_MS, {"NIFTY": ctx}, tr)["calls"]
    ctx2 = dict(ctx, spot=c["u_sl"] - 1)
    ctx2["quotes"] = dict(ctx["quotes"]); ctx2["quotes"][c["instrument_key"]] = {"bp1": c["p_sl"] - 1}
    eng.step(NOW_MS + 120_000, {"NIFTY": ctx2}, tr)
    c = eng.calls[c["id"]]
    assert c["outcome"] == "SL" and c["pnl_per_lot"] < 0 and c["r_now"] < -0.8


def test_gates_block_calls(tmp_path):
    tr = _tracker()
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    late = datetime(2026, 10, 20, 15, 0, tzinfo=IST).timestamp() * 1000
    blk = eng.step(late, {"NIFTY": _ctx()}, tr)
    assert not blk["calls"] and any("outside entry window" in g for g in blk["diagnostics"]["NIFTY"]["gates"])
    blk = eng.step(NOW_MS, {"NIFTY": dict(_ctx(), feed_age=9.0)}, tr)
    assert not blk["calls"] and any("stale" in g for g in blk["diagnostics"]["NIFTY"]["gates"])
    blk = eng.step(NOW_MS, {"NIFTY": dict(_ctx(), live=False)}, tr)
    assert not blk["calls"]


def test_cooldown_and_one_open_call(tmp_path):
    tr = _tracker()
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    eng.step(NOW_MS, {"NIFTY": _ctx()}, tr)
    ctx = _ctx(); ctx["bars"] = ctx["bars"][:-1] + [[NOW_MS - 60_000, *ctx["bars"][-1][1:]]]
    blk = eng.step(NOW_MS + 60_000, {"NIFTY": ctx}, tr)
    assert len(blk["calls"]) == 1
    g = blk["diagnostics"]["NIFTY"]["gates"]
    assert any("cooldown" in x for x in g) and any("already open" in x for x in g)


def test_oi_wall_before_target_blocks(tmp_path):
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    blk = eng.step(NOW_MS, {"NIFTY": _ctx(spot=25130.0, wall_far=False)}, _tracker())  # call wall 25150 < T1
    assert not blk["calls"] and "wall" in (blk["diagnostics"]["NIFTY"]["blocked"] or "")
