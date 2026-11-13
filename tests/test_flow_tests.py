import numpy as np

from tbrain.research import flow_tests as F

MIN = 60_000


def _row(und, day, k, spot, bull=True, bear=False, clue="vwap", t0=1_800_000_000_000):
    return {"und": und, "day": day, "t_ms": t0 + k * MIN, "spot": spot,
            "sides": {"bullish": {"clues": {clue: bull}}, "bearish": {"clues": {clue: bear}}}}


def test_clue_trades_enter_next_minute_exit_15_later_and_respect_cooldown():
    rows = [_row("NIFTY", "2026-11-16", k, 100.0 + k) for k in range(60)]
    tr = F.clue_trades(rows, "vwap", "NIFTY", "2026-11-16")["2026-11-16"]
    pts = [p for p, _ in tr]
    assert all(abs(p - 15.0) < 1e-9 for p in pts)                 # entry t+1, exit +15 min, long on rising prices
    assert len(tr) == 3                                           # minutes 0,15,30 (cooldown); minute 45 has no exit row, skipped
    assert tr[0][1] == 101.0                                      # entry is the NEXT row's spot, never the signal row's


def test_conflicting_or_missing_clues_never_fire_and_days_before_start_are_untouched():
    rows = [_row("NIFTY", "2026-11-13", k, 100.0 + k) for k in range(40)]
    assert F.clue_trades(rows, "vwap", "NIFTY", "2026-11-16") == {}
    both = [_row("NIFTY", "2026-11-16", k, 100.0 + k, bull=True, bear=True) for k in range(40)]
    assert F.clue_trades(both, "vwap", "NIFTY", "2026-11-16") == {}
    assert F._fires({"sides": {}}, "vwap") == 0


def test_family3_refuses_to_compute_statistics_below_the_sample_floor():
    rows = [_row("NIFTY", f"2026-11-{16 + d}", k, 100.0 + k) for d in range(3) for k in range(40)]
    out = F.run_family3(rows)
    assert out["status"] == "insufficient_data" and out["forward_days"] == 3 and out["results"] == []


def _call(day, pnl, cost=72.0):
    return {"status": "closed", "day": day, "pnl_per_lot": pnl, "cost_est_per_lot": cost}


def test_gate_a_rejects_flat_cost_calls_and_concentrated_profit_and_accepts_a_clean_book():
    days = [f"2026-12-{d:02d}" for d in range(1, 29)] + ["2027-01-01", "2027-01-02"]
    clean = [_call(d, p) for d in days for p in (150.0, 90.0, -60.0, 120.0, 30.0)]          # 150 calls, 30 days, steady
    assert F.gate_a(clean, "2026-12-01")["pass"] is True
    flat = [_call(d, p, cost=40.0) for d in days for p in (150.0, 90.0, -60.0, 120.0, 30.0)]
    assert F.gate_a(flat, "2026-12-01")["checks"]["2 real-cost model in force on >=95% of calls"] is False
    lumpy = [_call(d, -10.0) for d in days for _ in range(5)] + [_call(days[0], 100_000.0)]
    g = F.gate_a(lumpy, "2026-12-01")
    assert g["pass"] is False and g["checks"]["5 no single day > 40% of total profit"] is False
    assert F.gate_a([], "2026-12-01")["pass"] is False
