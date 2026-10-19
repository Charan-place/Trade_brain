"""Records the live feed for each configured underlying (NIFTY on NSE_FO, SENSEX on BSE_FO):
index + nearest future + option chain (ATM +/- n strikes x nearest n expiries), all in 'full' mode,
plus a REST option-chain snapshot (adds prev_oi, pcr, pop) every chain_poll_s for the nearest expiry.

Strikes are only ever ADDED during a session (re-centred as spot moves) so no series is cut mid-day.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from tbrain.brain.state import BrainState, count_recorded_sessions, write_state
from tbrain.brokers.upstox import instruments as ins
from tbrain.brokers.upstox.feed import LiveFeed
from tbrain.brokers.upstox.normalize import normalize, parse_market_info
from tbrain.brokers.upstox.rest import RestError, fetch_option_chain, flatten_chain
from tbrain.collector.health import Health
from tbrain.collector.store import CHAIN_SCHEMA, TICK_SCHEMA, ParquetStore
from tbrain.config import Settings
from tbrain.timeutil import IST, MARKET_CLOSE, at_ist, ist_date, now_ist

BRAIN_TICK_S = 0.25      # recompute + push to the UI 4x per second
STATE_FILE_EVERY_S = 5   # on-disk copy for standalone `tbrain ui` / after the session ends

RECENTER_CHECK_S = 10


class Recorder:
    def __init__(self, s: Settings, token_provider: Callable[[], str], *, write: bool = True,
                 capture_raw: int = 0, n_strikes: int | None = None, n_expiries: int | None = None, log=print,
                 ui_port: int | None = None, open_browser: bool = False):
        self.s, self.log = s, log
        self.token_provider = token_provider
        self.n_strikes = s.n_strikes if n_strikes is None else n_strikes
        self.n_expiries = s.n_expiries if n_expiries is None else n_expiries
        self.write, self.capture_raw = write, capture_raw
        self.health = Health()
        self.q: queue.Queue = queue.Queue(maxsize=100_000)
        self.ticks = ParquetStore(s.data_dir / "ticks", TICK_SCHEMA, s.flush_s) if write else None
        self.chain = ParquetStore(s.data_dir / "chain", CHAIN_SCHEMA, s.flush_s) if write else None
        self.raw: list[dict] = []
        self.market_status: dict[str, str] = {}
        self.spot: dict[str, float] = {}           # underlying -> last spot
        self.index_keys: dict[str, str] = {}       # underlying -> index instrument key
        self._key_to_und: dict[str, str] = {}
        self._center: dict[str, float] = {}
        self._stop = threading.Event()
        self.brain: BrainState | None = None
        self.ui_port, self.open_browser = ui_port, open_browser
        self.ui = None
        self.bc = None
        self.note = ""
        self.feed = LiveFeed(token_provider, self._on_msg, self._on_state, tls_verify=s.tls_verify, stale_s=s.stale_s)

    # ---- callbacks (feed thread) -------------------------------------
    def _on_state(self, ev: str, detail: str) -> None:
        self.log(f"[feed] {ev} {detail}".rstrip())
        if ev == "auth_failed":
            self._stop.set()

    def _on_msg(self, msg: dict, recv_ns: int) -> None:
        if self.capture_raw and len(self.raw) < self.capture_raw:
            self.raw.append(msg)
        if msg.get("type") == "market_info":
            st = parse_market_info(msg)
            if st != self.market_status:
                self.market_status = st
                self.log(f"[market] {st}")
            return
        rows = normalize(msg, recv_ns)
        if not rows:
            return
        if self.brain is not None:
            self.brain.note_message(rows[0].get("cur_ts_ms"), recv_ns)
        self.health.observe(rows, recv_ns)
        if self.brain is not None:
            self.brain.ingest(rows)
        for r in rows:
            und = self._key_to_und.get(r["instrument_key"])
            if und and r["instrument_key"] == self.index_keys.get(und):
                px = r["ltp"] or r["cp"]
                if px:
                    self.spot[und] = px
        if self.ticks is not None:
            try:
                self.q.put_nowait(rows)
            except queue.Full:
                self.health.drops += len(rows)

    # ---- writer thread -----------------------------------------------
    def _writer(self) -> None:
        while not (self._stop.is_set() and self.q.empty()):
            try:
                rows = self.q.get(timeout=1)
                self.ticks.add(rows)
            except queue.Empty:
                pass
            self.ticks.maybe_flush()
        self.ticks.flush()

    # ---- helpers -----------------------------------------------------
    def _sub(self, keys: list[str], und: str) -> None:
        room = self.s.max_keys - len(self.feed.subscribed())
        keys = [k for k in keys if k not in self.feed.subscribed()][:max(0, room)]
        for k in keys:
            self._key_to_und[k] = und
        if keys:
            self.feed.subscribe(keys, "full")

    def _chain_keys(self, contracts, und: str, spot: float) -> list[str]:
        legs = ins.select_chain(contracts, und, spot, ist_date(), self.n_expiries, self.n_strikes)
        return [c.instrument_key for c in legs]

    def _strike_step(self, contracts, und: str) -> float:
        exps = ins.option_expiries(contracts, und, ist_date())
        if not exps:
            return 0.0
        ks = sorted({c.strike for c in contracts if c.underlying == und and c.expiry == exps[0] and c.strike})
        diffs = sorted(b - a for a, b in zip(ks, ks[1:]) if b > a)
        return diffs[len(diffs) // 4] if diffs else 0.0  # lower-quartile step ~ the near-ATM spacing

    def _wait_spots(self, unds: list[str], timeout: float) -> None:
        end = time.time() + timeout
        while time.time() < end and not self._stop.is_set():
            if all(u in self.spot for u in unds):
                return
            time.sleep(0.2)

    def _poll_chain(self, contracts, und: str) -> None:
        exps = ins.option_expiries(contracts, und, ist_date())
        ukey = self.index_keys.get(und)
        if not exps or not ukey or self.chain is None:
            return
        try:
            payload = fetch_option_chain(self.token_provider(), ukey, exps[0])
            rows = flatten_chain(payload, und, time.time_ns())
            self.chain.add(rows)
            if self.brain is not None:
                self.brain.ingest_chain(rows)
        except (RestError, OSError) as e:
            self.log(f"[chain] {und}: {e}")

    def _sleep_until(self, dt: datetime) -> None:
        while not self._stop.is_set() and now_ist() < dt:
            self._stop.wait(min(30, (dt - now_ist()).total_seconds()))

    # ---- main --------------------------------------------------------
    def run(self, duration_s: float | None = None, respect_hours: bool = True) -> dict:
        today = ist_date()
        contracts = ins.load_contracts(self.s.instrument_urls, self.s.underlyings,
                                       self.s.data_dir / "instruments", today, log=self.log)
        unds = [u for u, _ in self.s.underlyings]
        base: dict[str, list[str]] = {}
        for und in unds:
            ik = ins.index_key_for(contracts, und)
            if not ik:
                self.log(f"WARNING: no underlying_key for {und} in instrument file; skipping it")
                continue
            self.index_keys[und] = ik
            fut = ins.nearest_future(contracts, und, today)
            base[und] = [ik] + ([fut.instrument_key] if fut else [])
        if not base:
            raise RuntimeError("No underlyings resolved from the instrument file.")
        engine = None
        if self.write:  # live recording only: probes / replays never issue calls
            from tbrain.signals.engine import EngineConfig, SignalEngine
            engine = SignalEngine(EngineConfig(min_score=self.s.signal_min_score, cost_per_lot_inr=self.s.cost_per_lot_inr,
                                               risk_per_trade_inr=self.s.risk_per_trade_inr, require_vwap=self.s.require_vwap,
                                               trend_k=self.s.trend_k, loss_pause_n=self.s.loss_pause_n,
                                               regime_gate=self.s.regime_gate, premium_stop=self.s.premium_stop,
                                               fut_oi_fresh=self.s.fut_oi_fresh),
                                  store_dir=self.s.data_dir / "signals", log=self.log)
        self.brain = BrainState(contracts, self.index_keys, self.s.vix_key, self.s.risk_free,
                                count_recorded_sessions(self.s.data_dir), engine=engine)
        try:  # restart-safe: rebuild today's candles + VWAP from what was already recorded
            from tbrain.collector.backfill import backfill_today
            futs = {u: ins.nearest_future(contracts, u, today).instrument_key
                    for u in self.index_keys if ins.nearest_future(contracts, u, today)}
            keys = set(self.index_keys.values()) | set(futs.values())
            if engine:
                keys |= {c["instrument_key"] for c in engine.today_calls()}
            backfill_today(self.brain, self.s.data_dir / "ticks", today.isoformat(), keys, futs, log=self.log)
        except Exception as e:
            self.log(f"[backfill] skipped: {e!r}")
        self._start_ui()

        if respect_hours:
            if today.weekday() >= 5:
                raise RuntimeError("Weekend - market closed. Use `record --now --seconds 60` to test.")
            if now_ist() >= at_ist(today, MARKET_CLOSE):
                raise RuntimeError("Market closed for today. Use `record --now --seconds 60` to test.")
            start_at = at_ist(today, dtime(9, 0))
            if now_ist() < start_at:
                self.log(f"Waiting until {start_at:%H:%M} IST to start...")
                self.note = f"Waiting until {start_at:%H:%M} IST to connect"
                self._sleep_until(start_at)
                self.note = ""

        writer = None
        if self.ticks is not None:
            writer = threading.Thread(target=self._writer, daemon=True, name="writer")
            writer.start()
        self.feed.start()
        for und, keys in base.items():
            self._sub(keys + ([self.s.vix_key] if und == "NIFTY" else []), und)
        if not self.feed.wait_connected(20):
            self.log("WARNING: feed not connected after 20s (check token / network)")
        self._wait_spots(list(base), 30)
        for und in base:
            if und in self.spot:
                self._center[und] = self.spot[und]
                self._sub(self._chain_keys(contracts, und, self.spot[und]), und)
                self.log(f"{und}: spot {self.spot[und]:.2f}, index {self.index_keys[und]}")
            else:
                self.log(f"WARNING: no spot for {und} yet; chain will subscribe once it ticks")
        self.log(f"subscribed {len(self.feed.subscribed())} instruments")

        started = now_ist()
        end_at = started + timedelta(seconds=duration_s) if duration_s else None
        close_at = at_ist(today, MARKET_CLOSE) + timedelta(minutes=5)
        last_chain = last_report = last_recenter = 0.0
        steps = {u: self._strike_step(contracts, u) for u in base}
        try:
            while not self._stop.is_set():
                now = now_ist()
                if end_at and now >= end_at:
                    break
                if respect_hours and not end_at and now >= close_at:
                    break
                t = time.time()
                if t - last_recenter >= RECENTER_CHECK_S:
                    last_recenter = t
                    for und in base:
                        sp = self.spot.get(und)
                        if sp is None:
                            continue
                        c0 = self._center.get(und)
                        if c0 is None or (steps[und] and abs(sp - c0) >= 3 * steps[und]):
                            self._center[und] = sp
                            before = len(self.feed.subscribed())
                            self._sub(self._chain_keys(contracts, und, sp), und)
                            added = len(self.feed.subscribed()) - before
                            if added:
                                self.log(f"{und}: re-centred at {sp:.2f}, +{added} strikes")
                if self.chain is not None and t - last_chain >= self.s.chain_poll_s:
                    last_chain = t
                    for und in base:
                        self._poll_chain(contracts, und)
                    self.chain.maybe_flush()
                if t - last_report >= 30:
                    last_report = t
                    h = self.health.summary()
                    spots = " ".join(f"{u}={v:.1f}" for u, v in self.spot.items())
                    self.log(f"[health] msgs={h['msgs']} rows={h['rows']} keys={h['keys_seen']}/{len(self.feed.subscribed())} "
                             f"gaps={h['gaps_over_10s']} drops={h['drops']} reconnects={self.feed.reconnects} {spots}")
                self._stop.wait(1)
        except KeyboardInterrupt:
            self.log("stopping (Ctrl+C)...")
        finally:
            self._stop.set()
            self.feed.stop()
            self._stop_ui()
            if writer:
                writer.join(timeout=30)
            if self.chain is not None:
                self.chain.flush()
            self.write_live_state()
        return self._manifest(started, contracts)

    def feed_info(self) -> dict:
        age = (time.time_ns() - self.feed.last_msg_ns) / 1e9 if self.feed.last_msg_ns else None
        return {"connected": self.feed.wait_connected(0), "last_msg_age_s": age, "reconnects": self.feed.reconnects,
                "market_status": self.market_status, "subscribed": len(self.feed.subscribed()), **self.health.summary()}

    def write_live_state(self, snap: dict | None = None) -> None:
        if self.brain is None:
            return
        try:
            write_state(self.s.data_dir / "live" / "state.json", snap or self.brain.snapshot(self.feed_info(), mode="live"))
        except Exception as e:  # the UI must never break recording
            self.log(f"[state] {e!r}")

    # ---- embedded UI (push) -------------------------------------------
    def _start_ui(self) -> None:
        if not self.ui_port:
            return
        from tbrain.ui.server import Broadcaster, UIServer
        self.bc = Broadcaster()
        try:
            self.ui = UIServer(self.bc, self.ui_port).start()
        except OSError as e:
            self.log(f"[ui] port {self.ui_port} busy ({e}); recording continues without the UI")
            self.bc = None
            return
        self.log(f"[ui] Brain UI live at {self.ui.url} (updates every {int(BRAIN_TICK_S*1000)} ms)")
        threading.Thread(target=self._brain_loop, daemon=True, name="brain-tick").start()
        if self.open_browser:
            import webbrowser
            webbrowser.open(self.ui.url)

    def _brain_loop(self) -> None:
        last_file = 0.0
        nxt = time.monotonic()
        while not self._stop.is_set():
            try:
                info = self.feed_info()
                if self.note:
                    info["note"] = self.note
                snap = self.brain.snapshot(info, mode="live")
                self.bc.publish(snap)
                if time.monotonic() - last_file >= STATE_FILE_EVERY_S:
                    last_file = time.monotonic()
                    self.write_live_state(snap)
            except Exception as e:  # never let the UI loop kill recording
                self.log(f"[brain] {e!r}")
            nxt += BRAIN_TICK_S
            delay = nxt - time.monotonic()
            if delay < 0:          # compute took longer than a tick: skip ahead instead of piling up
                nxt = time.monotonic()
                delay = 0
            self._stop.wait(delay)

    def _stop_ui(self) -> None:
        if self.ui:
            self.ui.stop()

    def _manifest(self, started: datetime, contracts) -> dict:
        subs = sorted(self.feed.subscribed())
        by_key = {c.instrument_key: c for c in contracts}
        m = {
            "started": started.isoformat(), "ended": now_ist().isoformat(),
            "underlyings": {u: {"index_key": self.index_keys.get(u), "last_spot": self.spot.get(u)} for u in self.index_keys},
            "subscribed": len(subs),
            "subscribed_detail": [by_key[k].to_json() if k in by_key else {"instrument_key": k} for k in subs],
            "health": self.health.summary(),
            "feed": {"reconnects": self.feed.reconnects, "decode_errors": self.feed.decode_errors,
                     "callback_errors": self.feed.callback_errors, "fatal": self.feed.fatal},
            "rows_written": self.ticks.rows_written if self.ticks else 0,
            "chain_rows_written": self.chain.rows_written if self.chain else 0,
            "market_status": self.market_status,
        }
        if self.write:
            p = self.s.data_dir / "sessions" / f"session_{started:%Y-%m-%d_%H%M%S}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(m, indent=1, default=str))
        return m
