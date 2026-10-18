"""Option-chain analytics for one underlying + expiry from the latest per-instrument rows.

Inputs are plain dicts so this works identically for live data, replays and tests.
leg = {"strike", "kind" CE/PE, "ltp", "bid", "ask", "oi", "oi_base", "ltp_base", "vtt", "iv_vendor", "delta_vendor"}
"""
from __future__ import annotations

import math

from tbrain.analytics.black76 import forward_from_parity, greeks, implied_vol

MAX_SPREAD_FOR_MID = 0.25  # if (ask-bid)/mid > 25%, the mid is not trusted -> use LTP


def mid_price(leg: dict) -> float | None:
    b, a, l = leg.get("bid"), leg.get("ask"), leg.get("ltp")
    if b and a and a >= b > 0:
        m = 0.5 * (a + b)
        if (a - b) / m <= MAX_SPREAD_FOR_MID:
            return m
    return l if l and l > 0 else None


def spread_pct(leg: dict) -> float | None:
    b, a = leg.get("bid"), leg.get("ask")
    if b and a and a >= b > 0:
        return (a - b) / (0.5 * (a + b)) * 100.0
    return None


def buildup(price_chg: float | None, oi_chg: float | None) -> str | None:
    """Classic OI x price read. Needs both changes vs the SAME baseline."""
    if price_chg is None or oi_chg is None or (price_chg == 0 and oi_chg == 0):
        return None
    if oi_chg > 0:
        return "long build-up" if price_chg > 0 else "short build-up"
    if oi_chg < 0:
        return "short covering" if price_chg > 0 else "long unwinding"
    return None


def max_pain(strikes: list[float], ce_oi: dict, pe_oi: dict) -> float | None:
    if not strikes:
        return None
    best, best_s = None, None
    for s in strikes:
        pain = sum(ce_oi.get(k, 0) * max(s - k, 0) + pe_oi.get(k, 0) * max(k - s, 0) for k in strikes)
        if best is None or pain < best:
            best, best_s = pain, s
    return best_s


def analyze(legs: list[dict], T: float, r: float, spot: float | None) -> dict:
    by_k: dict[float, dict] = {}
    for leg in legs:
        by_k.setdefault(leg["strike"], {})[leg["kind"]] = leg
    strikes = sorted(by_k)
    if not strikes:
        return {"strikes": []}
    ref = spot or strikes[len(strikes) // 2]
    near = sorted(strikes, key=lambda k: abs(k - ref))[:3]
    pairs = [(k, mid_price(by_k[k].get("CE", {})), mid_price(by_k[k].get("PE", {}))) for k in near]
    F = forward_from_parity([p for p in pairs if p[1] and p[2]], T, r) or spot
    if not F:
        return {"strikes": []}
    atm = min(strikes, key=lambda k: abs(k - F))

    rows, ce_oi, pe_oi = [], {}, {}
    for k in strikes:
        row = {"strike": k, "atm": k == atm,
               "call_moneyness": "ATM" if k == atm else ("ITM" if k < F else "OTM"),
               "put_moneyness": "ATM" if k == atm else ("ITM" if k > F else "OTM")}
        for kind in ("CE", "PE"):
            leg = by_k[k].get(kind)
            if not leg:
                row[kind.lower()] = None
                continue
            px = mid_price(leg)
            iv = implied_vol(px, F, k, T, r, kind) if px else None
            g = greeks(F, k, T, iv, r, kind) if iv else {}
            oi, oi_b = leg.get("oi"), leg.get("oi_base")
            lt, lt_b = leg.get("ltp"), leg.get("ltp_base")
            d_oi = (oi - oi_b) if oi is not None and oi_b is not None else None
            d_px = (lt - lt_b) if lt is not None and lt_b is not None else None
            (ce_oi if kind == "CE" else pe_oi)[k] = oi or 0
            row[kind.lower()] = {
                "key": leg.get("key"), "ltp": lt, "mid": px, "bid": leg.get("bid"), "ask": leg.get("ask"),
                "spread_pct": spread_pct(leg), "oi": oi, "d_oi": d_oi, "d_px": d_px, "buildup": buildup(d_px, d_oi),
                "vol": leg.get("vtt"), "iv": iv, "iv_vendor": leg.get("iv_vendor"),
                "delta": g.get("delta"), "gamma": g.get("gamma"), "theta": g.get("theta"), "vega": g.get("vega"),
                "delta_vendor": leg.get("delta_vendor"), "tbq": leg.get("tbq"), "tsq": leg.get("tsq"),
            }
        rows.append(row)

    def iv_of(k, kind):
        x = (by_k.get(k) and next((r[kind.lower()] for r in rows if r["strike"] == k), None)) or {}
        return x.get("iv") if x else None

    atm_row = next(r for r in rows if r["strike"] == atm)
    atm_ivs = [x["iv"] for x in (atm_row.get("ce"), atm_row.get("pe")) if x and x.get("iv")]
    vend = [x["iv_vendor"] for x in (atm_row.get("ce"), atm_row.get("pe")) if x and x.get("iv_vendor")]

    def nearest_delta(kind, target, tol=0.08):
        """IV of the leg whose delta is closest to target; None unless one is genuinely near it (|d - target| <= tol)."""
        cands = [(abs(r[kind]["delta"] - target), r[kind]["iv"]) for r in rows
                 if r.get(kind) and r[kind].get("delta") is not None and r[kind].get("iv")]
        best = min(cands) if cands else None
        return best[1] if best and best[0] <= tol else None

    p25, c25 = nearest_delta("pe", -0.25), nearest_delta("ce", 0.25)
    straddle = None
    if atm_row.get("ce") and atm_row.get("pe") and atm_row["ce"]["mid"] and atm_row["pe"]["mid"]:
        straddle = atm_row["ce"]["mid"] + atm_row["pe"]["mid"]
    tot_ce, tot_pe = sum(ce_oi.values()), sum(pe_oi.values())
    vol_ce = sum((r["ce"] or {}).get("vol") or 0 for r in rows)
    vol_pe = sum((r["pe"] or {}).get("vol") or 0 for r in rows)
    return {
        "forward": F, "atm": atm, "T_years": T, "strikes": strikes, "rows": rows,
        "atm_iv": sum(atm_ivs) / len(atm_ivs) if atm_ivs else None,
        "atm_iv_vendor": sum(vend) / len(vend) if vend else None,
        "rr25": (p25 - c25) if p25 is not None and c25 is not None else None,  # +ve = puts richer
        "straddle": straddle,
        "pcr_oi": tot_pe / tot_ce if tot_ce else None,
        "pcr_vol": vol_pe / vol_ce if vol_ce else None,
        "max_pain": max_pain(strikes, ce_oi, pe_oi),
        "call_wall": max(ce_oi, key=ce_oi.get) if any(ce_oi.values()) else None,
        "put_wall": max(pe_oi, key=pe_oi.get) if any(pe_oi.values()) else None,
        "partial_chain": True,  # only recorded strikes (ATM +/- n) -> PCR / max pain are local, not whole-chain
    }
