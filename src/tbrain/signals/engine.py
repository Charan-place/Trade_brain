"""Signal engine (SIGNAL-ONLY, never trades). Rule-based confluence, clearly labelled EXPERIMENTAL until the
auto-scoreboard shows out-of-sample results.

Per underlying, every brain tick:
  7 conditions (1 point each) per direction + a trigger on completed 1-min candles + hard gates.
  Call when: trigger AND score >= min_score AND every gate passes AND net R:R (after est. costs) >= min_rr_net.
Levels are set on the UNDERLYING (swing/ATR stop, 2R / 3R targets), then converted to option premium with Black-76
(current IV, basis-adjusted forward, time decay over the expected horizon).
Tracking: SL / T1 (stop moves to breakeven) / T2 / time stop, on the underlying; P&L marked at the option BID.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from tbrain.analytics.black76 import price as b76
from tbrain.timeutil import IST

LABEL = "EXPERIMENTAL · rule-based, not yet validated"
YEAR_MIN = 365 * 24 * 60


@dataclass
class EngineConfig:
    min_score: int = 5            # of 7
    t1_r: float = 2.0             # T1 = 2x underlying risk
    t2_r: float = 3.0
    min_rr_net: float = 1.5       # (reward to T1 - costs) / (risk + costs), per lot, in rupees
    max_spread_pct: float = 2.0
    min_depth_lots: int = 2
    cost_per_lot_inr: float = 40.0  # PLACEHOLDER: ~2 x Rs20 brokerage; statutory charges (STT etc.) NOT included
    risk_per_trade_inr: float = 0.0  # 0 = not set -> size hint shows risk per lot only
    time_stop_min: int = 30
    cooldown_min: int = 10
    start: dtime = dtime(9, 25)
    last_entry: dtime = dtime(14, 45)
    square_off: dtime = dtime(15, 20)
    expiry_cutoff: dtime = dtime(13, 30)  # after this on expiry day use the next expiry
    max_feed_age_s: float = 3.0
    warmup_bars: int = 15
    horizon_min: int = 15
    big_window_s: int = 300


def _fmt(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x:,.{d}f}"


class SignalEngine:
    def __init__(self, cfg: EngineConfig | None = None, store_dir: Path | None = None, log=print):
        self.cfg = cfg or EngineConfig()
        self.store_dir = store_dir
        self.log = log
        self.calls: dict[str, dict] = {}
        self.diag: dict[str, dict] = {}
        self._last_issue_ms: dict[str, float] = {}
        self._issued_bar: dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------ persistence
    def _path(self) -> Path | None:
        return self.store_dir / "calls.jsonl" if self.store_dir else None

    def _load(self) -> None:
        p = self._path()
        if not p or not p.exists():
            return
        for line in p.read_text().splitlines():
            try:
                rec = json.loads(line)
                self.calls[rec["call"]["id"]] = rec["call"]
            except (json.JSONDecodeError, KeyError):
                continue

    def _append(self, event: str, call: dict) -> None:
        p = self._path()
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"event": event, "ts": time.time(), "call": call}, default=str) + "\n")

    def display_keys(self) -> list[str]:
        return [c["instrument_key"] for c in self._recent(8)]

    def _recent(self, n: int) -> list[dict]:
        return sorted(self.calls.values(), key=lambda c: c["issued_ms"], reverse=True)[:n]

    # ------------------------------------------------------------ main step
    def step(self, now_ms: float, ctxs: dict[str, dict], tracker) -> dict:
        for c in list(self.calls.values()):
            if c["status"] in ("open", "t1"):
                self._track(c, ctxs.get(c["und"]), now_ms)
        for und, ctx in ctxs.items():
            try:
                self.diag[und] = self._evaluate(und, ctx, tracker, now_ms)
            except Exception as e:  # a rule bug must never kill the brain loop
                self.diag[und] = {"gates": [f"engine error: {e!r}"], "sides": {}}
        return self._block()

    # ------------------------------------------------------------ evaluation
    def _evaluate(self, und: str, ctx: dict, tr, now_ms: float) -> dict:
        cfg = self.cfg
        now = datetime.fromtimestamp(now_ms / 1000, IST)
        bars = ctx.get("bars") or []
        gates: list[str] = []
        if not ctx.get("live"):
            gates.append("not live (market closed / replay)")
        if ctx.get("feed_age") is None or ctx["feed_age"] > cfg.max_feed_age_s:
            gates.append(f"feed stale (> {cfg.max_feed_age_s:.0f}s)")
        if not (cfg.start <= now.time() <= cfg.last_entry):
            gates.append(f"outside entry window {cfg.start:%H:%M}-{cfg.last_entry:%H:%M}")
        if len(bars) < cfg.warmup_bars:
            gates.append(f"warming up: {len(bars)}/{cfg.warmup_bars} one-minute candles")
        last = self._last_issue_ms.get(und)
        if last and now_ms - last < cfg.cooldown_min * 60_000:
            gates.append(f"cooldown {cfg.cooldown_min} min after last call")
        if any(c["und"] == und and c["status"] in ("open", "t1") for c in self.calls.values()):
            gates.append("a call is already open on this index")

        spot, fut, vwap = ctx.get("spot"), ctx.get("fut"), ctx.get("vwap")
        flow = ctx.get("flow") or {}
        cvd5, px5 = tr.delta(und, "cvd", 300), tr.delta(und, "fut", 300)
        oi5 = tr.delta(und, "fut_oi", 300)
        pe15, ce15 = tr.delta(und, "pe_oi_near", 900), tr.delta(und, "ce_oi_near", 900)
        wr15 = (pe15 - ce15) if pe15 is not None and ce15 is not None else None
        strad5 = tr.delta(und, "straddle", 300)
        big5 = ctx.get("big_net")
        obi, rest = flow.get("obi"), flow.get("resting_imbalance")

        hh = ll = atr = None
        if len(bars) >= 11:
            prev = bars[-11:-1]
            hh, ll = max(b[2] for b in prev), min(b[3] for b in prev)
        if len(bars) >= 15:
            trs = [max(b[2] - b[3], abs(b[2] - p[4]), abs(b[3] - p[4])) for p, b in zip(bars[-15:-1], bars[-14:])]
            atr = sum(trs) / len(trs)
        last_close = bars[-1][4] if bars else None

        sides = {}
        for d, name in ((1, "bullish"), (-1, "bearish")):
            conds = []

            def add(key, ok, text):
                conds.append({"key": key, "ok": bool(ok), "text": text})

            add("vwap", fut is not None and vwap is not None and d * (fut - vwap) > 0,
                f"Futures {'above' if d > 0 else 'below'} VWAP ({_fmt(fut)} vs {_fmt(vwap)})")
            add("aggression", cvd5 is not None and px5 is not None and d * cvd5 > 0 and d * px5 > 0,
                f"Aggressive {'buyers' if d > 0 else 'sellers'}: 5-min CVD {_fmt(cvd5, 0)}, price {_fmt(px5)}")
            fut_ok = oi5 is not None and px5 is not None and d * px5 > 0
            fut_txt = "—" if oi5 is None or px5 is None else (
                ("long build-up" if oi5 > 0 else "short covering") if px5 > 0 else ("short build-up" if oi5 > 0 else "long unwinding"))
            add("fut_oi", fut_ok, f"Futures OI 5 min: {fut_txt} (ΔOI {_fmt(oi5, 0)})")
            add("big_players", big5 is not None and d * big5 > 0,
                f"Big prints net {'buy' if d > 0 else 'sell'}-side (5 min, {_fmt(big5, 0)} qty)")
            add("order_book", (obi is not None and d * obi > 0.15) or (rest is not None and d * rest > 0.10),
                f"Order book leans {'bid' if d > 0 else 'ask'} (top-5 {_fmt(obi)}, resting {_fmt(rest)})")
            add("writers", wr15 is not None and d * wr15 > 0,
                f"Option writers: {'put' if d > 0 else 'call'} OI added faster near ATM (15 min, {_fmt(wr15, 0)})")
            add("volatility", strad5 is not None and strad5 >= 0,
                f"ATM straddle steady/expanding (5 min {_fmt(strad5)}) — premium buying not fighting IV crush")
            trigger = None
            if last_close is not None and hh is not None:
                if d > 0 and last_close > hh:
                    trigger = f"1-min close {_fmt(last_close)} broke 10-min high {_fmt(hh)}"
                if d < 0 and last_close < ll:
                    trigger = f"1-min close {_fmt(last_close)} broke 10-min low {_fmt(ll)}"
            score = sum(c["ok"] for c in conds)
            sides[name] = {"dir": d, "score": score, "max": len(conds), "conditions": conds, "trigger": trigger}

        out = {"evaluated_at": now.isoformat(), "gates": gates, "sides": sides, "min_score": cfg.min_score,
               "atr_1m": atr, "hh10": hh, "ll10": ll, "blocked": None}
        best = max(sides.values(), key=lambda s: s["score"])
        if gates or not best["trigger"] or best["score"] < cfg.min_score:
            return out
        bar_id = int(bars[-1][0])
        if self._issued_bar.get(und) == bar_id:
            return out
        call, why = self._build(und, ctx, best, atr, bars, now, now_ms)
        if call is None:
            out["blocked"] = why
            return out
        self._issued_bar[und] = bar_id
        self._last_issue_ms[und] = now_ms
        self.calls[call["id"]] = call
        self._append("issue", call)
        self.log(f"[signal] {call['symbol']} {call['direction']} entry {call['entry']:.2f} SL {call['p_sl']:.2f} "
                 f"T1 {call['p_t1']:.2f} score {call['score']}/{call['max_score']}")
        return out

    # ------------------------------------------------------------ build a call
    def _pick_chain(self, ctx: dict, now: datetime) -> dict | None:
        chains = ctx.get("chains") or {}
        for exp in sorted(chains):
            ch = chains[exp]
            if not ch.get("rows"):
                continue
            e = datetime.fromisoformat(exp).date() if len(exp) == 10 else None
            if e == now.date() and now.time() >= self.cfg.expiry_cutoff:
                continue
            return ch
        return None

    def _build(self, und, ctx, side, atr, bars, now, now_ms):
        cfg, d = self.cfg, side["dir"]
        spot = ctx.get("spot")
        if not spot or not atr:
            return None, "no spot/ATR"
        recent = bars[-5:]
        if d > 0:
            sl_u = min(b[3] for b in recent) - 0.1 * atr
        else:
            sl_u = max(b[2] for b in recent) + 0.1 * atr
        risk_u = abs(spot - sl_u)
        if risk_u < 0.8 * atr:
            sl_u = spot - d * 0.8 * atr
            risk_u = 0.8 * atr
        if risk_u > 3 * atr:
            return None, f"stop too wide ({risk_u:.1f} pts > 3x ATR)"
        t1_u, t2_u = spot + d * cfg.t1_r * risk_u, spot + d * cfg.t2_r * risk_u
        ch = self._pick_chain(ctx, now)
        if not ch:
            return None, "no usable option chain"
        wall = ch.get("call_wall") if d > 0 else ch.get("put_wall")
        if wall is not None and ((d > 0 and spot < wall < t1_u) or (d < 0 and t1_u < wall < spot)):
            return None, f"{'call' if d > 0 else 'put'} OI wall at {wall:.0f} sits before T1 {t1_u:.0f}"
        kind = "CE" if d > 0 else "PE"
        row = next((r for r in ch["rows"] if r["atm"]), None)
        leg = row and row.get(kind.lower())
        if not leg:
            return None, "ATM leg missing"
        q = (ctx.get("quotes") or {}).get(leg["key"], {})
        bid, ask = q.get("bp1"), q.get("ap1")
        if not bid or not ask or ask < bid:
            return None, "no two-sided quote on the ATM leg"
        mid = 0.5 * (bid + ask)
        spr = (ask - bid) / mid * 100
        if spr > cfg.max_spread_pct:
            return None, f"spread {spr:.1f}% > {cfg.max_spread_pct}%"
        lot = ctx.get("lot") or 1
        if (q.get("aq1") or 0) < cfg.min_depth_lots * lot:
            return None, f"thin offer (< {cfg.min_depth_lots} lots at best ask)"
        iv = leg.get("iv") or ch.get("atm_iv")
        T = ch.get("T_years") or 0
        if not iv or T * YEAR_MIN < 45:
            return None, "IV unavailable or < 45 min to expiry"
        basis = (ch.get("forward") or spot) - spot
        T_h = max(T - cfg.horizon_min / YEAR_MIN, 1 / YEAR_MIN)
        r = ctx.get("r", 0.065)
        K = row["strike"]
        prem = lambda u: b76(u + basis, K, T_h, iv, r, kind)  # noqa: E731
        p_sl, p_t1, p_t2 = prem(sl_u), prem(t1_u), prem(t2_u)
        entry = ask
        if not (p_sl < entry < p_t1 < p_t2):
            return None, "premium levels not ordered after time decay (move too small for this option)"
        risk_lot = (entry - p_sl) * lot + cfg.cost_per_lot_inr
        reward_lot = (p_t1 - entry) * lot - cfg.cost_per_lot_inr
        rr = reward_lot / risk_lot if risk_lot > 0 else 0
        if rr < cfg.min_rr_net:
            return None, f"net R:R {rr:.2f} < {cfg.min_rr_net} after est. costs"
        lots = None
        if cfg.risk_per_trade_inr > 0:
            lots = max(0, int(cfg.risk_per_trade_inr // risk_lot))
        stop_at = min(now + timedelta(minutes=cfg.time_stop_min),
                      datetime.combine(now.date(), cfg.square_off, tzinfo=IST))
        reasons = [c["text"] for c in side["conditions"] if c["ok"]]
        # Exact evidence: the specific big prints on the call's side in the last 5 min (exchange data is anonymous:
        # we can say WHAT traded, how much and when - never WHO).
        evidence = []
        cut = now_ms - cfg.big_window_s * 1000
        for t in (ctx.get("big_tape") or []):
            if t["t_ms"] >= cut and t["bullish"] == (d > 0):
                ts = datetime.fromtimestamp(t["t_ms"] / 1000, IST).strftime("%H:%M:%S")
                evidence.append(f"{ts} {t['side']} {t['lots']:.0f} lots ({t['qty']:,.0f} qty) {t['symbol']} @ {_fmt(t['px'])}")
            if len(evidence) >= 5:
                break
        call = {
            "id": uuid.uuid4().hex[:10], "und": und, "dir": d, "direction": "bullish" if d > 0 else "bearish",
            "action": f"BUY {kind}", "instrument_key": leg["key"], "kind": kind, "strike": K, "expiry": ch.get("expiry"),
            "symbol": f"{und} {K:.0f} {kind} {ch.get('expiry')}", "lot": lot, "issued_ms": now_ms,
            "issued_at": now.isoformat(), "score": side["score"], "max_score": side["max"], "trigger": side["trigger"],
            "reasons": reasons, "evidence": evidence, "label": LABEL,
            "u_entry": spot, "u_sl": sl_u, "u_sl_active": sl_u, "u_t1": t1_u, "u_t2": t2_u,
            "entry": entry, "entry_zone": [round(mid, 2), round(ask * 1.005, 2)], "p_sl": p_sl, "p_t1": p_t1, "p_t2": p_t2,
            "iv_at_entry": iv, "risk_per_lot": risk_lot, "reward_t1_per_lot": reward_lot, "rr_net": rr,
            "cost_est_per_lot": cfg.cost_per_lot_inr, "size_hint_lots": lots, "time_stop_ms": stop_at.timestamp() * 1000,
            "status": "open", "t1_hit": False, "outcome": None, "exit": None, "exit_ms": None,
            "last_bid": bid, "pnl_per_lot": -cfg.cost_per_lot_inr, "r_now": 0.0, "mfe": 0.0, "mae": 0.0,
        }
        return call, None

    # ------------------------------------------------------------ tracking
    def _track(self, c: dict, ctx: dict | None, now_ms: float) -> None:
        q = ((ctx or {}).get("quotes") or {}).get(c["instrument_key"], {})
        u = (ctx or {}).get("spot")
        bid = q.get("bp1") or q.get("ltp")
        if bid:
            c["last_bid"] = bid
            pnl = (bid - c["entry"]) * c["lot"] - c["cost_est_per_lot"]
            c["pnl_per_lot"] = pnl
            c["r_now"] = pnl / c["risk_per_lot"] if c["risk_per_lot"] else 0.0
            c["mfe"] = max(c["mfe"], pnl)
            c["mae"] = min(c["mae"], pnl)
        c["u_last"] = u
        d = c["dir"]
        if u is not None:
            if (d > 0 and u <= c["u_sl_active"]) or (d < 0 and u >= c["u_sl_active"]):
                return self._close(c, "BREAKEVEN" if c["t1_hit"] else "SL", now_ms)
            if not c["t1_hit"] and ((d > 0 and u >= c["u_t1"]) or (d < 0 and u <= c["u_t1"])):
                c["t1_hit"], c["status"], c["t1_ms"], c["t1_bid"] = True, "t1", now_ms, c.get("last_bid")
                c["u_sl_active"] = c["u_entry"]  # protect: stop to breakeven
                self._append("t1", c)
                self.log(f"[signal] T1 hit {c['symbol']} bid {c.get('last_bid')}")
                return
            if c["t1_hit"] and ((d > 0 and u >= c["u_t2"]) or (d < 0 and u <= c["u_t2"])):
                return self._close(c, "T2", now_ms)
        if now_ms >= c["time_stop_ms"]:
            self._close(c, "TIME" if bid else "NO_DATA", now_ms)

    def _close(self, c: dict, outcome: str, now_ms: float) -> None:
        c["status"], c["outcome"], c["exit"], c["exit_ms"] = "closed", outcome, c.get("last_bid"), now_ms
        self._append("close", c)
        self.log(f"[signal] closed {c['symbol']} {outcome} P&L/lot {c['pnl_per_lot']:.0f}")

    # ------------------------------------------------------------ output
    def scoreboard(self) -> dict:
        closed = [c for c in self.calls.values() if c["status"] == "closed" and c["outcome"] != "NO_DATA"]
        n = len(closed)
        if not n:
            return {"calls": len(self.calls), "closed": 0, "hit_t1": None, "avg_r": None, "total_pnl_per_lot": None}
        return {"calls": len(self.calls), "closed": n, "hit_t1": sum(c["t1_hit"] for c in closed) / n,
                "avg_r": sum(c["r_now"] for c in closed) / n, "total_pnl_per_lot": sum(c["pnl_per_lot"] for c in closed),
                "by_outcome": {o: sum(1 for c in closed if c["outcome"] == o) for o in ("T2", "BREAKEVEN", "SL", "TIME")}}

    def _block(self) -> dict:
        opened = [c for c in self.calls.values() if c["status"] in ("open", "t1")]
        return {"status": "live" if opened else "experimental", "label": LABEL,
                "reason": "Rule-based calls with no track record yet: treat every call as an experiment until the "
                          "scoreboard has dozens of closed calls.",
                "calls": self._recent(12), "open_ids": [c["id"] for c in opened],
                "diagnostics": self.diag, "scoreboard": self.scoreboard(),
                "rules": {"min_score": self.cfg.min_score, "t1_r": self.cfg.t1_r, "t2_r": self.cfg.t2_r,
                          "min_rr_net": self.cfg.min_rr_net, "time_stop_min": self.cfg.time_stop_min,
                          "cost_per_lot_inr": self.cfg.cost_per_lot_inr}}
