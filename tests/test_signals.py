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
    assert c["label"].startswith("EXPERIMENTAL") and len(c["reasons"]) == 8  # 7 clues + the trend filter
    assert c["reasons"][0].startswith("20-min trend up")
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


def test_today_lists_every_call_with_day_summary(tmp_path):
    eng = SignalEngine(EngineConfig(cooldown_min=0), store_dir=None, log=lambda *a: None)
    tr = _tracker()
    t = NOW_MS
    for i in range(15):  # issue -> stop out -> issue again: 15 calls in one day
        ctx = _ctx()
        ctx["bars"] = [[b[0] + i * 60_000, *b[1:]] for b in ctx["bars"]]
        blk = eng.step(t, {"NIFTY": ctx}, tr)
        c = next(x for x in blk["calls"] if x["status"] == "open")
        lose = dict(ctx, spot=c["u_sl"] - 1); lose["quotes"] = dict(ctx["quotes"]); lose["quotes"][c["instrument_key"]] = {"bp1": c["p_sl"]}
        eng.step(t + 30_000, {"NIFTY": lose}, tr)
        t += 60_000
    blk = eng.step(t, {"NIFTY": dict(_ctx(), live=False)}, tr)
    assert len(blk["calls"]) == 15                       # not truncated to the most recent few
    d = blk["today"]
    assert d["calls"] == 15 and d["closed"] == 15 and d["losses"] == 15 and d["win_rate"] == 0
    assert d["pnl_total_per_lot"] < 0 and "NIFTY" in d["by_index"]



def _falling(ctx):
    """Same bullish clues but the last 20 one-minute bars fall: a counter-trend long."""
    bars = ctx["bars"]
    n = len(bars)
    ctx["bars"] = [[b[0], b[1] + (n - i) * 6, b[2] + (n - i) * 6, b[3] + (n - i) * 6, b[4] + (n - i) * 6, 0] for i, b in enumerate(bars)]
    last = ctx["bars"][-1]; prev_hi = max(b[2] for b in ctx["bars"][-11:-1])
    ctx["bars"][-1] = [last[0], last[1], prev_hi + 5, last[3], prev_hi + 3, 0]  # still breaks the 10-min high
    return ctx


def test_v2_blocks_counter_trend_call():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    blk = eng.step(NOW_MS, {"NIFTY": _falling(_ctx())}, _tracker())
    d = blk["diagnostics"]["NIFTY"]
    assert not blk["calls"] and d["blocked"] and "trend" in d["blocked"]
    assert d["sides"]["bullish"]["required_ok"] is False
    off = SignalEngine(EngineConfig(trend_k=None), store_dir=None, log=lambda *a: None)  # switch off -> v1 behaviour
    assert off.step(NOW_MS, {"NIFTY": _falling(_ctx())}, _tracker())["calls"]


def test_v2_vwap_is_mandatory():
    ctx = _ctx(); ctx["vwap"] = ctx["fut"] + 5      # futures BELOW vwap, everything else bullish
    eng = SignalEngine(EngineConfig(min_score=5), store_dir=None, log=lambda *a: None)
    blk = eng.step(NOW_MS, {"NIFTY": ctx}, _tracker())
    assert not blk["calls"] and "VWAP" in (blk["diagnostics"]["NIFTY"]["blocked"] or "")


def test_v2_fut_oi_needs_fresh_positions():
    tr = FeatureTracker(); t0 = NOW_MS / 1000 - 1200
    for i in range(1201):  # price up but OI FALLING = short covering -> no longer a point
        tr.sample("NIFTY", t0 + i, {"cvd": 100.0 * i, "fut": 25060 + 0.05 * i, "fut_oi": 2e6 - 50 * i,
                                    "pe_oi_near": 5e5 + 300 * i, "ce_oi_near": 5e5 + 100 * i, "straddle": 300.0})
    eng = SignalEngine(EngineConfig(fut_oi_fresh=True), store_dir=None, log=lambda *a: None)
    d = eng.step(NOW_MS, {"NIFTY": _ctx()}, tr)["diagnostics"]["NIFTY"]["sides"]["bullish"]
    fo = next(c for c in d["conditions"] if c["key"] == "fut_oi")
    assert fo["ok"] is False and "short covering" in fo["text"] and d["score"] == 6


def test_v2_premium_stop_fires_before_index_stop(tmp_path):
    eng = SignalEngine(EngineConfig(premium_stop=True), store_dir=None, log=lambda *a: None)
    tr = _tracker(); ctx = _ctx()
    (c,) = eng.step(NOW_MS, {"NIFTY": ctx}, tr)["calls"]
    # index still ABOVE its stop, but the option collapsed (IV crush / spread) below the SL premium
    ctx2 = dict(ctx, spot=c["u_sl"] + 2); ctx2["quotes"] = dict(ctx["quotes"])
    ctx2["quotes"][c["instrument_key"]] = {"bp1": c["p_sl"] - 0.5, "ltp": c["p_sl"] - 0.5}
    eng.step(NOW_MS + 60_000, {"NIFTY": ctx2}, tr)
    c = eng.calls[c["id"]]
    assert c["outcome"] == "SL" and c["stop_via"] == "premium" and c["r_now"] > -1.2
    # a single bad bid with LTP still above the stop must NOT trigger it
    eng2 = SignalEngine(EngineConfig(premium_stop=True), store_dir=None, log=lambda *a: None)
    (d,) = eng2.step(NOW_MS, {"NIFTY": _ctx()}, _tracker())["calls"]
    ctx3 = dict(ctx, spot=d["u_sl"] + 2); ctx3["quotes"] = dict(ctx["quotes"])
    ctx3["quotes"][d["instrument_key"]] = {"bp1": d["p_sl"] - 0.5, "ltp": d["entry"]}
    eng2.step(NOW_MS + 60_000, {"NIFTY": ctx3}, _tracker())
    assert eng2.calls[d["id"]]["status"] == "open"


def test_v21_defaults_from_backtest():
    c = EngineConfig()
    assert c.require_vwap and c.trend_k == 1.5
    assert not c.premium_stop and not c.fut_oi_fresh and not c.regime_gate


def test_v2_correlated_call_is_flagged():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    tr = _tracker()
    for i in range(1201):
        tr.sample("SENSEX", NOW_MS / 1000 - 1200 + i, {"cvd": 100.0 * i, "fut": 82060 + 0.05 * i, "fut_oi": 1e6 + 50 * i,
                                                     "pe_oi_near": 5e5 + 300 * i, "ce_oi_near": 5e5 + 100 * i, "straddle": 900.0})
    blk = eng.step(NOW_MS, {"NIFTY": _ctx(), "SENSEX": _ctx(spot=82000.0)}, tr)
    calls = sorted(blk["calls"], key=lambda c: c["und"])
    assert len(calls) == 2 and any(c["correlated_with"] for c in calls)


def test_v2_correlation_block_switch():
    # off by default: both same-direction calls fire (just flagged, per the test above)
    eng_off = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    tr = _tracker()
    for i in range(1201):
        tr.sample("SENSEX", NOW_MS / 1000 - 1200 + i, {"cvd": 100.0 * i, "fut": 82060 + 0.05 * i, "fut_oi": 1e6 + 50 * i,
                                                     "pe_oi_near": 5e5 + 300 * i, "ce_oi_near": 5e5 + 100 * i, "straddle": 900.0})
    blk_off = eng_off.step(NOW_MS, {"NIFTY": _ctx(), "SENSEX": _ctx(spot=82000.0)}, tr)
    assert len(blk_off["calls"]) == 2

    # on: the second same-direction call is blocked (shadowed) instead of firing live
    eng_on = SignalEngine(EngineConfig(correlation_block=True), store_dir=None, log=lambda *a: None)
    tr2 = _tracker()
    for i in range(1201):
        tr2.sample("SENSEX", NOW_MS / 1000 - 1200 + i, {"cvd": 100.0 * i, "fut": 82060 + 0.05 * i, "fut_oi": 1e6 + 50 * i,
                                                      "pe_oi_near": 5e5 + 300 * i, "ce_oi_near": 5e5 + 100 * i, "straddle": 900.0})
    blk_on = eng_on.step(NOW_MS, {"NIFTY": _ctx(), "SENSEX": _ctx(spot=82000.0)}, tr2)
    assert len(blk_on["calls"]) == 1
    assert any("correlation block" in b for s in eng_on.shadows.values() for b in s.get("blocked_by") or [])


def test_v2_loss_pause_switch():
    eng = SignalEngine(EngineConfig(loss_pause_n=2, cooldown_min=0), store_dir=None, log=lambda *a: None)
    tr = _tracker(); t = NOW_MS
    for i in range(2):
        ctx = _ctx(); ctx["bars"] = [[b[0] + i * 60_000, *b[1:]] for b in ctx["bars"]]
        c = next(x for x in eng.step(t, {"NIFTY": ctx}, tr)["calls"] if x["status"] == "open")
        lose = dict(ctx, spot=c["u_sl"] - 1); lose["quotes"] = dict(ctx["quotes"]); lose["quotes"][c["instrument_key"]] = {"bp1": c["p_sl"]}
        eng.step(t + 30_000, {"NIFTY": lose}, tr); t += 60_000
    ctx = _ctx(); ctx["bars"] = [[b[0] + 2 * 60_000, *b[1:]] for b in ctx["bars"]]
    g = eng.step(t, {"NIFTY": ctx}, tr)["diagnostics"]["NIFTY"]["gates"]
    assert any("paused" in x for x in g)


def test_review_report(tmp_path):
    from tbrain.signals.review import review
    eng = SignalEngine(EngineConfig(cooldown_min=0), store_dir=tmp_path, log=lambda *a: None)
    tr = _tracker()
    (c,) = eng.step(NOW_MS, {"NIFTY": _ctx()}, tr)["calls"]
    ctx = _ctx(); lose = dict(ctx, spot=c["u_sl"] - 1); lose["quotes"] = dict(ctx["quotes"]); lose["quotes"][c["instrument_key"]] = {"bp1": c["p_sl"] - 3}
    eng.step(NOW_MS + 60_000, {"NIFTY": lose}, tr)
    txt = review(tmp_path / "calls.jsonl", "2026-10-20")
    assert "ALL" in txt and "by direction" in txt and "bullish" in txt and "stop quality" in txt


def test_shadow_tracks_blocked_candidate_to_outcome(tmp_path):
    eng = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    tr = _tracker()
    ctx = _falling(_ctx())                      # blocked by the trend filter
    blk = eng.step(NOW_MS, {"NIFTY": ctx}, tr)
    assert not blk["calls"] and len(eng.shadows) == 1
    (s,) = eng.shadows.values()
    assert s["shadow"] and any("trend" in b for b in s["blocked_by"]) and s["playbook"] == "breakout"
    assert "regime" in s and s["regime"]["label"] in ("trend", "range", "mixed", "unknown")
    eng.step(NOW_MS + 30_000, {"NIFTY": ctx}, tr)  # same bar: no duplicate shadow
    assert len(eng.shadows) == 1
    win = dict(ctx, spot=s["u_t2"] + 1); win["quotes"] = dict(ctx["quotes"]); win["quotes"][s["instrument_key"]] = {"bp1": s["p_t2"], "ltp": s["p_t2"]}
    eng.step(NOW_MS + 60_000, {"NIFTY": dict(ctx, spot=s["u_t1"] + 1, quotes={**ctx["quotes"], s["instrument_key"]: {"bp1": s["p_t1"], "ltp": s["p_t1"]}})}, tr)
    blk = eng.step(NOW_MS + 120_000, {"NIFTY": win}, tr)
    assert eng.shadows[s["id"]]["outcome"] == "T2"
    tf = blk["shadow"]["by_blocker"]["trend filter"]
    assert tf["n"] == 1 and tf["sum_r"] > 0          # the filter would have COST money on this one
    assert (tmp_path / "shadow.jsonl").exists() and not (tmp_path / "calls.jsonl").exists()
    again = SignalEngine(EngineConfig(), store_dir=tmp_path, log=lambda *a: None)
    assert again.shadows[s["id"]]["outcome"] == "T2"  # survives restarts


def test_regime_gate_switch():
    eng = SignalEngine(EngineConfig(regime_gate=True), store_dir=None, log=lambda *a: None)
    blk = eng.step(NOW_MS, {"NIFTY": _ctx()}, _tracker())   # fixture bars are a gentle drift -> not 'trend'
    d = blk["diagnostics"]["NIFTY"]
    if d["regime"]["label"] != "trend":
        assert not blk["calls"] and any("regime gate" in g for g in d["gates"]) and eng.shadows
