"""Black-76 on the forward (Nifty/Sensex index options are European, cash-settled).
C = e^{-rT}[F N(d1) - K N(d2)],  P = e^{-rT}[K N(-d2) - F N(-d1)],  d1 = [ln(F/K) + s^2 T/2]/(s sqrt T), d2 = d1 - s sqrt T
Greeks are w.r.t. the forward. vega per 1 vol point (0.01), theta per calendar day.
"""
from __future__ import annotations

import math

_SQ2 = math.sqrt(2.0)
_ISQ2PI = 1.0 / math.sqrt(2.0 * math.pi)


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQ2))


def npdf(x: float) -> float:
    return _ISQ2PI * math.exp(-0.5 * x * x)


def price(F: float, K: float, T: float, sigma: float, r: float, kind: str) -> float:
    df = math.exp(-r * max(T, 0.0))
    if T <= 0 or sigma <= 0:
        return df * (max(F - K, 0.0) if kind == "CE" else max(K - F, 0.0))
    st = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    if kind == "CE":
        return df * (F * ncdf(d1) - K * ncdf(d2))
    return df * (K * ncdf(-d2) - F * ncdf(-d1))


def greeks(F: float, K: float, T: float, sigma: float, r: float, kind: str) -> dict:
    if T <= 0 or sigma <= 0:
        return {"delta": None, "gamma": None, "vega": None, "theta": None}
    df = math.exp(-r * T)
    sqt = math.sqrt(T)
    st = sigma * sqt
    d1 = (math.log(F / K) + 0.5 * st * st) / st
    d2 = d1 - st
    pdf = npdf(d1)
    if kind == "CE":
        delta = df * ncdf(d1)
        theta_y = -df * F * pdf * sigma / (2 * sqt) + r * df * (F * ncdf(d1) - K * ncdf(d2))
    else:
        delta = -df * ncdf(-d1)
        theta_y = -df * F * pdf * sigma / (2 * sqt) + r * df * (K * ncdf(-d2) - F * ncdf(-d1))
    return {"delta": delta, "gamma": df * pdf / (F * st), "vega": df * F * pdf * sqt / 100.0, "theta": theta_y / 365.0}


def implied_vol(px: float, F: float, K: float, T: float, r: float, kind: str,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-7, max_iter: int = 200) -> float | None:
    """Bisection (monotone in sigma, always converges). None if price is outside no-arbitrage bounds."""
    if px is None or px <= 0 or T <= 0 or F <= 0 or K <= 0:
        return None
    df = math.exp(-r * T)
    intrinsic = df * (max(F - K, 0.0) if kind == "CE" else max(K - F, 0.0))
    upper = df * (F if kind == "CE" else K)
    if px <= intrinsic + 1e-12 or px >= upper:
        return None
    f_lo = price(F, K, T, lo, r, kind) - px
    f_hi = price(F, K, T, hi, r, kind) - px
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = price(F, K, T, mid, r, kind) - px
        if abs(f_mid) < tol or (hi - lo) < 1e-10:
            return mid
        if f_mid > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def forward_from_parity(pairs: list[tuple[float, float, float]], T: float, r: float) -> float | None:
    """pairs = [(K, call_mid, put_mid), ...] near ATM.  F = K + e^{rT}(C - P), averaged."""
    vals = [K + math.exp(r * T) * (c - p) for K, c, p in pairs if c and p and c > 0 and p > 0]
    return sum(vals) / len(vals) if vals else None
