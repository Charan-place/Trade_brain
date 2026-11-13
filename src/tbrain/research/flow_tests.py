"""Family 3 (forward order-flow clue test) and Gate A (paper-trading gate). See docs/RESEARCH_PREREGISTRATION.md.

Input is the per-minute `features` collection exported to JSON (one doc per index per minute). No credentials are read here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from .edge_tests import HURDLE_BP, Result, evaluate, holm

FORWARD_START = "2026-11-16"          # only days on or after this count toward any verdict
MIN_DAYS = 60                          # no statistics before this many forward trading days
HORIZON_MIN = 15
COOLDOWN_MIN = 15
# "volatility" is true on both sides at once (it is not directional), so it cannot be tested as long/short; see the 2026-11-13 amendment.
CLUES = ("vwap", "aggression", "fut_oi", "big_players", "order_book", "writers")
MS_MIN = 60_000


def load_features(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text())


def _series(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r.get("spot") and r.get("t_ms") and r.get("und") and r.get("day"):
            out.setdefault((r["und"], r["day"]), []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["t_ms"])
    return out


def _fires(row: dict, clue: str) -> int:
    s = row.get("sides") or {}
    bull = ((s.get("bullish") or {}).get("clues") or {}).get(clue)
    bear = ((s.get("bearish") or {}).get("clues") or {}).get(clue)
    if bull is None or bear is None:
        return 0
    return 1 if (bull and not bear) else -1 if (bear and not bull) else 0


def _at(rows: list[dict], t_ms: float, tol_ms: float = 60_000):
    """Row nearest to t_ms within tolerance, else None (rows sorted by t_ms)."""
    ts = np.fromiter((r["t_ms"] for r in rows), float, len(rows))
    i = int(np.abs(ts - t_ms).argmin())
    return rows[i] if abs(ts[i] - t_ms) <= tol_ms else None


def clue_trades(rows: list[dict], clue: str, und: str, since: str = "") -> dict:
    """{day: [(points, entry_price)]} for one clue on one index; days before `since` are never touched."""
    out = {}
    for (u, day), seq in _series(rows).items():
        if u != und or day < since:
            continue
        next_ok = 0.0
        for k, r in enumerate(seq[:-1]):
            d = _fires(r, clue)
            if d == 0 or r["t_ms"] < next_ok:
                continue
            entry = seq[k + 1]
            if entry["t_ms"] - r["t_ms"] > 90_000:
                continue
            ex = _at(seq, entry["t_ms"] + HORIZON_MIN * MS_MIN)
            if ex is None:
                continue
            out.setdefault(day, []).append((d * (ex["spot"] - entry["spot"]), entry["spot"]))
            next_ok = r["t_ms"] + COOLDOWN_MIN * MS_MIN
    return out


def forward_days(rows: list[dict], since: str = FORWARD_START) -> int:
    return len({day for (_, day) in _series(rows) if day >= since})


def run_family3(rows: list[dict], since: str = FORWARD_START, min_days: int = MIN_DAYS) -> dict:
    n_days = forward_days(rows, since)
    if n_days < min_days:
        return {"status": "insufficient_data", "forward_days": n_days, "needed": min_days, "results": []}
    res = []
    for und in ("NIFTY", "SENSEX"):
        for clue in CLUES:
            r = evaluate(f"{und} {clue}", clue_trades(rows, clue, und, since))
            if r:
                res.append(r)
    holm(res)
    for r in res:
        r.extra["PASS"] = bool(r.p_holm < 0.05 and (r.ci_bp[0] > 0 or r.ci_bp[1] < 0) and abs(r.mean_bp) >= HURDLE_BP)
    return {"status": "evaluated", "forward_days": n_days, "tests": len(res), "results": res}


def pipeline_check(rows: list[dict]) -> dict:
    """Counts only, no p-values: proves the labelling works on already-seen days without producing a result."""
    return {c: sum(len(v) for v in clue_trades(rows, c, u, "").values()) for u in ("NIFTY",) for c in CLUES}


# ---------------------------------------------------------------- Gate A
def gate_a(calls: list[dict], start_day: str, n_boot: int = 20_000, seed: int = 7) -> dict:
    cs = [c for c in calls if c.get("status") == "closed" and (c.get("day") or "") >= start_day and c.get("pnl_per_lot") is not None]
    days = sorted({c["day"] for c in cs})
    checks: dict[str, bool] = {}
    info = {"calls": len(cs), "days": len(days)}
    checks["1 sample: >=30 days and >=150 calls"] = len(days) >= 30 and len(cs) >= 150
    real = [c for c in cs if (c.get("cost_est_per_lot") or 0) >= 60]
    checks["2 real-cost model in force on >=95% of calls"] = bool(cs) and len(real) / len(cs) >= 0.95
    if cs:
        pnl = np.array([c["pnl_per_lot"] for c in cs], float)
        by_day = {d: np.array([c["pnl_per_lot"] for c in cs if c["day"] == d], float) for d in days}
        sums = np.array([v.sum() for v in by_day.values()]); cnts = np.array([len(v) for v in by_day.values()])
        rng = np.random.default_rng(seed)
        pick = rng.integers(0, len(days), (n_boot, len(days)))
        lo = float(np.percentile(sums[pick].sum(1) / cnts[pick].sum(1), 2.5))
        info.update(mean_per_call=float(pnl.mean()), ci_low=lo)
        checks["3 mean > 0 and day-block 95% lower bound > 0"] = bool(pnl.mean() > 0 and lo > 0)
        trimmed = np.sort(pnl)[:-3] if len(pnl) > 3 else np.array([])
        checks["4 mean > 0 after removing the 3 best calls"] = bool(len(trimmed) and trimmed.mean() > 0)
        total = pnl.sum()
        share = float(sums.max() / total) if total > 0 else 1.0
        info["best_day_share"] = share
        checks["5 no single day > 40% of total profit"] = bool(total > 0 and share <= 0.40)
    else:
        for k in ("3 mean > 0 and day-block 95% lower bound > 0", "4 mean > 0 after removing the 3 best calls", "5 no single day > 40% of total profit"):
            checks[k] = False
    return {"pass": all(checks.values()), "checks": checks, **info}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Family 3 forward flow-clue test, or Gate A on exported calls.")
    ap.add_argument("what", choices=("family3", "gate-a"))
    ap.add_argument("path", help="JSON export of the features (family3) or calls (gate-a) collection")
    ap.add_argument("--start", default=FORWARD_START)
    ap.add_argument("--mode", default=None, help="gate-a: only calls whose `mode` equals this profile")
    a = ap.parse_args(argv)
    data = json.loads(Path(a.path).read_text())
    if a.what == "family3":
        out = run_family3(data, a.start)
        if out["status"] != "evaluated":
            print(f"Forward days on file: {out['forward_days']} of {out['needed']} needed. No statistics computed (pre-registered).")
            return 0
        for r in out["results"]:
            print(f"{r.name:22s} n={r.n_trades:5d} {r.mean_bp:+6.2f}bp [{r.ci_bp[0]:+.2f},{r.ci_bp[1]:+.2f}] p={r.p_two_sided:.4f} holm={r.p_holm:.4f} {'PASS' if r.extra['PASS'] else ''}")
        print(f"PASS {sum(r.extra['PASS'] for r in out['results'])} of {out['tests']} (cumulative tests incl. families 1-2: {50 + out["tests"]})")
        return 0
    calls = [c for c in data if c.get("_coll", "calls") == "calls" and (a.mode is None or c.get("mode") == a.mode)]
    g = gate_a(calls, a.start)
    for k, v in g["checks"].items():
        print(f"{'OK  ' if v else 'FAIL'} {k}")
    print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in g.items() if k not in ("checks", "pass")})
    print("GATE A:", "PASS (1 lot, daily loss cap, no scaling)" if g["pass"] else "NOT PASSED - keep real money OFF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
