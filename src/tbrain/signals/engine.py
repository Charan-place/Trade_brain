"""Signal engine (SIGNAL-ONLY, never trades). Rule-based, EXPERIMENTAL until the scoreboard proves it.

Per index, every brain tick:
  * BREAKOUT playbook (can issue live calls): 7 clues (1 pt each) + trigger (1-min close breaks the 10-min high/low)
    + MUST filters (VWAP side, 60-min trend) + gates (window, cooldown, open call, regime*, loss pause*,
    afternoon cutoff (12:00, dashboard switch), drawdown stop (today's closed R gives back 2R from a +2R peak)).
  * RANGE-FADE playbook (shadow-only until evidence): in a range regime, a rejection candle at the 60-min extreme,
    target = VWAP.
  * SHADOW CALLS: any candidate that is blocked (by a filter, gate or build check) is tracked silently to its outcome,
    so every filter's real value is measured every day (`tbrain review` / `tbrain backtest`).
Levels are set on the UNDERLYING, converted to option premium with Black-76 (current IV, basis, horizon decay).
Tracking: SL (index OR option premium) / T1 (stop -> breakeven) / T2 / time stop; P&L marked at the option BID.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from tbrain.analytics.black76 import price as b76
from tbrain.signals.event_calendar import is_event_blackout
from tbrain.timeutil import IST

LABEL = "EXPERIMENTAL · rule-based, not yet validated"
YEAR_MIN = 365 * 24 * 60
DATA_GATES = ("not live", "feed stale", "warming up", "outside entry window")  # no shadow for these: not a decision


@dataclass
class EngineConfig:
    min_score: int = 5            # of 7
    t1_r: float = 2.0
    t2_r: float = 3.0
    min_rr_net: float = 1.5
    max_spread_pct: float = 2.0
    min_depth_lots: int = 2
    cost_per_lot_inr: float | None = None  # None = use real_cost_per_lot() below; set a number to override/test
    risk_per_trade_inr: float = 0.0
    time_stop_min: int = 30
    cooldown_min: int = 10
    start: dtime = dtime(9, 25)
    last_entry: dtime = dtime(14, 45)
    square_off: dtime = dtime(15, 20)
    expiry_cutoff: dtime = dtime(13, 30)
    max_feed_age_s: float = 3.0
    max_quote_age_s: float = 3.0
    warmup_bars: int = 15
    horizon_min: int = 15
    big_window_s: int = 300
    # ---- v2 filters (from the 2026-10-19 replay; see docs/ALGORITHM.md) ----
    require_vwap: bool = True
    trend_k: float | None = 1.5
    trend_bars: int = 60
    loss_pause_n: int = 0
    loss_pause_min: int = 30
    premium_stop: bool = False        # v2.1: off; it stopped trades out on option noise (replay 2026-10-19: -2.4R)
    fut_oi_fresh: bool = False        # v2.1: off; needing OI rising with price skipped good trades (replay: -2.8R)
    # ---- regime brain ----
    regime_gate: bool = False         # breakout calls only in a 'trend' regime (decided by backtest evidence)
    trend_er: float = 0.25            # efficiency ratio over 60 min at/above which the market is trending
    range_er: float = 0.15            # ... below which it is ranging
    range_crosses: int = 4            # VWAP crossings in 60 min that also mark a range
    regime_confirm_bars: int = 3      # regime_gate needs this many CONSECUTIVE 1-min bars reading 'trend',
                                       # not just the current one (ADX/ER-style regime labels lag and flicker
                                       # right at the trend/range boundary; a single noisy reading shouldn't open
                                       # or close the gate)
    event_calendar_gate: bool = False  # block NEW calls around known scheduled events (RBI MPC/Budget/FOMC reaction)
    # ---- v2.2 session rules (9 live days 19 Oct-30 Oct: 12:00-13:59 = -9,316, 14:00+ = -7,924 pnl/lot) ----
    afternoon_cutoff: dtime | None = dtime(12, 0)   # no NEW calls after this unless allow_afternoon (UI switch)
    allow_afternoon: bool = False
    dd_stop: bool = True              # stop new calls once today's realized R falls dd_giveback_r from a peak >= dd_peak_r
    dd_peak_r: float = 2.0
    dd_giveback_r: float = 2.0
    correlation_block: bool = False   # block (not just flag) a same-direction call when the other index already has one open
    t1_direction_check: bool = False  # while in profit: exit now instead of riding if order flow or trend/VWAP flip
    direction_check_interval_min: int = 5   # slow layer (trend/VWAP/candles): re-run at most once every N minutes
    fast_check_window_s: int = 60           # fast layer (CVD/price momentum): lookback window, runs every tick
    direction_check_min_hold_s: int = 120    # don't even look until the call has been open this long
    direction_check_min_r: float = 0.3      # ...and is up at least this many R (noise filter, not "any profit")
    direction_check_fast_confirms: int = 2  # fast layer must read "against" this many ticks IN A ROW to close
    direction_check_fast_min_move: float = 2.0  # futures must move at least this many points against us (not just <0)
    # ---- v2.3 volatility regime filter (10:xx vs 11:xx split, 19 Oct-4 Nov: 10:xx payoff ratio 1.13 vs 11:xx 1.86,
    # driven by a wider avg SL (34.9 vs 23.7 pts) across those 11 days; NOT a within-day "open decays" pattern --
    # the 2026-10-19 replay showed that day's ATR peaked AT the open, so a fixed opening-window baseline never
    # fired. Compares current ATR to a trailing SAME-DAY median instead, catching a spike whenever it happens) ----
    vol_regime_filter: bool = False   # when current ATR is a spike vs the day's trailing median ATR: tighten stop + raise bar
    vol_spike_k: float = 1.3          # current atr_1m / trailing median ATR >= this -> "spiking"
    vol_spike_sl_floor_mult: float = 0.6   # breakout SL floor in spike regime (vs the normal 0.8 x ATR)
    vol_spike_min_score_bonus: int = 1     # extra clue required (on top of min_score) while the regime is spiking
    # ---- shadows ----
    shadows: bool = True
    range_fade_shadow: bool = True
    mode_id: str = "balanced"


def _fmt(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x:,.{d}f}"


# 2026 NSE/BSE index-options round-trip charges (buy to open, sell to close), per lot. All ad-valorem rates are
# on PREMIUM turnover (entry_px + exit_px) x lot, not notional - these are the actual numbers the earlier flat
# cost_per_lot_inr=40 placeholder didn't include (its own comment admitted as much).
BROKERAGE_PER_ORDER_INR = 20.0       # flat, most discount brokers; 2 orders (buy + sell) per round trip
STT_SELL_PREMIUM_RATE = 0.0015       # STT on options premium, SELL leg only. 0.15% since 1 Apr 2026 per broker notices (not yet
                                     # confirmed against the NSE circular); the higher rate is the conservative choice
EXCH_TXN_RATE = 0.0003503            # NSE/BSE transaction charges, both legs, on premium turnover
SEBI_FEE_RATE = 0.0000010            # negligible but real
STAMP_DUTY_BUY_RATE = 0.00003        # stamp duty, BUY leg only, on premium turnover
GST_RATE = 0.18                      # on (brokerage + exchange txn charges), not on STT/stamp duty


def real_cost_per_lot(entry_px: float, exit_px: float, lot: int) -> float:
    """Real round-trip cost per lot for one NSE/BSE index option trade, given an (unknown at entry time)
    estimated exit price. Used instead of a flat placeholder so min_rr_net reflects actual, not guessed, cost."""
    buy_turnover = entry_px * lot
    sell_turnover = exit_px * lot
    brokerage = 2 * BROKERAGE_PER_ORDER_INR
    stt = sell_turnover * STT_SELL_PREMIUM_RATE
    exch = (buy_turnover + sell_turnover) * EXCH_TXN_RATE
    sebi = (buy_turnover + sell_turnover) * SEBI_FEE_RATE
    stamp = buy_turnover * STAMP_DUTY_BUY_RATE
    gst = (brokerage + exch) * GST_RATE
    return brokerage + stt + exch + sebi + stamp + gst


def _baseline_atr(all_bars: list, exclude_last: int = 5) -> float | None:
    """Trailing same-day MEDIAN true range, excluding the most recent `exclude_last` bars (so a live spike
    doesn't inflate its own baseline). Median, not mean, so one earlier spike doesn't mask a new one.
    A fixed opening window (e.g. first 30 min) assumes volatility always decays after the open; that's false
    on days where the open IS the day's calmest stretch and a later window spikes instead (confirmed on the
    2026-10-19 replay: the open had the day's highest ATR, so an opening-relative ratio never exceeded 1.0)."""
    hist = all_bars[:-exclude_last] if len(all_bars) > exclude_last else []
    if len(hist) < 15:
        return None
    trs = [max(b[2] - b[3], abs(b[2] - p[4]), abs(b[3] - p[4])) for p, b in zip(hist[:-1], hist[1:])]
    if not trs:
        return None
    trs.sort()
    n = len(trs)
    return trs[n // 2] if n % 2 else 0.5 * (trs[n // 2 - 1] + trs[n // 2])


def regime_of(bars: list, atr: float | None, vwap_crosses: int | None, cfg: EngineConfig) -> dict:
    """Day-type read from completed 1-min bars (whole day) + VWAP crossings (Dalton-style trend vs balance)."""
    out = {"label": "unknown", "er60": None, "vwap_crosses60": vwap_crosses, "ib_high": None, "ib_low": None,
           "vs_ib": None, "move60": None}
    if len(bars) < 20:
        return out
    last60 = bars[-60:]
    closes = [b[4] for b in last60]
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    er = abs(closes[-1] - closes[0]) / path if path else 0.0
    out.update(er60=er, move60=closes[-1] - closes[0])
    day0 = datetime.fromtimestamp(bars[0][0] / 1000, IST)
    open_ms = datetime.combine(day0.date(), dtime(9, 15), tzinfo=IST).timestamp() * 1000
    ib = [b for b in bars if open_ms <= b[0] < open_ms + 60 * 60_000]
    if len(ib) >= 45:  # initial balance = first trading hour (needs most of it recorded)
        out["ib_high"], out["ib_low"] = max(b[2] for b in ib), min(b[3] for b in ib)
        c = bars[-1][4]
        out["vs_ib"] = "above" if c > out["ib_high"] else "below" if c < out["ib_low"] else "inside"
    crosses = vwap_crosses if vwap_crosses is not None else 0
    if er >= cfg.trend_er and crosses <= 2:
        out["label"] = "trend"
    elif er < cfg.range_er or crosses >= cfg.range_crosses:
        out["label"] = "range"
    else:
        out["label"] = "mixed"
    return out


class SignalEngine:
    def __init__(self, cfg: EngineConfig | None = None, store_dir: Path | None = None, log=print):
        self.cfg = cfg or EngineConfig()
        self.store_dir = store_dir
        self.log = log
        self.calls: dict[str, dict] = {}
        self.shadows: dict[str, dict] = {}
        self.diag: dict[str, dict] = {}
        self._last_issue_ms: dict[str, float] = {}
        self._issued_bar: dict[str, int] = {}
        self._shadow_bar: dict[tuple, int] = {}
        self._regime_streak: dict[str, int] = {}   # consecutive bars reading 'trend', per underlying
        self._regime_bar: dict[str, int] = {}      # last bar_id the streak was advanced on, per underlying (dedup)
        self.on_event = None  # optional hook (event, call) -> None, e.g. MongoSink.on_call_event; must not raise
        self.on_step = None   # optional hook (now_ms, ctxs) -> None, e.g. ExecutionHub.on_step
        self._load()

    # ------------------------------------------------------------ persistence
    def _path(self, shadow: bool = False) -> Path | None:
        return (self.store_dir / ("shadow.jsonl" if shadow else "calls.jsonl")) if self.store_dir else None

    def _load(self) -> None:
        for shadow, book in ((False, self.calls), (True, self.shadows)):
            p = self._path(shadow)
            if not p or not p.exists():
                continue
            for line in p.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    book[rec["call"]["id"]] = rec["call"]
                except (json.JSONDecodeError, KeyError):
                    continue

    def _append(self, event: str, call: dict) -> None:
        p = self._path(bool(call.get("shadow")))
        if p:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps({"event": event, "ts": time.time(), "call": call}, default=str) + "\n")
        if self.on_event is not None:
            try:
                self.on_event(event, call)
            except Exception as e:  # noqa: BLE001 - a storage hook must never break signal tracking
                self.log(f"[signal] storage hook failed: {e!r}")

    def display_keys(self) -> list[str]:
        return [c["instrument_key"] for c in self._recent(8)]

    def _recent(self, n: int) -> list[dict]:
        return sorted(self.calls.values(), key=lambda c: c["issued_ms"], reverse=True)[:n]

    # ------------------------------------------------------------ main step
    def step(self, now_ms: float, ctxs: dict[str, dict], tracker) -> dict:
        if self.on_step is not None:     # execution (paper/real books) sees the same quotes first
            try:
                self.on_step(now_ms, ctxs)
            except Exception as e:  # noqa: BLE001 - execution problems must never stop signals
                self.log(f"[exec] step failed: {e!r}")
        for book in (self.calls, self.shadows):
            for c in list(book.values()):
                if c["status"] in ("open", "t1"):
                    self._track(c, ctxs.get(c["und"]), now_ms, tracker)
        for und, ctx in ctxs.items():
            try:
                self.diag[und] = self._evaluate(und, ctx, tracker, now_ms)
            except Exception as e:  # a rule bug must never kill the brain loop
                self.diag[und] = {"gates": [f"engine error: {e!r}"], "sides": {}}
        return self._block(now_ms)

    # ------------------------------------------------------------ evaluation
    def _evaluate(self, und: str, ctx: dict, tr, now_ms: float) -> dict:
        cfg = self.cfg
        now = datetime.fromtimestamp(now_ms / 1000, IST)
        all_bars = ctx.get("bars") or []
        bars = all_bars[-60:]
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
        if cfg.loss_pause_n > 0:
            done = sorted((c for c in self.calls.values() if c["und"] == und and c["status"] == "closed" and c.get("exit_ms")),
                          key=lambda c: c["exit_ms"])[-cfg.loss_pause_n:]
            if (len(done) == cfg.loss_pause_n and all(c["outcome"] == "SL" for c in done)
                    and now_ms - done[-1]["exit_ms"] < cfg.loss_pause_min * 60_000):
                gates.append(f"paused {cfg.loss_pause_min} min after {cfg.loss_pause_n} stop-losses in a row")
        if cfg.afternoon_cutoff is not None and not cfg.allow_afternoon and now.time() >= cfg.afternoon_cutoff:
            gates.append(f"afternoon calls are off after {cfg.afternoon_cutoff:%H:%M} (switch on the dashboard)")
        if cfg.event_calendar_gate:
            blackout = is_event_blackout(now.date(), now.time())
            if blackout:
                gates.append(f"event calendar: {blackout}")
        if cfg.dd_stop:
            peak, cum = self.realized_path(now_ms)
            if peak >= cfg.dd_peak_r and peak - cum >= cfg.dd_giveback_r:
                gates.append(f"drawdown stop: today's closed calls fell {peak - cum:.1f}R from their +{peak:.1f}R peak")

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
        crosses = tr.crossings(und, "vwap_dev", 3600, eps=0.1 * atr if atr else 0.0) if hasattr(tr, "crossings") else None
        regime = regime_of(all_bars, atr, crosses, cfg)
        cur_bar_id = int(bars[-1][0]) if bars else None
        if cur_bar_id is not None and self._regime_bar.get(und) != cur_bar_id:
            self._regime_bar[und] = cur_bar_id
            self._regime_streak[und] = self._regime_streak.get(und, 0) + 1 if regime["label"] == "trend" else 0
        regime_streak = self._regime_streak.get(und, 0)
        regime_confirmed_trend = regime_streak >= cfg.regime_confirm_bars
        if cfg.regime_gate and not regime_confirmed_trend:
            gates.append(f"regime gate: market is '{regime['label']}' ({regime_streak}/{cfg.regime_confirm_bars} "
                         f"trend bars confirmed; breakouts only once trend is confirmed)")

        baseline_atr = _baseline_atr(all_bars)
        vol_ratio = atr / baseline_atr if (atr and baseline_atr) else None
        vol_spiking = cfg.vol_regime_filter and vol_ratio is not None and vol_ratio >= cfg.vol_spike_k
        min_score_needed = cfg.min_score + (cfg.vol_spike_min_score_bonus if vol_spiking else 0)

        sides = {}
        for d, name in ((1, "bullish"), (-1, "bearish")):
            conds = []

            def add(key, ok, text):
                conds.append({"key": key, "ok": bool(ok), "text": text})

            add("vwap", fut is not None and vwap is not None and d * (fut - vwap) > 0,
                f"Futures {'above' if d > 0 else 'below'} VWAP ({_fmt(fut)} vs {_fmt(vwap)})")
            add("aggression", cvd5 is not None and px5 is not None and d * cvd5 > 0 and d * px5 > 0,
                f"Aggressive {'buyers' if d > 0 else 'sellers'}: 5-min CVD {_fmt(cvd5, 0)}, price {_fmt(px5)}")
            fut_ok = oi5 is not None and px5 is not None and d * px5 > 0 and (oi5 > 0 or not cfg.fut_oi_fresh)
            fut_txt = "—" if oi5 is None or px5 is None else (
                ("long build-up" if oi5 > 0 else "short covering") if px5 > 0 else ("short build-up" if oi5 > 0 else "long unwinding"))
            add("fut_oi", fut_ok, f"Futures OI 5 min: {fut_txt} (ΔOI {_fmt(oi5, 0)}; needs fresh {'longs' if d > 0 else 'shorts'})")
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
            required = []
            if cfg.require_vwap:
                vw = next(c for c in conds if c["key"] == "vwap")
                required.append({"key": "vwap", "ok": vw["ok"], "text": "MUST: " + vw["text"]})
            if cfg.trend_k is not None:
                tb = bars[-cfg.trend_bars:]
                mv = (tb[-1][4] - tb[0][4]) if len(tb) >= 2 else None
                need = cfg.trend_k * atr if atr else None
                ok = mv is not None and need is not None and d * mv >= need
                required.append({"key": "trend", "ok": ok,
                                 "text": f"MUST: {len(tb)}-min trend {'up' if d > 0 else 'down'} >= {cfg.trend_k} x ATR "
                                         f"(move {_fmt(mv, 1)} pts, need {'+' if d > 0 else '-'}{_fmt(need, 1)})"})
            sides[name] = {"dir": d, "score": score, "max": len(conds), "conditions": conds, "trigger": trigger,
                           "required": required, "required_ok": all(r["ok"] for r in required)}

        out = {"evaluated_at": now.isoformat(), "gates": gates, "sides": sides, "min_score": cfg.min_score,
               "atr_1m": atr, "hh10": hh, "ll10": ll, "blocked": None, "regime": regime,
               "regime_streak": regime_streak, "regime_confirmed_trend": regime_confirmed_trend,
               "vol_ratio": vol_ratio, "vol_spiking": vol_spiking, "min_score_needed": min_score_needed}
        data_blocked = any(g.startswith(DATA_GATES) for g in gates)
        if not bars or data_blocked:
            return out
        bar_id = cur_bar_id
        sl_mult = cfg.vol_spike_sl_floor_mult if vol_spiking else 0.8

        # ---------------- breakout playbook ----------------
        cands = [s for s in sides.values() if s["trigger"] and s["score"] >= min_score_needed]
        if cands:
            best = max(cands, key=lambda s: (s["required_ok"], s["score"]))
            blockers = list(gates) + [r["text"].replace("MUST: ", "") for r in best["required"] if not r["ok"]]
            if not blockers and self._issued_bar.get(und) != bar_id:
                call, why = self._build(und, ctx, best, self._breakout_levels(spot, best["dir"], atr, bars, sl_mult),
                                        now, now_ms, "breakout", regime, enforce=True)
                if call is not None:
                    self._issued_bar[und] = bar_id
                    self._last_issue_ms[und] = now_ms
                    self.calls[call["id"]] = call
                    self._append("issue", call)
                    self.log(f"[signal] {call['symbol']} {call['direction']} entry {call['entry']:.2f} SL {call['p_sl']:.2f} "
                             f"T1 {call['p_t1']:.2f} score {call['score']}/{call['max_score']} regime {regime['label']}")
                    return out
                blockers.append(why)
            if blockers:
                out["blocked"] = "; ".join(b for b in blockers if b)
                self._shadow(und, ctx, best, self._breakout_levels(spot, best["dir"], atr, bars, sl_mult), now, now_ms,
                             "breakout", regime, blockers, bar_id)
        else:
            # One clue short of the bar: not a call, but tracked as a shadow so raising min_score never goes blind
            # to what the dropped bucket would have done (17 live days: score-5 = -5,082/162 calls, score>=6 positive).
            near = [s for s in sides.values() if s["trigger"] and s["score"] == min_score_needed - 1]
            if near:
                best = max(near, key=lambda s: (s["required_ok"], s["score"]))
                blockers = ([f"score {best['score']} < {min_score_needed} (one clue short)"] + list(gates)
                            + [r["text"].replace("MUST: ", "") for r in best["required"] if not r["ok"]])
                self._shadow(und, ctx, best, self._breakout_levels(spot, best["dir"], atr, bars, sl_mult), now, now_ms,
                             "breakout", regime, blockers, bar_id)

        # ---------------- range-fade playbook (shadow-only) ----------------
        if cfg.range_fade_shadow and regime["label"] == "range" and atr and len(bars) >= 30:
            vwap_idx = ctx.get("vwap_index")
            hi60, lo60 = max(b[2] for b in bars[:-1]), min(b[3] for b in bars[:-1])
            b = bars[-1]
            for d in (-1, 1):  # -1: fade the top, +1: fade the bottom
                touched = (b[2] >= hi60 - 0.1 * atr) if d < 0 else (b[3] <= lo60 + 0.1 * atr)
                rejected = (b[4] <= b[2] - 0.5 * atr) if d < 0 else (b[4] >= b[3] + 0.5 * atr)
                if not (touched and rejected and vwap_idx and spot) or d * (vwap_idx - spot) <= 0:
                    continue
                ext = b[2] if d < 0 else b[3]
                sl_u = ext - d * 0.3 * atr
                risk = abs(spot - sl_u)
                t1 = vwap_idx
                t2 = vwap_idx + d * 0.5 * abs(vwap_idx - spot)
                if risk <= 0 or abs(t1 - spot) < 1.0 * risk:
                    continue
                side = {"dir": d, "score": 0, "max": 0, "conditions": [], "required": [],
                        "trigger": f"rejection at 60-min {'high' if d < 0 else 'low'} {_fmt(ext)} in a range; target VWAP {_fmt(vwap_idx)}"}
                self._shadow(und, ctx, side, (sl_u, t1, t2, None), now, now_ms, "range_fade", regime,
                             ["shadow-only playbook (collecting evidence)"], bar_id)
        return out

    @staticmethod
    def _breakout_levels(spot, d, atr, bars, sl_floor_mult=0.8):
        if not spot or not atr or len(bars) < 5:
            return (None, None, None, "no spot/ATR")
        recent = bars[-5:]
        sl_u = (min(b[3] for b in recent) - 0.1 * atr) if d > 0 else (max(b[2] for b in recent) + 0.1 * atr)
        risk_u = abs(spot - sl_u)
        if risk_u < sl_floor_mult * atr:
            sl_u, risk_u = spot - d * sl_floor_mult * atr, sl_floor_mult * atr
        if risk_u > 3 * atr:
            return (None, None, None, f"stop too wide ({risk_u:.1f} pts > 3x ATR)")
        return (sl_u, spot + d * 2.0 * risk_u, spot + d * 3.0 * risk_u, None)

    def _shadow(self, und, ctx, side, levels, now, now_ms, playbook, regime, blockers, bar_id) -> None:
        if not self.cfg.shadows:
            return
        key = (und, side["dir"], playbook)
        if self._shadow_bar.get(key) == bar_id:
            return
        if any(s["und"] == und and s["dir"] == side["dir"] and s.get("playbook") == playbook and s["status"] in ("open", "t1")
               for s in self.shadows.values()):
            return
        self._shadow_bar[key] = bar_id
        call, _ = self._build(und, ctx, side, levels, now, now_ms, playbook, regime, enforce=False)
        if call is None:
            return
        call.update(shadow=True, blocked_by=[b for b in blockers if b], label="SHADOW · not shown as a call")
        self.shadows[call["id"]] = call
        self._append("issue", call)

    # ------------------------------------------------------------ build a call
    def _pick_chain(self, ctx: dict, now: datetime) -> dict | None:
        for exp in sorted(ctx.get("chains") or {}):
            ch = ctx["chains"][exp]
            if not ch.get("rows"):
                continue
            e = datetime.fromisoformat(exp).date() if len(exp) == 10 else None
            if e == now.date() and now.time() >= self.cfg.expiry_cutoff:
                continue
            return ch
        return None

    def _build(self, und, ctx, side, levels, now, now_ms, playbook, regime, enforce=True):
        """enforce=False builds the same call for SHADOW tracking, skipping the tradability checks
        (wall, spread, depth, R:R) but never the data requirements."""
        cfg, d = self.cfg, side["dir"]
        spot = ctx.get("spot")
        sl_u, t1_u, t2_u, why = levels
        if why or sl_u is None or not spot:
            return None, why or "no levels"
        ch = self._pick_chain(ctx, now)
        if not ch:
            return None, "no usable option chain"
        wall = ch.get("call_wall") if d > 0 else ch.get("put_wall")
        if enforce and playbook == "breakout" and wall is not None and (
                (d > 0 and spot < wall < t1_u) or (d < 0 and t1_u < wall < spot)):
            return None, f"{'call' if d > 0 else 'put'} OI wall at {wall:.0f} sits before T1 {t1_u:.0f}"
        kind = "CE" if d > 0 else "PE"
        row = next((r for r in ch["rows"] if r["atm"]), None)
        leg = row and row.get(kind.lower())
        if not leg:
            return None, "ATM leg missing"
        q = (ctx.get("quotes") or {}).get(leg["key"], {})
        quote_recv_ns = q.get("recv_ns")
        quote_age_s = (now_ms - quote_recv_ns / 1e6) / 1000 if quote_recv_ns else None
        if quote_recv_ns is not None and (quote_age_s < 0 or quote_age_s > cfg.max_quote_age_s):
            return None, f"option quote stale or unavailable (> {cfg.max_quote_age_s:.0f}s)"
        bid, ask = q.get("bp1"), q.get("ap1")
        if not bid or not ask or ask < bid:
            return None, "no two-sided quote on the ATM leg"
        mid = 0.5 * (bid + ask)
        spr = (ask - bid) / mid * 100
        lot = ctx.get("lot") or 1
        if enforce and spr > cfg.max_spread_pct:
            return None, f"spread {spr:.1f}% > {cfg.max_spread_pct}%"
        if enforce and (q.get("aq1") or 0) < cfg.min_depth_lots * lot:
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
        cost_sl = cfg.cost_per_lot_inr if cfg.cost_per_lot_inr is not None else real_cost_per_lot(entry, p_sl, lot)
        cost_t1 = cfg.cost_per_lot_inr if cfg.cost_per_lot_inr is not None else real_cost_per_lot(entry, p_t1, lot)
        risk_lot = (entry - p_sl) * lot + cost_sl
        reward_lot = (p_t1 - entry) * lot - cost_t1
        rr = reward_lot / risk_lot if risk_lot > 0 else 0
        if enforce and rr < cfg.min_rr_net:
            return None, f"net R:R {rr:.2f} < {cfg.min_rr_net} after est. costs"
        lots = max(0, int(cfg.risk_per_trade_inr // risk_lot)) if cfg.risk_per_trade_inr > 0 else None
        stop_at = min(now + timedelta(minutes=cfg.time_stop_min), datetime.combine(now.date(), cfg.square_off, tzinfo=IST))
        reasons = [rq["text"].replace("MUST: ", "") for rq in side.get("required", []) if rq["key"] == "trend" and rq["ok"]]
        reasons += [c["text"] for c in side.get("conditions", []) if c["ok"]]
        evidence = []
        cut = now_ms - cfg.big_window_s * 1000
        for t in (ctx.get("big_tape") or []):
            if t["t_ms"] >= cut and t["bullish"] == (d > 0):
                ts = datetime.fromtimestamp(t["t_ms"] / 1000, IST).strftime("%H:%M:%S")
                evidence.append(f"{ts} {t['side']} {t['lots']:.0f} lots ({t['qty']:,.0f} qty) {t['symbol']} @ {_fmt(t['px'])}")
            if len(evidence) >= 5:
                break
        correlated = [c["symbol"] for c in self.calls.values()
                      if c["und"] != und and c["status"] in ("open", "t1") and c["dir"] == d]
        if enforce and cfg.correlation_block and correlated:
            return None, f"same-direction call already open on {correlated[0]} (correlation block)"
        return {
            "id": f"{cfg.mode_id}-{uuid.uuid4().hex[:10]}", "mode": cfg.mode_id, "mode_version": 1,
            "und": und, "dir": d, "direction": "bullish" if d > 0 else "bearish",
            "action": f"BUY {kind}", "instrument_key": leg["key"], "kind": kind, "strike": K, "expiry": ch.get("expiry"),
            "symbol": f"{und} {K:.0f} {kind} {ch.get('expiry')}", "lot": lot, "issued_ms": now_ms,
            "issued_at": now.isoformat(), "score": side["score"], "max_score": side["max"], "trigger": side["trigger"],
            "reasons": reasons, "evidence": evidence, "label": LABEL, "playbook": playbook,
            "regime": {k: regime.get(k) for k in ("label", "er60", "vwap_crosses60", "vs_ib", "move60")},
            "correlated_with": correlated,
            "u_entry": spot, "u_sl": sl_u, "u_sl_active": sl_u, "u_t1": t1_u, "u_t2": t2_u, "p_sl_active": p_sl,
            "entry": entry, "entry_zone": [round(mid, 2), round(ask * 1.005, 2)], "p_sl": p_sl, "p_t1": p_t1, "p_t2": p_t2,
            "iv_at_entry": iv, "spread_pct": spr, "risk_per_lot": risk_lot, "reward_t1_per_lot": reward_lot, "rr_net": rr,
            "cost_override": cfg.cost_per_lot_inr, "size_hint_lots": lots, "time_stop_ms": stop_at.timestamp() * 1000,
            "status": "open", "t1_hit": False, "outcome": None, "exit": None, "exit_ms": None,
            "last_bid": bid,
            "cost_est_per_lot": (cfg.cost_per_lot_inr if cfg.cost_per_lot_inr is not None
                                  else real_cost_per_lot(entry, entry, lot)),
            "pnl_per_lot": -(cfg.cost_per_lot_inr if cfg.cost_per_lot_inr is not None
                             else real_cost_per_lot(entry, entry, lot)),
            "r_now": 0.0, "mfe": 0.0, "mae": 0.0,
        }, None

    # ------------------------------------------------------------ direction re-check (while in profit)
    def _direction_favorable_slow(self, c: dict, ctx: dict) -> dict:
        """Slow layer (re-run every `direction_check_interval_min`): same MUST filters as entry.
          * futures still on our side of VWAP.
          * 60-min trend still >= trend_k x ATR.
          * tighter: of the last 2 COMPLETED 1-min candles, at least 1 must still close with our direction.
        This reacts to a sustained reversal, not a single print - too slow on its own for options that can
        swing on one block trade, hence the fast layer below.
        """
        cfg, d = self.cfg, c["dir"]
        fut, vwap = ctx.get("fut"), ctx.get("vwap")
        vwap_ok = fut is None or vwap is None or d * (fut - vwap) > 0
        bars = (ctx.get("bars") or [])[-cfg.trend_bars:]
        trend_ok = True
        if cfg.trend_k is not None and len(bars) >= 15:
            trs = [max(b[2] - b[3], abs(b[2] - p[4]), abs(b[3] - p[4])) for p, b in zip(bars[:-1], bars[1:])]
            atr = sum(trs) / len(trs) if trs else None
            move = bars[-1][4] - bars[0][4]
            need = cfg.trend_k * atr if atr else None
            trend_ok = need is None or d * move >= need
        last2 = bars[-2:]
        tighter_ok = not last2 or any(d * (b[4] - b[1]) > 0 for b in last2)
        favorable = vwap_ok and trend_ok and tighter_ok
        return {"favorable": favorable, "vwap_ok": vwap_ok, "trend_ok": trend_ok, "tighter_ok": tighter_ok}

    def _direction_favorable_fast(self, c: dict, ctx: dict, tr) -> dict:
        """Fast layer (every tick while in profit, once past the min-hold/min-R gate): tick-level order flow
        (CVD + futures price over a short window), same idea as the entry 'aggression' clue but short enough to
        catch a reversal before it shows up in a completed 1-min candle. A HARD flip only: the futures move must
        exceed `direction_check_fast_min_move` points against us, and CVD must also be negative - a flat/near-zero
        reading (normal noise inside a trend) does not count as 'against'.
        """
        cfg, d, und = self.cfg, c["dir"], c["und"]
        cvd_fast = tr.delta(und, "cvd", cfg.fast_check_window_s)
        px_fast = tr.delta(und, "fut", cfg.fast_check_window_s)
        cvd_ok = cvd_fast is None or d * cvd_fast >= 0
        px_ok = px_fast is None or d * px_fast >= -cfg.direction_check_fast_min_move
        favorable = cvd_ok or px_ok   # only "against" when BOTH a real price move AND CVD have flipped
        return {"favorable": favorable, "cvd_ok": cvd_ok, "px_ok": px_ok}

    def _direction_favorable(self, c: dict, ctx: dict, tr, now_ms: float, run_slow: bool) -> dict:
        fast = self._direction_favorable_fast(c, ctx, tr)
        slow = c.get("t1_check_slow", {"favorable": True}) if not run_slow else self._direction_favorable_slow(c, ctx)
        if run_slow:
            c["t1_check_slow"] = slow
        fast_streak = c.get("fast_against_streak", 0) + 1 if not fast["favorable"] else 0
        c["fast_against_streak"] = fast_streak
        fast_confirmed_against = fast_streak >= self.cfg.direction_check_fast_confirms
        return {"favorable": not fast_confirmed_against and slow["favorable"], "fast": fast, "slow": slow,
                "fast_streak": fast_streak}

    # ------------------------------------------------------------ tracking
    def _track(self, c: dict, ctx: dict | None, now_ms: float, tr=None) -> None:
        q = ((ctx or {}).get("quotes") or {}).get(c["instrument_key"], {})
        recv_ns = q.get("recv_ns")
        quote_age_s = (now_ms - recv_ns / 1e6) / 1000 if recv_ns else None
        quote_fresh = recv_ns is None or 0 <= quote_age_s <= self.cfg.max_quote_age_s
        u = (ctx or {}).get("spot")
        bid = (q.get("bp1") or q.get("ltp")) if quote_fresh else None
        if bid:
            c["last_bid"] = bid
            cost = c["cost_override"] if c.get("cost_override") is not None else real_cost_per_lot(c["entry"], bid, c["lot"])
            c["cost_est_per_lot"] = cost
            pnl = (bid - c["entry"]) * c["lot"] - cost
            c["pnl_per_lot"] = pnl
            c["r_now"] = pnl / c["risk_per_lot"] if c["risk_per_lot"] else 0.0
            c["mfe"] = max(c["mfe"], pnl)
            c["mae"] = min(c["mae"], pnl)
        c["u_last"] = u
        d = c["dir"]
        psl = c.get("p_sl_active", c.get("p_sl"))
        ltp = (q.get("ltp") or bid) if quote_fresh else None
        if c["t1_hit"] and bid and ltp and bid <= c["entry"] and ltp <= c["entry"]:
            c["stop_via"] = "premium_breakeven"
            return self._close(c, "T1_STOP", now_ms)
        if self.cfg.premium_stop and not c["t1_hit"] and psl and bid and bid <= psl and ltp and ltp <= psl:
            c["stop_via"] = "premium"
            return self._close(c, "BREAKEVEN" if c["t1_hit"] else "SL", now_ms)
        if u is not None:
            if (d > 0 and u <= c["u_sl_active"]) or (d < 0 and u >= c["u_sl_active"]):
                c["stop_via"] = "index"
                return self._close(c, "T1_STOP" if c["t1_hit"] else "SL", now_ms)
            cfg = self.cfg
            held_long_enough = now_ms - c["issued_ms"] >= cfg.direction_check_min_hold_s * 1000
            up_enough = c["risk_per_lot"] > 0 and c["r_now"] >= cfg.direction_check_min_r
            if held_long_enough and up_enough and ctx and tr is not None:
                interval_ms = cfg.direction_check_interval_min * 60_000
                run_slow = now_ms - c.get("last_dir_check_ms", c["issued_ms"]) >= interval_ms
                if run_slow:
                    c["last_dir_check_ms"] = now_ms
                check = self._direction_favorable(c, ctx, tr, now_ms, run_slow)
                c["t1_check"] = check
                if cfg.t1_direction_check and not check["favorable"]:
                    c["stop_via"] = "direction_check"
                    return self._close(c, "EARLY_EXIT", now_ms)
            if not c["t1_hit"] and ((d > 0 and u >= c["u_t1"]) or (d < 0 and u <= c["u_t1"])):
                c["t1_hit"], c["status"], c["t1_ms"], c["t1_bid"] = True, "t1", now_ms, c.get("last_bid")
                c["u_sl_active"] = c["u_entry"]
                c["p_sl_active"] = c["entry"]
                self._append("t1", c)
                if not c.get("shadow"):
                    self.log(f"[signal] T1 hit {c['symbol']} bid {c.get('last_bid')}")
                return
            if c["t1_hit"] and ((d > 0 and u >= c["u_t2"]) or (d < 0 and u <= c["u_t2"])):
                return self._close(c, "T2", now_ms)
        if now_ms >= c["time_stop_ms"]:
            self._close(c, "TIME" if bid else "NO_DATA", now_ms)

    def _close(self, c: dict, outcome: str, now_ms: float) -> None:
        c["status"], c["outcome"], c["exit"], c["exit_ms"] = "closed", outcome, c.get("last_bid"), now_ms
        self._append("close", c)
        if not c.get("shadow"):
            self.log(f"[signal] closed {c['symbol']} {outcome} P&L/lot {c['pnl_per_lot']:.0f}")

    # ------------------------------------------------------------ output
    def scoreboard(self) -> dict:
        closed = [c for c in self.calls.values() if c["status"] == "closed" and c["outcome"] != "NO_DATA"]
        n = len(closed)
        if not n:
            return {"calls": len(self.calls), "closed": 0, "hit_t1": None, "avg_r": None, "total_pnl_per_lot": None}
        return {"calls": len(self.calls), "closed": n, "hit_t1": sum(c["t1_hit"] for c in closed) / n,
                "avg_r": sum(c["r_now"] for c in closed) / n, "total_pnl_per_lot": sum(c["pnl_per_lot"] for c in closed),
                "by_outcome": {o: sum(1 for c in closed if c["outcome"] == o)
                               for o in ("T2", "T1_STOP", "BREAKEVEN", "SL", "TIME", "EARLY_EXIT")}}

    def realized_path(self, now_ms: float) -> tuple[float, float]:
        """(peak, current) cumulative R of today's REAL calls that had closed by now_ms, in exit order."""
        day = datetime.fromtimestamp(now_ms / 1000, IST).date()
        done = sorted((c for c in self.calls.values() if c["status"] == "closed" and c.get("exit_ms")
                       and c["exit_ms"] <= now_ms and datetime.fromtimestamp(c["issued_ms"] / 1000, IST).date() == day),
                      key=lambda c: c["exit_ms"])
        cum = peak = 0.0
        for c in done:
            cum += c.get("r_now") or 0.0
            peak = max(peak, cum)
        return peak, cum

    RUNTIME_KEYS = ("allow_afternoon", "dd_stop", "t1_direction_check", "vol_regime_filter", "regime_gate",
                    "event_calendar_gate")

    def apply_settings(self, settings: dict | None) -> dict:
        """Runtime switches from the dashboard (only whitelisted booleans). Returns what changed."""
        changed = {}
        for k in self.RUNTIME_KEYS:
            if settings and k in settings and isinstance(settings[k], bool) and getattr(self.cfg, k) != settings[k]:
                setattr(self.cfg, k, settings[k])
                changed[k] = settings[k]
        if changed:
            self.log(f"[signal] settings changed from the dashboard: {changed}")
        return changed

    def today_calls(self, now_ms: float | None = None, book: dict | None = None) -> list[dict]:
        day = datetime.fromtimestamp((now_ms or time.time() * 1000) / 1000, IST).date()
        src = self.calls if book is None else book
        out = [c for c in src.values()
               if datetime.fromtimestamp(c["issued_ms"] / 1000, IST).date() == day or c["status"] in ("open", "t1")]
        return sorted(out, key=lambda c: c["issued_ms"], reverse=True)

    @staticmethod
    def summarize(calls: list[dict]) -> dict:
        closed = [c for c in calls if c["status"] == "closed" and c["outcome"] != "NO_DATA"]
        pnl_closed = sum(c["pnl_per_lot"] for c in closed)
        pnl_open = sum(c["pnl_per_lot"] for c in calls if c["status"] != "closed")
        wins = sum(1 for c in closed if c["pnl_per_lot"] > 0)
        return {"calls": len(calls), "closed": len(closed), "open": len(calls) - len(closed),
                "wins": wins, "losses": len(closed) - wins, "win_rate": wins / len(closed) if closed else None,
                "hit_t1": sum(c["t1_hit"] for c in closed) / len(closed) if closed else None,
                "pnl_closed_per_lot": pnl_closed, "pnl_open_per_lot": pnl_open, "pnl_total_per_lot": pnl_closed + pnl_open,
                "sum_r": sum(c["r_now"] for c in closed),
                "avg_r": sum(c["r_now"] for c in closed) / len(closed) if closed else None,
                "by_index": {u: round(sum(c["pnl_per_lot"] for c in calls if c["und"] == u), 2)
                             for u in sorted({c["und"] for c in calls})}}

    @staticmethod
    def blocker_key(text: str) -> str:
        t = text.lower()
        for k, label in (("trend", "trend filter"), ("vwap", "VWAP filter"), ("cooldown", "cooldown"),
                         ("already open", "one open per index"), ("wall", "OI wall"), ("r:r", "R:R"), ("spread", "spread"),
                         ("thin offer", "depth"), ("regime", "regime gate"), ("paused", "loss pause"),
                         ("shadow-only", "range-fade (shadow)"), ("levels", "levels"), ("stop too wide", "stop too wide"),
                         ("afternoon", "afternoon cutoff"), ("drawdown stop", "drawdown stop"),
                         ("event calendar", "event calendar"), ("clue short", "score threshold")):
            if k in t:
                return label
        return "other"

    def shadow_summary(self, now_ms: float | None = None) -> dict:
        """What each blocker 'saved' (negative sum R blocked) or 'cost' (positive sum R blocked), today."""
        today = self.today_calls(now_ms, self.shadows)
        by: dict[str, list] = {}
        for s in today:
            for b in {self.blocker_key(x) for x in s.get("blocked_by", [])} or {"other"}:
                by.setdefault(b, []).append(s)
        rows = {}
        for b, ss in sorted(by.items()):
            closed = [s for s in ss if s["status"] == "closed"]
            rows[b] = {"n": len(ss), "closed": len(closed), "wins": sum(s["pnl_per_lot"] > 0 for s in closed),
                       "sum_r": round(sum(s["r_now"] for s in closed), 2)}
        return {"total": len(today), "by_blocker": rows}

    def _block(self, now_ms: float | None = None) -> dict:
        opened = [c for c in self.calls.values() if c["status"] in ("open", "t1")]
        today = self.today_calls(now_ms)
        return {"status": "live" if opened else "experimental", "label": LABEL, "today": self.summarize(today),
                "shadow": self.shadow_summary(now_ms),
                "reason": "Rule-based calls with no track record yet: treat every call as an experiment until the "
                          "scoreboard has dozens of closed calls.",
                "calls": today, "open_ids": [c["id"] for c in opened],
                "diagnostics": self.diag, "scoreboard": self.scoreboard(),
                "rules": {"min_score": self.cfg.min_score, "t1_r": self.cfg.t1_r, "t2_r": self.cfg.t2_r,
                          "require_vwap": self.cfg.require_vwap, "trend_k": self.cfg.trend_k,
                          "loss_pause_n": self.cfg.loss_pause_n,
                          "premium_stop": self.cfg.premium_stop, "min_rr_net": self.cfg.min_rr_net,
                          "time_stop_min": self.cfg.time_stop_min, "cost_per_lot_inr": self.cfg.cost_per_lot_inr,
                          "afternoon_cutoff": self.cfg.afternoon_cutoff.strftime("%H:%M") if self.cfg.afternoon_cutoff else None,
                          "allow_afternoon": self.cfg.allow_afternoon, "dd_stop": self.cfg.dd_stop,
                          "dd_peak_r": self.cfg.dd_peak_r, "dd_giveback_r": self.cfg.dd_giveback_r,
                          "t1_direction_check": self.cfg.t1_direction_check,
                          "vol_regime_filter": self.cfg.vol_regime_filter, "vol_spike_k": self.cfg.vol_spike_k,
                          "regime_gate": self.cfg.regime_gate, "regime_confirm_bars": self.cfg.regime_confirm_bars,
                          "event_calendar_gate": self.cfg.event_calendar_gate}}
