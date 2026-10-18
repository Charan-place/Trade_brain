import math

import pytest

from tbrain.analytics import black76 as b
from tbrain.analytics.chain import analyze, buildup, max_pain, mid_price


@pytest.mark.parametrize("kind", ["CE", "PE"])
@pytest.mark.parametrize("K", [22000.0, 23100.0, 24500.0])
def test_iv_roundtrip(kind, K):
    F, T, r = 23146.0, 7 / 365, 0.065
    for sig in (0.06, 0.13, 0.35):
        px = b.price(F, K, T, sig, r, kind)
        if px - b.price(F, K, T, 1e-6, r, kind) < 1e-6:
            continue  # no time value -> IV undefined
        assert abs(b.implied_vol(px, F, K, T, r, kind) - sig) < 1e-5


def test_put_call_parity_and_forward():
    F, K, T, r, s = 23146.0, 23100.0, 3 / 365, 0.065, 0.14
    c, p = b.price(F, K, T, s, r, "CE"), b.price(F, K, T, s, r, "PE")
    assert abs((c - p) - math.exp(-r * T) * (F - K)) < 1e-8
    assert abs(b.forward_from_parity([(K, c, p)], T, r) - F) < 1e-6


def test_greeks_against_finite_differences():
    F, K, T, r, s = 23146.0, 23200.0, 5 / 365, 0.065, 0.13
    g = b.greeks(F, K, T, s, r, "CE")
    h = 0.5
    d_num = (b.price(F + h, K, T, s, r, "CE") - b.price(F - h, K, T, s, r, "CE")) / (2 * h)
    gam = (b.price(F + h, K, T, s, r, "CE") - 2 * b.price(F, K, T, s, r, "CE") + b.price(F - h, K, T, s, r, "CE")) / h**2
    v_num = (b.price(F, K, T, s + 1e-4, r, "CE") - b.price(F, K, T, s - 1e-4, r, "CE")) / (2e-4) / 100
    dt = 1 / 365 / 24  # one hour, scaled to per-day
    th = (b.price(F, K, T - dt, s, r, "CE") - b.price(F, K, T, s, r, "CE")) * 24
    assert abs(g["delta"] - d_num) < 1e-5 and abs(g["gamma"] - gam) < 1e-6 and abs(g["vega"] - v_num) < 1e-4
    assert abs(g["theta"] - th) / abs(th) < 0.01


def test_iv_rejects_arbitrage_prices():
    assert b.implied_vol(10.0, 23146, 23000, 0.02, 0.065, "CE") is None  # below intrinsic
    assert b.implied_vol(0.0, 23146, 23000, 0.02, 0.065, "CE") is None


def test_buildup_and_max_pain():
    assert buildup(+5, +100) == "long build-up" and buildup(-5, +100) == "short build-up"
    assert buildup(+5, -100) == "short covering" and buildup(-5, -100) == "long unwinding"
    assert buildup(None, 10) is None
    ks = [100, 110, 120]
    assert max_pain(ks, {100: 0, 110: 10, 120: 50}, {100: 50, 110: 10, 120: 0}) == 110


def test_mid_price_falls_back_on_wide_spread():
    assert mid_price({"bid": 10, "ask": 10.5, "ltp": 9}) == 10.25
    assert mid_price({"bid": 1, "ask": 10, "ltp": 4}) == 4


def test_analyze_synthetic_chain_recovers_inputs():
    F, T, r = 23146.0, 4 / 365, 0.065
    legs = []
    for K in range(22900, 23401, 50):
        for kind in ("CE", "PE"):
            sig = 0.12 + 0.0000004 * (K - F) ** 2 + (0.01 if kind == "PE" else 0)  # smile, puts richer
            px = b.price(F, K, T, 0.12 + 0.0000004 * (K - F) ** 2, r, kind)
            legs.append({"strike": float(K), "kind": kind, "ltp": px, "bid": px - 0.05, "ask": px + 0.05,
                         "oi": 1000 + (K % 200), "vtt": 10})
    a = analyze(legs, T, r, spot=23140.0)
    assert abs(a["forward"] - F) < 0.5 and a["atm"] == 23150.0
    assert abs(a["atm_iv"] - 0.12) < 0.002
    atm = next(x for x in a["rows"] if x["atm"])
    assert atm["call_moneyness"] == "ATM"
    low = a["rows"][0]
    assert low["call_moneyness"] == "ITM" and low["put_moneyness"] == "OTM"
    assert a["straddle"] and a["pcr_oi"] and a["max_pain"] in a["strikes"]


def test_rr25_is_none_without_25_delta_strikes():
    F, T, r = 23146.0, 3 / 365, 0.065
    legs = [{"strike": float(K), "kind": k, "ltp": b.price(F, K, T, 0.11, r, k)} for K in (23100, 23150, 23200) for k in ("CE", "PE")]
    assert analyze(legs, T, r, spot=F)["rr25"] is None
