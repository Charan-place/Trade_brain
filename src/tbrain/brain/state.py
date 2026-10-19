"""BrainState: merges live rows into a per-instrument picture and produces the UI snapshot.

Senses: IV/Greeks/moneyness/OI/flow/liquidity, 1-min candles, big-print detection, rolling feature history.
Judgement: SignalEngine (rule-based, EXPERIMENTAL) issues and tracks signal-only calls.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from tbrain.analytics.chain import analyze
from tbrain.brain.candles import CandleBook
from tbrain.brain.features import FeatureTracker
from tbrain.brokers.upstox.instruments import Contract
from tbrain.brokers.upstox.normalize import DEPTH
from tbrain.timeutil import IST, MARKET_CLOSE, in_market_hours, now_ist

FLOW_WINDOW_S = 300
BIG_MIN_LOTS = 20       # a "big print" is at least this many lots in one update ...
BIG_PCTL = 0.99         # ... and above this percentile of the instrument's own update sizes today
BIG_KEEP_S = 900
_DEPTH_KEYS = [f"{s}{n}" for n in range(1, DEPTH + 1) for s in ("bp", "bq", "ap", "aq")]
MIN_SESSIONS_FOR_TRAINING = 20
CHART_BARS = 400  # a full NSE/BSE session is 375 one-minute bars


def _t_years(expiry, val: datetime) -> float:
    exp_dt = datetime.combine(expiry, MARKET_CLOSE, tzinfo=IST)
    return max((exp_dt - val).total_seconds(), 60.0) / (365.0 * 86400.0)


class BrainState:
    def __init__(self, contracts: list[Contract], index_keys: dict[str, str], vix_key: str | None,
                 risk_free: float = 0.065, sessions_recorded: int = 0, engine=None):
        self.meta = {c.instrument_key: c for c in contracts}
        self.index_keys = dict(index_keys)
        self.vix_key = vix_key
        self.r = risk_free
        self.sessions_recorded = sessions_recorded
        self.engine = engine
        self.latest: dict[str, dict] = {}
        self.first: dict[str, dict] = {}
        self.prev_oi: dict[str, float] = {}
        self.flow: dict[str, dict] = {}
        self.big: dict[str, deque] = {}
        self._dv_hist: dict[str, deque] = {}
        self._big_thr: dict[str, float] = {}
        self.candles = CandleBook()
        self.tracker = FeatureTracker()
        self._lags_ms: deque = deque(maxlen=400)
        self._last_recv_ns = 0
        self.vwap_hist: dict[str, dict[int, float]] = {}   # und -> minute -> index-equivalent VWAP
        self.segment = {}
        for c in contracts:
            self.segment.setdefault(c.underlying, c.segment)
        self._lock = threading.Lock()

    def note_message(self, cur_ts_ms: int | None, recv_ns: int) -> None:
        """Per feed message: how long after Upstox stamped it did we receive it (includes clock skew)."""
        with self._lock:
            self._last_recv_ns = recv_ns
            if cur_ts_ms:
                self._lags_ms.append(recv_ns / 1e6 - cur_ts_ms)

    # ---------------- ingest ----------------
    def ingest(self, rows: list[dict]) -> None:
        with self._lock:
            for r in rows:
                k = r["instrument_key"]
                old = self.latest.get(k)
                dv = 0.0
                if old is None:
                    new = dict(r)
                    self.first[k] = dict(r)
                else:
                    new = dict(old)
                    if any(r.get(x) is not None for x in _DEPTH_KEYS):  # depth snapshot replaces the whole book
                        for x in _DEPTH_KEYS:
                            new[x] = r.get(x)
                    for x, v in r.items():
                        if v is not None and x not in _DEPTH_KEYS:
                            new[x] = v
                    if old.get("vtt") is not None and new.get("vtt") is not None and new["vtt"] > old["vtt"]:
                        dv = float(new["vtt"] - old["vtt"])
                    self._track_flow(k, old, new, dv)
                self.latest[k] = new
                px = r.get("ltp")
                if px:
                    ts = r.get("ltt_ms") or (r.get("recv_ns") or time.time_ns()) // 1_000_000
                    self.candles.update(k, ts, px, dv)

    def record_vwap(self, und: str, ts_ms: float, value: float) -> None:
        h = self.vwap_hist.setdefault(und, {})
        h[int(ts_ms) - int(ts_ms) % 60_000] = value
        if len(h) > CHART_BARS:
            for k in sorted(h)[: len(h) - CHART_BARS]:
                del h[k]

    def ingest_chain(self, chain_rows: list[dict]) -> None:
        with self._lock:
            for c in chain_rows:
                if c.get("instrument_key") and c.get("prev_oi") is not None:
                    self.prev_oi[c["instrument_key"]] = float(c["prev_oi"])

    def _track_flow(self, k: str, old: dict, new: dict, dv: float) -> None:
        """Lee-Ready style: traded volume since last update, signed by last price vs PREVIOUS quote mid (tick rule if at mid)."""
        if dv <= 0:
            return
        px = new.get("ltp")
        b, a = old.get("bp1"), old.get("ap1")
        sign = 0
        if px is not None and b and a:
            mid = 0.5 * (a + b)
            sign = 1 if px > mid else -1 if px < mid else 0
        if sign == 0 and px is not None and old.get("ltp") is not None:
            sign = 1 if px > old["ltp"] else -1 if px < old["ltp"] else 0
        f = self.flow.setdefault(k, {"cvd": 0.0, "win": deque()})
        f["cvd"] += sign * dv
        now = new.get("recv_ns") or time.time_ns()
        f["win"].append((now, sign * dv, dv))
        cutoff = now - FLOW_WINDOW_S * 1_000_000_000
        while f["win"] and f["win"][0][0] < cutoff:
            f["win"].popleft()
        # big-print detection with an adaptive, per-instrument threshold
        h = self._dv_hist.setdefault(k, deque(maxlen=3000))
        h.append(dv)
        c = self.meta.get(k)
        floor = BIG_MIN_LOTS * ((c.lot_size if c and c.lot_size else 1))
        if len(h) % 100 == 0 or k not in self._big_thr:
            srt = sorted(h)
            self._big_thr[k] = max(floor, srt[int(len(srt) * BIG_PCTL)] if len(srt) >= 200 else floor)
        if sign and dv >= self._big_thr[k]:
            q = self.big.setdefault(k, deque())
            q.append((now, sign * dv, px))
            cut = now - BIG_KEEP_S * 1_000_000_000
            while q and q[0][0] < cut:
                q.popleft()

    # ---------------- snapshot ----------------
    def _flow_summary(self, k: str | None) -> dict:
        if not k:
            return {}
        f, row = self.flow.get(k), self.latest.get(k, {})
        out = {"cvd": f["cvd"] if f else None, "buy_share_5m": None, "resting_imbalance": None, "obi": None}
        if f and f["win"]:
            tot = sum(x[2] for x in f["win"])
            buys = sum(x[2] for x in f["win"] if x[1] > 0)
            out["buy_share_5m"] = buys / tot if tot else None
        tb, ts = row.get("tbq"), row.get("tsq")
        if tb is not None and ts is not None and (tb + ts) > 0:
            out["resting_imbalance"] = (tb - ts) / (tb + ts)
        bq = sum(row.get(f"bq{n}") or 0 for n in range(1, DEPTH + 1))
        aq = sum(row.get(f"aq{n}") or 0 for n in range(1, DEPTH + 1))
        if bq + aq > 0:
            out["obi"] = (bq - aq) / (bq + aq)
        return out

    def _big_summary(self, und: str, now_ns: int, window_s: int = 300) -> dict:
        """Bullish-signed big flow: fut buys +, CE buys +, PE buys -.  Also returns the latest prints for the tape."""
        cut = now_ns - window_s * 1_000_000_000
        net, tape = 0.0, []
        for k, q in self.big.items():
            c = self.meta.get(k)
            if not c or c.underlying != und:
                continue
            bull = -1 if c.kind == "PE" else 1
            for t, sv, px in q:
                if t >= cut:
                    net += bull * sv
                tape.append({"t_ms": t / 1e6, "symbol": c.trading_symbol, "side": "BUY" if sv > 0 else "SELL",
                             "qty": abs(sv), "lots": abs(sv) / (c.lot_size or 1), "px": px, "bullish": bull * sv > 0})
        tape.sort(key=lambda x: x["t_ms"], reverse=True)
        return {"net_5m": net if tape else None, "tape": tape[:12]}

    def _fut_key(self, und: str, latest: dict) -> str | None:
        return next((k for k, c in self.meta.items() if c.underlying == und and c.kind == "FUT" and k in latest), None)

    def snapshot(self, feed: dict | None = None, mode: str = "live") -> dict:
        t0 = time.perf_counter()
        now_ns = time.time_ns()
        show_keys = self.engine.display_keys() if self.engine else []
        with self._lock:  # copy only; heavy maths happens after the lock is released so ticks are never blocked
            latest = {k: dict(v) for k, v in self.latest.items()}
            first = {k: dict(v) for k, v in self.first.items()}
            prev_oi = dict(self.prev_oi)
            flows = {u: self._flow_summary(self._fut_key(u, latest)) for u in self.index_keys}
            bigs = {u: self._big_summary(u, now_ns) for u in self.index_keys}
            lags = sorted(self._lags_ms)
            last_recv = self._last_recv_ns
            now_ms = now_ns / 1e6
            idx_bars = {u: self.candles.series(k, CHART_BARS) for u, k in self.index_keys.items()}
            idx_done = {u: self.candles.completed(k, now_ms, 60) for u, k in self.index_keys.items()}
            call_bars = {k: self.candles.series(k, 120) for k in show_keys}
        unds = {u: self._underlying(u, k, latest, first, prev_oi, mode, flows[u]) for u, k in self.index_keys.items()}
        feed = feed or {}
        feed_age = (now_ns - last_recv) / 1e9 if last_recv else None
        market = feed.get("market_status") or {}
        ctxs = {}
        for u, U in unds.items():
            U["big"] = bigs[u]
            U["candles"] = idx_bars[u]
            fut = latest.get(self._fut_key(u, latest) or "", {})
            nearest = next(iter(U["chains"].values()), None)
            ce_near = pe_near = None
            if nearest and nearest.get("rows"):
                atm_i = next((i for i, r in enumerate(nearest["rows"]) if r["atm"]), None)
                if atm_i is not None:
                    win = nearest["rows"][max(0, atm_i - 2): atm_i + 3]
                    ce_near = sum(((r.get("ce") or {}).get("oi") or 0) for r in win)
                    pe_near = sum(((r.get("pe") or {}).get("oi") or 0) for r in win)
            vwap = fut.get("atp")
            self.tracker.sample(u, now_ns / 1e9, {
                "spot": U["spot"], "fut": fut.get("ltp"), "fut_oi": fut.get("oi"), "cvd": flows[u].get("cvd"),
                "vwap_dev": (fut["ltp"] - vwap) if fut.get("ltp") and vwap else None,
                "straddle": nearest.get("straddle") if nearest else None, "ce_oi_near": ce_near, "pe_oi_near": pe_near})
            seg = self.segment.get(u)
            ctxs[u] = {
                "live": mode == "live" and in_market_hours() and market.get(seg) == "NORMAL_OPEN",
                "feed_age": feed_age, "spot": U["spot"], "fut": fut.get("ltp"), "vwap": vwap,
                "flow": flows[u], "big_net": bigs[u]["net_5m"], "big_tape": bigs[u]["tape"], "bars": idx_done[u], "chains": U["chains"],
                "lot": U["lot_size"], "r": self.r, "quotes": latest,
            }
            U["vwap"] = vwap
            U["vwap_index_equiv"] = (vwap - U["future"]["basis"]) if vwap and U["future"].get("basis") is not None else None
            if U["vwap_index_equiv"] and mode == "live":
                self.record_vwap(u, now_ms, U["vwap_index_equiv"])
            U["vwap_series"] = sorted(self.vwap_hist.get(u, {}).items())
        if self.engine:
            signals = self.engine.step(now_ms, ctxs, self.tracker)
            for c in signals["calls"]:
                c["candles"] = call_bars.get(c["instrument_key"], [])
        else:
            signals = {"status": "observing", "label": "", "calls": [], "diagnostics": {},
                       "reason": "Signal engine runs only while recording live.", "scoreboard": {"calls": 0}}
        latency = {
            "upstox_to_us_ms_p50": lags[len(lags) // 2] if lags else None,
            "upstox_to_us_ms_p95": lags[int(len(lags) * 0.95)] if lags else None,
            "feed_age_s": feed_age,
            "exchange_age_s": {u: ((now_ms - latest[k]["ltt_ms"]) / 1000) if latest.get(k, {}).get("ltt_ms") else None
                               for u, k in self.index_keys.items()},
            "compute_ms": None,
        }
        out = {"generated_at": now_ist().isoformat(), "generated_ms": now_ms, "mode": mode, "feed": feed,
               "latency": latency, "underlyings": unds, "brain": self._layers(feed, unds, signals), "signals": signals}
        latency["compute_ms"] = (time.perf_counter() - t0) * 1000
        return _round(out)

    def _underlying(self, und, ikey, latest, first, prev_oi, mode, flow) -> dict:
        idx = latest.get(ikey, {})
        spot = idx.get("ltp") or idx.get("cp")
        fut_key = self._fut_key(und, latest)
        fut = latest.get(fut_key, {}) if fut_key else {}
        legs_by_exp: dict = {}
        for k, row in latest.items():
            c = self.meta.get(k)
            if not c or c.underlying != und or c.kind not in ("CE", "PE"):
                continue
            use_prev = k in prev_oi
            legs_by_exp.setdefault(c.expiry, []).append({
                "key": k, "strike": c.strike, "kind": c.kind, "ltp": row.get("ltp"), "bid": row.get("bp1"), "ask": row.get("ap1"),
                "oi": row.get("oi"), "vtt": row.get("vtt"), "iv_vendor": row.get("iv"), "delta_vendor": row.get("delta"),
                "tbq": row.get("tbq"), "tsq": row.get("tsq"),
                "oi_base": prev_oi[k] if use_prev else first.get(k, {}).get("oi"),
                "ltp_base": row.get("cp") if use_prev else first.get(k, {}).get("ltp"),
                "ltt_ms": row.get("ltt_ms"),
            })
        chains = {}
        for exp in sorted(legs_by_exp)[:3]:
            legs = legs_by_exp[exp]
            if mode == "live" and in_market_hours():
                val = now_ist()
            else:  # after hours / demo: value at the time the prices were made, not "now"
                ltts = [l["ltt_ms"] for l in legs if l.get("ltt_ms")]
                val = datetime.fromtimestamp(max(ltts) / 1000, IST) if ltts else now_ist()
            a = analyze(legs, _t_years(exp, val), self.r, spot)
            a["expiry"] = exp.isoformat()
            a["valued_at"] = val.isoformat()
            a["baseline"] = "prev close (REST prev_oi)" if any(l["key"] in prev_oi for l in legs) else "session start"
            chains[exp.isoformat()] = a
        lot = next((c.lot_size for c in self.meta.values() if c.underlying == und and c.lot_size), None)
        out = {
            "index_key": ikey, "spot": spot, "spot_prev_close": idx.get("cp"),
            "spot_chg_pct": ((idx["ltp"] / idx["cp"] - 1) * 100) if idx.get("ltp") and idx.get("cp") else None,
            "future": {"symbol": self.meta[fut_key].trading_symbol if fut_key else None, "ltp": fut.get("ltp"),
                       "basis": (fut["ltp"] - spot) if fut.get("ltp") and spot else None, "oi": fut.get("oi"), "vtt": fut.get("vtt")},
            "flow": flow, "lot_size": lot, "chains": chains,
        }
        if und == "NIFTY" and self.vix_key in latest:
            v = latest[self.vix_key]
            out["vix"] = {"ltp": v.get("ltp"), "cp": v.get("cp")}
        return out

    def _layers(self, feed: dict, unds: dict, signals: dict) -> list[dict]:
        have_chain = any(u.get("chains") for u in unds.values())
        connected = feed.get("connected", False)
        senses = "on" if have_chain and connected else ("partial" if have_chain else "off")
        sb = signals.get("scoreboard") or {}
        judging = bool(self.engine) and connected
        return [
            {"id": "senses", "name": "Senses", "status": senses,
             "detail": "Live IV, Greeks, moneyness, OI, flow, big prints, candles" if have_chain else "Waiting for data"},
            {"id": "memory", "name": "Memory", "status": "learning" if self.sessions_recorded else "off",
             "detail": f"{self.sessions_recorded} session(s) recorded; candles + 30-min feature history in RAM"},
            {"id": "regime", "name": "Regime", "status": "off", "detail": "Not trained (needs recorded sessions)"},
            {"id": "cortex", "name": "Cortex", "status": "off", "detail": "ML vol/direction models not trained yet"},
            {"id": "judgement", "name": "Judgement", "status": "learning" if judging else "off",
             "detail": "EXPERIMENTAL rule engine: 7-signal confluence + trigger + gates" if judging else "Runs while recording live"},
            {"id": "reflex", "name": "Reflex", "status": "on", "detail": "Gates armed: window, cooldown, spread, depth, R:R, freshness"},
            {"id": "mirror", "name": "Mirror", "status": "on", "detail": f"Scoreboard: {sb.get('closed', 0)} closed of {sb.get('calls', 0)} calls"},
        ]


def _round(x, nd: int = 6):
    """Trim float noise so the 4 Hz stream stays small."""
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: _round(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v, nd) for v in x]
    return x


def count_recorded_sessions(data_dir: Path) -> int:
    n = 0
    for p in (data_dir / "sessions").glob("session_*.json"):
        try:
            if json.loads(p.read_text()).get("rows_written", 0) > 10_000:
                n += 1
        except (OSError, json.JSONDecodeError):
            pass
    return n


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str))
    os.replace(tmp, path)
