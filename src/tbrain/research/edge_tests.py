"""Pre-registered price-only edge tests (see docs/RESEARCH_PREREGISTRATION.md). Pure numpy/pyarrow.

Everything here follows the frozen protocol: chronological splits, next-bar-open entries, random-direction null,
day-block bootstrap, Holm correction over one family, and a loader that refuses FINAL-period data unless opened.
"""
from __future__ import annotations

import glob
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np

TRAIN_END, VALIDATE_END = "2024-06-30", "2025-06-30"
N_BARS = 375
HURDLE_BP = 1.0
MIN_BARS = 360


def idx(h: int, m: int) -> int:
    return h * 60 + m - 555


def split_of(day: str) -> str:
    return "train" if day <= TRAIN_END else "validate" if day <= VALIDATE_END else "final"


@dataclass
class Day:
    date: str
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray


def load_days(history_dir: Path, name: str, splits=("train", "validate"), open_final: bool = False) -> tuple[list[Day], dict]:
    """Daily 375-bar arrays (NaN where a minute is missing). FINAL-period days are never read unless open_final."""
    import pyarrow.parquet as pq
    if "final" in splits and not open_final:
        raise PermissionError("FINAL period is sealed; it may be opened once, after all decisions, with open_final=True")
    by: dict[str, dict] = {}
    for f in sorted(glob.glob(str(Path(history_dir) / name / "*.parquet"))):
        month = Path(f).stem
        if split_of(month + "-01") == "final" and not open_final:   # whole month lies in the sealed period
            continue
        t = pq.read_table(f, columns=["ts", "open", "high", "low", "close"]).to_pydict()
        for ts, o, h, l, c in zip(t["ts"], t["open"], t["high"], t["low"], t["close"]):
            d = ts[:10]
            if split_of(d) not in splits or (split_of(d) == "final" and not open_final):
                continue
            k = int(ts[11:13]) * 60 + int(ts[14:16]) - 555
            if 0 <= k < N_BARS:
                by.setdefault(d, {})[k] = (o, h, l, c)
    days, excluded = [], []
    for d in sorted(by):
        if len(by[d]) < MIN_BARS:
            excluded.append((d, len(by[d])))
            continue
        arr = np.full((4, N_BARS), np.nan)
        for k, v in by[d].items():
            arr[:, k] = v
        days.append(Day(d, arr[0], arr[1], arr[2], arr[3]))
    return days, {"days": len(days), "excluded_short_days": excluded}


# ---------------------------------------------------------------- trade generators: each returns {day: [(points, ref_price)]}
def _sgn(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def h1_trades(days: list[Day]) -> dict:
    out = {}
    for d in days:
        x = d.c[idx(9, 44)] / d.o[idx(9, 15)] - 1
        o15, c_end = d.o[idx(15, 0)], d.c[idx(15, 29)]
        if np.isnan(x) or np.isnan(o15) or np.isnan(c_end) or _sgn(x) == 0:
            continue
        out[d.date] = [(_sgn(x) * (c_end - o15), o15)]
    return out


def h2_trades(days: list[Day]) -> tuple[dict, dict]:
    to_day, to_last = {}, {}
    for prev, d in zip(days, days[1:]):
        pc = prev.c[~np.isnan(prev.c)][-1] if (~np.isnan(prev.c)).any() else np.nan
        r = d.o[idx(9, 15)] / pc - 1
        o945, c1514 = d.o[idx(9, 45)], d.c[idx(15, 14)]
        o15, c_end = d.o[idx(15, 0)], d.c[idx(15, 29)]
        if np.isnan(r) or _sgn(r) == 0:
            continue
        if not (np.isnan(o945) or np.isnan(c1514)):
            to_day[d.date] = [(_sgn(r) * (c1514 - o945), o945)]
        if not (np.isnan(o15) or np.isnan(c_end)):
            to_last[d.date] = [(_sgn(r) * (c_end - o15), o15)]
    return to_day, to_last


def h3_trades(days: list[Day]) -> dict:
    out = {}
    for d in days:
        hi, lo = np.nanmax(d.h[0:15]), np.nanmin(d.l[0:15])
        for t in range(idx(9, 30), idx(15, 0)):
            c = d.c[t]
            dr = 1 if c > hi else -1 if c < lo else 0
            if dr == 0 or np.isnan(c):
                continue
            entry, ex = d.o[t + 1], d.c[min(t + 30, idx(15, 14))]
            if not (np.isnan(entry) or np.isnan(ex)):
                out[d.date] = [(dr * (ex - entry), entry)]
            break
    return out


def h4_events(d: Day) -> list[tuple[int, int]]:
    """(bar index t, direction) for the engine's price-only skeleton, with the 10-minute cooldown."""
    ev, nxt = [], 0
    pc = np.concatenate(([np.nan], d.c[:-1]))
    tr = np.fmax.reduce([d.h - d.l, np.abs(d.h - pc), np.abs(d.l - pc)])
    for t in range(max(idx(9, 45), 11), idx(14, 45) + 1):
        if t < nxt:
            continue
        w_h, w_l = d.h[t - 10:t], d.l[t - 10:t]
        atr = np.nanmean(tr[max(1, t - 13):t + 1])
        mv = d.c[t] - d.c[max(0, t - 59)]
        if np.isnan(d.c[t]) or np.isnan(atr) or np.isnan(mv) or atr <= 0:
            continue
        dr = 1 if (d.c[t] > np.nanmax(w_h) and mv >= 1.5 * atr) else -1 if (d.c[t] < np.nanmin(w_l) and mv <= -1.5 * atr) else 0
        if dr:
            ev.append((t, dr))
            nxt = t + 10
    return ev


def h4_trades(days: list[Day], horizon: int) -> dict:
    out = {}
    for d in days:
        rows = []
        for t, dr in h4_events(d):
            if t + horizon > idx(15, 29):
                continue
            entry, ex = d.o[t + 1], d.c[t + horizon]
            if not (np.isnan(entry) or np.isnan(ex)):
                rows.append((dr * (ex - entry), entry))
        if rows:
            out[d.date] = rows
    return out


# ---------------------------------------------------------------- family 2: previous-day levels (docs/RESEARCH_PREREGISTRATION.md)
def _last(a: np.ndarray) -> float:
    ok = ~np.isnan(a)
    return float(a[ok][-1]) if ok.any() else float("nan")


def _prev_levels(days: list[Day], i: int):
    """(PDH, PDL, PDC) of the previous loaded day, or None for the first day of a split."""
    if i == 0:
        return None
    p = days[i - 1]
    return float(np.nanmax(p.h)), float(np.nanmin(p.l)), _last(p.c)


def _hold_trade(d: Day, t: int, direction: int, hold: int = 30):
    entry, ex = d.o[t + 1], d.c[min(t + hold, idx(15, 14))]
    return None if (np.isnan(entry) or np.isnan(ex)) else (direction * (ex - entry), entry)


def p2_p3_trades(days: list[Day]) -> tuple[dict, dict, dict]:
    """P2 break (continuation), P3 failed break (fade), and the P2 direction per day (used by P7)."""
    brk, fade, brk_dir = {}, {}, {}
    for i, d in enumerate(days):
        lv = _prev_levels(days, i)
        if lv is None:
            continue
        pdh, pdl, _ = lv
        b = f = None
        for t in range(idx(9, 30), idx(14, 45) + 1):
            c, pc = d.c[t], d.c[t - 1]
            if np.isnan(c) or np.isnan(pc):
                continue
            if b is None and ((c > pdh and pc <= pdh) or (c < pdl and pc >= pdl)):
                brk_dir[d.date] = 1 if c > pdh else -1
                b = _hold_trade(d, t, brk_dir[d.date])
            if f is None and ((d.h[t] > pdh and c < pdh) or (d.l[t] < pdl and c > pdl)):
                f = _hold_trade(d, t, -1 if (d.h[t] > pdh and c < pdh) else 1)
            if b is not None and f is not None:
                break
        if b:
            brk[d.date] = [b]
        if f:
            fade[d.date] = [f]
    return brk, fade, brk_dir


def p4_trades(days: list[Day]) -> dict:
    out = {}
    for i, d in enumerate(days):
        lv = _prev_levels(days, i)
        if lv is None or np.isnan(lv[2]):
            continue
        gap = d.o[idx(9, 15)] / lv[2] - 1
        entry, ex = d.o[idx(9, 45)], d.c[idx(12, 0) - 1]
        if np.isnan(gap) or abs(gap) < 0.003 or np.isnan(entry) or np.isnan(ex):
            continue
        out[d.date] = [(-_sgn(gap) * (ex - entry), entry)]
    return out


def _day_move(d: Day, direction: int):
    entry, ex = d.o[idx(9, 45)], d.c[idx(15, 14)]
    return None if (direction == 0 or np.isnan(entry) or np.isnan(ex)) else (direction * (ex - entry), entry)


def p5_p6_trades(days: list[Day]) -> tuple[dict, dict, dict]:
    t1, t5, clv = {}, {}, {}
    closes = [_last(d.c) for d in days]
    for i, d in enumerate(days):
        lv = _prev_levels(days, i)
        if lv is None:
            continue
        pdh, pdl, pdc = lv
        for store, back in ((t1, 2), (t5, 6)):
            if i >= back:
                r = _day_move(d, _sgn(pdc / closes[i - back] - 1))
                if r:
                    store[d.date] = [r]
        if pdh > pdl:
            loc = (pdc - pdl) / (pdh - pdl)
            r = _day_move(d, 1 if loc >= 0.8 else -1 if loc <= 0.2 else 0)
            if r:
                clv[d.date] = [r]
    return t1, t5, clv


def p7_trades(days: list[Day]) -> dict:
    """P2 break kept only when the five-day trend (PDC vs the close 5 days before it) points the same way."""
    brk, _, brk_dir = p2_p3_trades(days)
    closes = [_last(d.c) for d in days]
    return {d.date: brk[d.date] for i, d in enumerate(days)
            if d.date in brk and i >= 6 and _sgn(closes[i - 1] / closes[i - 6] - 1) == brk_dir[d.date]}


def run_family2(history_dir: Path, splits=("train", "validate"), open_final: bool = False, log=print) -> dict:
    out = {}
    for index in ("NIFTY", "SENSEX"):
        for split in splits:
            days, info = load_days(history_dir, index, splits=(split,), open_final=open_final)
            log(f"{index} {split}: {info['days']} days")
            brk, fade, _ = p2_p3_trades(days)
            t1, t5, clv = p5_p6_trades(days)
            gens = {"P2 PDH/PDL break": brk, "P3 PDH/PDL failed break fade": fade, "P4 gap fill": p4_trades(days),
                    "P5a prior-day trend": t1, "P5b five-day trend": t5, "P6 prior close location": clv,
                    "P7 trend-aligned break": p7_trades(days)}
            tests = [r for nm, tr in gens.items() if (r := evaluate(f"{index} {nm}", tr))]
            out[(index, split)] = {"tests": tests, "info": info}
    return out


# ---------------------------------------------------------------- statistics
@dataclass
class Result:
    name: str
    n_trades: int
    n_days: int
    mean_pts: float
    mean_bp: float
    ci_bp: tuple
    p_two_sided: float
    p_holm: float = float("nan")
    extra: dict = field(default_factory=dict)


def _flatten(tr: dict):
    days = sorted(tr)
    pts = [np.array([p for p, _ in tr[d]]) for d in days]
    bps = [np.array([p / r * 1e4 for p, r in tr[d]]) for d in days]
    return days, pts, bps


def evaluate(name: str, tr: dict, seed: int = 7, n_boot: int = 20_000, n_null: int = 10_000) -> Result | None:
    if not tr:
        return None
    days, pts, bps = _flatten(tr)
    all_pts, all_bp = np.concatenate(pts), np.concatenate(bps)
    n = len(all_bp)
    rng = np.random.default_rng(seed)
    sums = np.array([b.sum() for b in bps]); cnts = np.array([len(b) for b in bps])
    boot = np.empty(n_boot)
    for i in range(0, n_boot, 2000):
        k = min(2000, n_boot - i)
        pick = rng.integers(0, len(days), (k, len(days)))
        boot[i:i + k] = sums[pick].sum(1) / cnts[pick].sum(1)
    ci = (float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5)))
    obs, mag = float(all_bp.mean()), np.abs(all_bp)
    null = np.empty(n_null)
    for i in range(0, n_null, 500):
        k = min(500, n_null - i)
        null[i:i + k] = (rng.choice([-1.0, 1.0], (k, n)) * mag).mean(1)
    p = float(min(1.0, 2 * min((null >= obs).mean(), (null <= obs).mean())))
    return Result(name, n, len(days), float(all_pts.mean()), obs, ci, max(p, 1 / n_null))


def holm(results: list[Result]) -> None:
    order = sorted(range(len(results)), key=lambda i: results[i].p_two_sided)
    m, running = len(results), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * results[i].p_two_sided))
        results[i].p_holm = running


def label_shuffle_p(day_values: dict, high_days: set, seed: int = 7, n: int = 1000) -> dict:
    """Difference of mean per-day value between high-regime and other days, versus shuffled regime labels."""
    days = sorted(day_values)
    v = np.array([np.mean(day_values[d]) for d in days]); hi = np.array([d in high_days for d in days])
    if hi.sum() < 20 or (~hi).sum() < 20:
        return {"diff": float("nan"), "p": float("nan"), "n_high": int(hi.sum()), "n_low": int((~hi).sum())}
    obs = v[hi].mean() - v[~hi].mean()
    rng = np.random.default_rng(seed)
    null = np.empty(n)
    for i in range(n):
        s = rng.permutation(hi)
        null[i] = v[s].mean() - v[~s].mean()
    return {"diff": float(obs), "p": float(min(1.0, 2 * min((null >= obs).mean(), (null <= obs).mean()))),
            "n_high": int(hi.sum()), "n_low": int((~hi).sum())}


def high_vol_days(days: list[Day]) -> set:
    closes = np.array([d.c[~np.isnan(d.c)][-1] for d in days])
    ret = np.diff(np.log(closes))
    vols, hi = [], set()
    for i in range(20, len(ret) + 1):
        v = ret[i - 20:i].std(ddof=1)             # known at the close of days[i]; classifies the NEXT day only
        if len(vols) >= 20 and v > np.median(vols) and i + 1 < len(days):
            hi.add(days[i + 1].date)
        vols.append(v)
    return hi


def descriptive(days: list[Day]) -> dict:
    """H6 bucket drift and H8 weekday drift, in bp."""
    buckets = {"09:15-09:35": (idx(9, 15), idx(9, 34)), "09:35-12:00": (idx(9, 35), idx(11, 59)),
               "12:00-14:00": (idx(12, 0), idx(13, 59)), "14:00-15:00": (idx(14, 0), idx(14, 59)),
               "15:00-15:30": (idx(15, 0), idx(15, 29))}
    h6 = {k: {d.date: [(d.c[b] - d.o[a], d.o[a])] for d in days if not (np.isnan(d.c[b]) or np.isnan(d.o[a]))}
          for k, (a, b) in buckets.items()}
    wd = {}
    for d in days:
        w = date.fromisoformat(d.date).strftime("%a")
        a, b = d.o[0], d.c[idx(15, 29)]
        if not (np.isnan(a) or np.isnan(b)):
            wd.setdefault(w, {})[d.date] = [(b - a, a)]
    return {"H6": h6, "H8": wd}


def expiry_days(days: list[Day], index: str) -> set:
    """UNVERIFIED calendar assumption (see pre-registration amendment)."""
    import bisect
    ordered = sorted(d.date for d in days)
    have, last = set(ordered), ordered[-1]
    out = set()
    for d in days:
        dt = date.fromisoformat(d.date)
        after = d.date >= "2025-09-01"
        want = {"NIFTY": 1 if after else 3, "SENSEX": 3 if after else 4}[index]
        nominal = (dt + timedelta(days=(want - dt.weekday()) % 7)).isoformat()
        if nominal in have:
            expiry = nominal
        elif nominal > last:
            expiry = None                      # beyond the data: unknown, never guessed
        else:                                  # a trading day inside the data range is missing: exchange holiday
            i = bisect.bisect_left(ordered, nominal)
            expiry = ordered[i - 1] if i else None
        if expiry == d.date:
            out.add(d.date)
    return out


# ---------------------------------------------------------------- runner
def run_family(history_dir: Path, splits=("train", "validate"), open_final: bool = False, log=print) -> dict:
    out = {}
    for index in ("NIFTY", "SENSEX"):
        for split in splits:
            days, info = load_days(history_dir, index, splits=(split,), open_final=open_final)
            log(f"{index} {split}: {info['days']} days, excluded short days: {len(info['excluded_short_days'])}")
            tests: list[Result] = []
            d2, dlast = h2_trades(days)
            gens = {"H1 first30->last30": h1_trades(days), "H2a overnight->day": d2, "H2b overnight->last30": dlast,
                    "H3 ORB15 hold30/15:14": h3_trades(days)}
            for h in (5, 15, 30):
                gens[f"H4 skeleton +{h}m"] = h4_trades(days, h)
            for name, tr in gens.items():
                r = evaluate(f"{index} {name}", tr)
                if r:
                    tests.append(r)
            desc = descriptive(days)
            for fam in ("H6", "H8"):
                for k, tr in desc[fam].items():
                    r = evaluate(f"{index} {fam} {k}", tr)
                    if r:
                        r.extra["descriptive"] = True
                        tests.append(r)
            hv = high_vol_days(days)
            vol = {"H5 H1 high-vs-low vol": label_shuffle_p({d: [p / r * 1e4 for p, r in v] for d, v in gens["H1 first30->last30"].items()}, hv),
                   "H5 H4+15 high-vs-low vol": label_shuffle_p({d: [p / r * 1e4 for p, r in v] for d, v in gens["H4 skeleton +15m"].items()}, hv)}
            ex = expiry_days(days, index)
            expiry = {nm: label_shuffle_p({d: [p / r * 1e4 for p, r in v] for d, v in gens[nm].items()}, ex)
                      for nm in ("H1 first30->last30", "H3 ORB15 hold30/15:14", "H4 skeleton +15m")}
            out[(index, split)] = {"tests": tests, "vol": vol, "expiry": expiry, "info": info}
    return out


def judge(out: dict) -> list[dict]:
    """Apply the frozen pass rule. Holm runs once over ALL train tests (both indices, descriptive ones included)."""
    train = [r for (ix, sp), v in out.items() if sp == "train" for r in v["tests"]]
    holm(train)
    val = {r.name: r for (ix, sp), v in out.items() if sp == "validate" for r in v["tests"]}
    rows = []
    for r in train:
        v = val.get(r.name)
        excl = lambda x: x is not None and (x.ci_bp[0] > 0 or x.ci_bp[1] < 0)   # noqa: E731
        row = {"test": r.name, "descriptive": bool(r.extra.get("descriptive")), "train_n": r.n_trades,
               "train_bp": round(r.mean_bp, 2), "train_ci": tuple(round(x, 2) for x in r.ci_bp), "p": round(r.p_two_sided, 4),
               "p_holm": round(r.p_holm, 4), "train_sig": bool(r.p_holm < 0.05 and excl(r)),
               "val_bp": None if v is None else round(v.mean_bp, 2),
               "val_ci": None if v is None else tuple(round(x, 2) for x in v.ci_bp),
               "val_same_sign": bool(v is not None and np.sign(v.mean_bp) == np.sign(r.mean_bp) and excl(v)),
               "clears_hurdle": bool(v is not None and min(abs(v.mean_bp), abs(r.mean_bp)) >= HURDLE_BP)}
        row["PASS"] = (not row["descriptive"]) and row["train_sig"] and row["val_same_sign"] and row["clears_hurdle"]
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Run the pre-registered price-only edge tests (train + validate only).")
    ap.add_argument("--history", default="data/history")
    ap.add_argument("--out", default="data/research")
    ap.add_argument("--family", type=int, choices=(1, 2), default=1)
    a = ap.parse_args(argv)
    out = (run_family2 if a.family == 2 else run_family)(Path(a.history))
    rows = judge(out)
    n_tests = len(rows)
    print(f"\nTests in the Holm family: {n_tests}. PASS requires Holm p<0.05 + interval excluding 0 on TRAIN, same sign + interval"
          f" excluding 0 on VALIDATE, and |mean| >= {HURDLE_BP} bp in both.\n")
    print(f"{'test':34s} {'trN':>6s} {'train bp [95% CI]':>24s} {'p':>7s} {'holm':>7s} {'valid bp [95% CI]':>24s}  verdict")
    for r in rows:
        v = "—" if r["val_bp"] is None else f"{r['val_bp']:+.2f} [{r['val_ci'][0]:+.2f},{r['val_ci'][1]:+.2f}]"
        tag = "PASS" if r["PASS"] else ("sig-train" if r["train_sig"] else "")
        print(f"{r['test']:34s} {r['train_n']:6d} {r['train_bp']:+8.2f} [{r['train_ci'][0]:+.2f},{r['train_ci'][1]:+.2f}] "
              f"{r['p']:7.4f} {r['p_holm']:7.4f} {v:>24s}  {tag}")
    print("\nH5 volatility regime and H7 expiry splits (diff of mean bp, label-shuffle p, informational):")
    for (ix, sp), v in (out.items() if a.family == 1 else []):
        for k, d in {**v["vol"], **v["expiry"]}.items():
            print(f"  {ix} {sp:8s} {k:28s} diff={d['diff']:+.2f}bp p={d['p']:.3f} (n_high/yes={d['n_high']}, n_low/no={d['n_low']})")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    Path(a.out, "edge_results_train_validate.json" if a.family == 1 else "edge_results_family2.json").write_text(json.dumps(rows, indent=1, default=str))
    print(f"\nPASS count: {sum(r['PASS'] for r in rows)} of {n_tests}. FINAL period untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
