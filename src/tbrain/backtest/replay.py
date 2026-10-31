"""Replay backtester: run the REAL brain + signal engine over recorded parquet, on simulated time.

    tbrain backtest --date 2026-10-19 --compare
Every tick is fed in the order it was received; the brain is snapshotted every `step_s` of simulated time
(live uses 0.25 s; 1 s is ~4x faster and gives near-identical decisions for 1-minute rules).
No network, no files written into data/signals (results stay in memory unless out_dir is given).
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, time as dtime
from pathlib import Path

import pyarrow.parquet as pq

from tbrain.brain.state import BrainState
from tbrain.brokers.upstox import instruments as ins
from tbrain.brokers.upstox.instruments import Contract
from tbrain.signals.engine import EngineConfig, SignalEngine
from tbrain.timeutil import IST, MARKET_CLOSE, MARKET_OPEN

SKIP_COLS = {"rho", "gamma", "theta", "vega", "rp", "i1_ts_ms", "i1_open", "i1_high", "i1_low", "i1_close", "i1_vol"}

_NO_SESSION_RULES = dict(allow_afternoon=True, dd_stop=False)   # behaviour before v2.2
PRESETS: dict[str, dict] = {
    "v1": dict(require_vwap=False, trend_k=None, fut_oi_fresh=False, premium_stop=False, regime_gate=False,
               **_NO_SESSION_RULES),
    "v2": dict(fut_oi_fresh=True, premium_stop=True, **_NO_SESSION_RULES),   # first v2 (all new filters)
    "v2.1": dict(**_NO_SESSION_RULES),                  # v1 + trend + VWAP (live 19-23 Oct)
    "v2.1+regime": dict(regime_gate=True, **_NO_SESSION_RULES),
    "v2.2": dict(),                                     # current defaults: v2.1 + 12:00 cutoff + drawdown stop
    "v2.2+afternoon": dict(allow_afternoon=True),       # v2.2 with the afternoon switch ON
}


def preset(name: str, base: EngineConfig | None = None) -> EngineConfig:
    return replace(base or EngineConfig(), **PRESETS[name])


def _files_in_order(paths: list[Path]) -> list[Path]:
    firsts = []
    for p in paths:
        try:
            t = pq.read_table(p, columns=["recv_ns"])
            if t.num_rows:
                firsts.append((t["recv_ns"][0].as_py(), p))
        except Exception:
            continue
    return [p for _, p in sorted(firsts)]


def load_contracts(data_dir: Path, day: str) -> list[Contract]:
    f = data_dir / "instruments" / f"contracts_{day}.json"
    if not f.exists():
        raise FileNotFoundError(f"{f} missing: run `tbrain instruments` on that day (it is cached automatically by record)")
    return [Contract.from_json(d) for d in json.loads(f.read_text())]


def replay_day(data_dir: Path, day: str, cfg: EngineConfig, *, tick_files: list[Path] | None = None,
               chain_files: list[Path] | None = None, contracts: list[Contract] | None = None, step_s: float = 1.0,
               risk_free: float = 0.065, vix_key: str = "NSE_INDEX|India VIX", log=print) -> dict:
    t_start = time.time()
    data_dir = Path(data_dir)
    contracts = contracts or load_contracts(data_dir, day)
    d = datetime.fromisoformat(day).date()
    unds = sorted({c.underlying for c in contracts})
    index_keys = {u: ins.index_key_for(contracts, u) for u in unds}
    index_keys = {u: k for u, k in index_keys.items() if k}
    segs = {c.underlying: c.segment for c in contracts}
    engine = SignalEngine(cfg, store_dir=None, log=lambda *a: None)
    brain = BrainState(contracts, index_keys, vix_key, risk_free, 0, engine=engine)
    tick_files = _files_in_order(tick_files or sorted((data_dir / "ticks" / f"date={day}").glob("*.parquet")))
    chain_rows = []
    for f in chain_files if chain_files is not None else sorted((data_dir / "chain" / f"date={day}").glob("*.parquet")):
        chain_rows += pq.read_table(f).to_pylist()
    chain_rows.sort(key=lambda r: r["recv_ns"])
    ci = 0
    open_ns = datetime.combine(d, MARKET_OPEN, tzinfo=IST).timestamp() * 1e9
    close_ns = datetime.combine(d, MARKET_CLOSE, tzinfo=IST).timestamp() * 1e9
    step_ns = int(step_s * 1e9)
    next_snap = None
    n_rows = n_snaps = 0
    last_snap = None

    def feed_info(ns):
        st = "NORMAL_OPEN" if open_ns <= ns <= close_ns else "NORMAL_CLOSE"
        return {"connected": True, "market_status": {s: st for s in segs.values()}}

    def snap(ns):
        nonlocal n_snaps, last_snap
        last_snap = brain.snapshot(feed_info(ns), mode="live", now_ns=int(ns))
        n_snaps += 1

    group, g_ns = [], None
    for f in tick_files:
        pf = pq.ParquetFile(f)
        cols = [c for c in pf.schema_arrow.names if c not in SKIP_COLS]
        for batch in pf.iter_batches(batch_size=100_000, columns=cols):  # streaming: constant memory
          rows = batch.to_pylist()
          rows.sort(key=lambda r: r["recv_ns"])
          for r in rows:
            ns = r["recv_ns"]
            if next_snap is None:
                next_snap = ns - ns % step_ns + step_ns
            while ns >= next_snap:  # snapshots happen BEFORE later ticks are known: no look-ahead
                if group:
                    brain.note_message(group[0].get("cur_ts_ms"), g_ns); brain.ingest(group); group = []
                while ci < len(chain_rows) and chain_rows[ci]["recv_ns"] <= next_snap:
                    brain.ingest_chain([chain_rows[ci]]); ci += 1
                snap(next_snap)
                next_snap += step_ns
            if g_ns is not None and ns != g_ns and group:
                brain.note_message(group[0].get("cur_ts_ms"), g_ns); brain.ingest(group); group = []
            group.append(r); g_ns = ns
            n_rows += 1
    if group:
        brain.note_message(group[0].get("cur_ts_ms"), g_ns); brain.ingest(group)
    if next_snap is not None:
        snap(next_snap)
    calls = sorted(engine.calls.values(), key=lambda c: c["issued_ms"])
    shadows = sorted(engine.shadows.values(), key=lambda c: c["issued_ms"])
    res = {"day": day, "config": {k: getattr(cfg, k) for k in ("require_vwap", "trend_k", "fut_oi_fresh", "premium_stop",
                                                                 "regime_gate", "min_score", "loss_pause_n",
                                                                 "allow_afternoon", "dd_stop")},
           "rows": n_rows, "snapshots": n_snaps, "seconds": round(time.time() - t_start, 1),
           "summary": SignalEngine.summarize(calls), "calls": calls, "shadows": shadows,
           "shadow_summary": _shadow_table(engine, shadows)}
    log(f"[backtest] {day}: {n_rows:,} ticks, {n_snaps:,} snapshots, {len(calls)} calls, {len(shadows)} shadows "
        f"in {res['seconds']}s")
    return res


def _shadow_table(engine: SignalEngine, shadows: list[dict]) -> dict:
    by: dict[str, list] = {}
    for s in shadows:
        for b in {engine.blocker_key(x) for x in s.get("blocked_by", [])} or {"other"}:
            by.setdefault(b, []).append(s)
    return {b: {"n": len(v), "wins": sum(x["pnl_per_lot"] > 0 for x in v if x["status"] == "closed"),
                "sum_r": round(sum(x["r_now"] for x in v), 2), "pnl_per_lot": round(sum(x["pnl_per_lot"] for x in v))}
            for b, v in sorted(by.items())}


def fidelity(live_calls: list[dict], replay_calls: list[dict], tol_min: int = 2) -> dict:
    """How closely a replay reproduces the live session: same index + direction within tol minutes."""
    matched = 0
    used = set()
    for lc in live_calls:
        for i, rc in enumerate(replay_calls):
            if i in used:
                continue
            if rc["und"] == lc["und"] and rc["dir"] == lc["dir"] and abs(rc["issued_ms"] - lc["issued_ms"]) <= tol_min * 60_000:
                used.add(i); matched += 1
                break
    return {"live": len(live_calls), "replay": len(replay_calls), "matched": matched,
            "recall": matched / len(live_calls) if live_calls else None}


def format_result(res: dict) -> str:
    s = res["summary"]
    lines = [f"{res['day']}  calls {s['calls']} (W {s['wins']} / L {s['losses']}, open {s['open']})  "
             f"sumR {s['sum_r']:+.2f}  P&L/lot {s['pnl_total_per_lot']:+,.0f}   [{res['rows']:,} ticks, {res['seconds']}s]"]
    for c in res["calls"]:
        t = datetime.fromtimestamp(c["issued_ms"] / 1000, IST).strftime("%H:%M")
        lines.append(f"   {t} {c['und']:6} {c['direction'][:4]} {c['symbol'][-20:]:20} sc {c['score']}/7 "
                     f"regime {c['regime']['label']:7} -> {c['outcome'] or c['status']:9} {c['r_now']:+5.2f}R {c['pnl_per_lot']:+7,.0f}")
    if res["shadow_summary"]:
        lines.append("   shadows (what blocked candidates would have done):")
        for b, v in res["shadow_summary"].items():
            verdict = "filter SAVED money" if v["sum_r"] < 0 else "filter COST money" if v["sum_r"] > 0 else "neutral"
            lines.append(f"     {b:22} n={v['n']:3} wins {v['wins']:3}  sumR {v['sum_r']:+7.2f}  -> {verdict}")
    return "\n".join(lines)
