"""ExecutionHub: feeds every call into the PAPER book (always) and the REAL book (only when allowed).

Runs inside the signal engine's step (engine.on_step / engine.on_event), so live trading and replays use the same
code. Paper is never affected by anything that happens in real (manual exits, loss cap, switch off, funds).

Real orders need ALL of: switch on (owner typed the phrase) · broker connected for today · enough funds ·
today's real loss under the cap · open/order limits · inside the entry window · broker price close to ours ·
contract mapped. Otherwise the real leg is recorded as SKIPPED with the reason and the paper leg goes on.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, time as dtime
from pathlib import Path

from tbrain.execution.book import BookConfig, TradeBook
from tbrain.execution.broker import BrokerError, PaperBroker
from tbrain.timeutil import IST

SQUARE_OFF = dtime(15, 20)
CONFIRM_PHRASE = "yes use my real money"


class ExecutionHub:
    def __init__(self, s, store, log=print, real_broker=None, store_dir: Path | None = None, now_fn=None,
                 on_event=None, control_poll_s: float = 2.0):
        self.s, self.store, self.log = s, store, log
        self.now_fn = now_fn or (lambda: time.time() * 1000)
        self._emit = on_event
        base = dict(lots=s.real_lots, max_open=s.real_max_open, max_orders=s.real_max_orders,
                    cost_per_lot_inr=s.cost_per_lot_inr, safety_stop_r=s.real_safety_stop_r,
                    entry_timeout_s=s.real_entry_timeout_s)
        self.paper = TradeBook("paper", PaperBroker(now_fn=self.now_fn), BookConfig(**base), log=log,
                               store_dir=store_dir, on_event=self._emit, now_fn=self.now_fn)
        self.real = (TradeBook("real", real_broker, BookConfig(**base, confirm_each=s.real_confirm_each), log=log,
                               store_dir=store_dir, on_event=self._emit, now_fn=self.now_fn,
                               quote_fn=lambda k: self._quotes.get(k)) if real_broker else None)
        self._connected = False
        self._real_snap = None
        self.real_on = False
        self.why_off = None
        self._seen_cmds: set[str] = set()
        self._last_poll = -1e18
        self._quotes: dict[str, dict] = {}
        self._mismatch = 0
        self._last_reconcile = -1e18
        self._skips: dict[str, dict] = {}     # call_id -> real skip record when there is no real book at all
        self._day = None
        self._rday = None
        self._lock = threading.RLock()
        self._async = bool(real_broker is not None and getattr(real_broker, "remote", False))
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._poll(force=True)
        if self._async:          # real orders go over the network: never block the brain's 250 ms loop
            threading.Thread(target=self._worker, daemon=True, name="real-orders").start()

    # ------------------------------------------------------------------ control (switch, cap, commands)
    def loss_cap(self) -> float:
        r = self.store.get_real() if self.store else {}
        cap = r.get("loss_cap")
        try:
            cap = float(cap) if cap is not None else float(self.s.real_daily_loss_cap)
        except (TypeError, ValueError):
            cap = float(self.s.real_daily_loss_cap)
        return max(0.0, min(cap, float(self.s.real_loss_cap_max)))

    def _poll(self, force: bool = False) -> None:
        now = self.now_fn()
        if not self.store or (not force and now - self._last_poll < 2000):
            return
        self._last_poll = now
        try:
            r = self.store.get_real()
            want = bool(r.get("on"))
            if want and not self.real_on:
                if self.real is None:
                    self._set_off("broker not connected")
                else:
                    self.real_on, self.why_off = True, None
                    self.log("[real] real money ON (from the next new call)")
            elif not want and self.real_on:
                self._turn_off(r.get("why_off") or "switched off")
            for c in self.store.commands():
                if c["id"] in self._seen_cmds:
                    continue
                self._seen_cmds.add(c["id"])
                if time.time() * 1000 - c.get("ms", 0) > 600_000:
                    continue                        # stale command from an earlier run (wall clock: set by the web app)
                self._command(c)
        except Exception as e:  # noqa: BLE001 - control problems must never stop the brain
            self.log(f"[real] control read failed: {e!r}")

    def _command(self, c: dict) -> None:
        if not self.real:
            return
        if c["kind"] == "exit" and c.get("call_id"):
            self.real.exit(f"real:{c['call_id']}", "manual exit")
            self.log(f"[real] manual exit requested for {c['call_id']}")
        elif c["kind"] == "exit_all":
            self.real.exit_all("manual exit all")
            self.log("[real] manual exit-all requested")
        elif c["kind"] == "approve" and c.get("call_id"):
            self.real.approve(c["call_id"])

    def _set_off(self, why: str) -> None:
        self.real_on, self.why_off = False, why
        if self.store:
            try:
                self.store.set_real(on=False, why_off=why)
            except Exception:  # noqa: BLE001
                pass

    def _turn_off(self, why: str) -> None:
        """Real money OFF: no new real orders, close every open real position at the current price, then
        disconnect the broker session once flat. Paper is untouched."""
        self.log(f"[real] real money OFF ({why}) - closing open real positions")
        self._set_off(why)
        if self.real:
            self.real.exit_all(why)
            self.real.tick()
            self._disconnect_when_flat = True

    # ------------------------------------------------------------------ engine hooks
    def on_step(self, now_ms: float, ctxs: dict) -> None:
        quotes = {}
        for ctx in (ctxs or {}).values():
            quotes.update(ctx.get("quotes") or {})
        self._quotes.update(quotes)
        self.paper.broker.on_quotes(quotes, now_ms)
        self.tick(now_ms)

    def on_event(self, event: str, call: dict) -> None:
        if call.get("shadow"):
            return
        try:
            if event == "issue":
                self.paper.on_issue(call, gate=self._paper_gate)
            elif event == "t1":
                self.paper.on_t1(call)
            elif event == "close":
                self.paper.on_close(call)
        except Exception as e:  # noqa: BLE001
            self.log(f"[exec] paper {event} failed: {e!r}")
        if self.real is None:
            if event == "issue":
                self._skips[call["id"]] = {"state": "skipped", "reason": "broker not connected"}
            return
        if self._async:
            self._q.put((event, dict(call)))
        else:
            self._real_event(event, call)

    def _real_event(self, event: str, call: dict) -> None:
        with self._lock:
            try:
                if event == "issue":
                    self.real.on_issue(call, gate=self._real_gate)
                elif event == "t1":
                    self.real.on_t1(call)
                elif event == "close":
                    self.real.on_close(call)
            except Exception as e:  # noqa: BLE001
                self.log(f"[real] {event} failed: {e!r}")
            self._real_snap = self._real_part()

    def _worker(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            try:
                ev, call = self._q.get(timeout=0.25)
                self._real_event(ev, call)
            except queue.Empty:
                pass
            if time.monotonic() - last >= 1.0:
                last = time.monotonic()
                try:
                    self._real_cycle(self.now_fn())
                except Exception as e:  # noqa: BLE001
                    self.log(f"[real] cycle failed: {e!r}")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ gates
    def _in_window(self, now_ms: float) -> bool:
        t = datetime.fromtimestamp(now_ms / 1000, IST).time()
        return t < dtime(15, 15)

    def _paper_gate(self, t: dict, call: dict):
        if not self._in_window(self.now_fn()):
            return False, "too close to the 15:20 square-off"
        q = self.paper.broker.quote(t["instrument"]) or {}
        if not q.get("ap1"):
            return False, "no live ask for this option"
        return True, None

    def real_pnl_today(self) -> float:
        if not self.real:
            return 0.0
        return self.real.realized_pnl() + self.real.unrealized_pnl()

    def _real_gate(self, t: dict, call: dict):
        b = self.real.broker
        if not self.real_on:
            return False, self.why_off or "real money is off"
        if not getattr(b, "connected", False):
            return False, "broker not connected today"
        if not self._in_window(self.now_fn()):
            return False, "too close to the 15:20 square-off"
        cap = self.loss_cap()
        if self.real_pnl_today() <= -cap:
            return False, f"daily loss cap ₹{cap:,.0f} reached"
        if hasattr(b, "supports") and not b.supports(t["instrument"]):
            return False, "contract not mapped on the broker"
        if hasattr(b, "matches"):
            ok, why = b.matches(t["instrument"], call)
            if not ok:
                return False, f"contract check failed: {why}"
        ours = (self._quotes.get(t["instrument"]) or {}).get("ap1")
        try:
            theirs = (b.quote(t["instrument"]) or {}).get("ap1")
        except BrokerError as e:
            return False, f"broker quote failed: {e}"
        if not ours or not theirs:
            return False, "no live price for this option"
        gap = abs(theirs - ours) / ours * 100
        if gap > self.s.real_max_price_gap_pct:
            return False, f"broker price {theirs:.2f} vs ours {ours:.2f} ({gap:.1f}% apart)"
        try:
            funds = b.funds()
        except BrokerError as e:
            return False, f"funds check failed: {e}"
        need = theirs * t["qty"] * 1.05 + self.s.cost_per_lot_inr * t["lots"]
        if funds is None or funds < need:
            return False, (f"needs ₹{need:,.0f}, available ₹{funds:,.0f}" if funds is not None else "funds unknown")
        return True, None

    # ------------------------------------------------------------------ loop
    def tick(self, now_ms: float | None = None) -> None:
        now_ms = now_ms or self.now_fn()
        day = datetime.fromtimestamp(now_ms / 1000, IST).date()
        if self._day != day:                   # new trading day: reset per-day order counters
            self._day = day
            self.paper.orders_today = 0
        if datetime.fromtimestamp(now_ms / 1000, IST).time() >= SQUARE_OFF:
            self.paper.exit_all("15:20 square-off")
        self.paper.tick(now_ms)
        if not self._async:
            self._real_cycle(now_ms)

    def _real_cycle(self, now_ms: float) -> None:
        with self._lock:
            self._real_cycle_locked(now_ms)
            if self.real is not None:
                self._connected = bool(getattr(self.real.broker, "connected", False))
            self._real_snap = self._real_part()

    def _real_cycle_locked(self, now_ms: float) -> None:
        self._poll()
        if self.real is None:
            return
        day = datetime.fromtimestamp(now_ms / 1000, IST).date()
        if self._rday != day:
            self._rday = day
            self.real.orders_today = 0
        if datetime.fromtimestamp(now_ms / 1000, IST).time() >= SQUARE_OFF:
            self.real.exit_all("15:20 square-off")
        if self.real_on and self.real_pnl_today() <= -self.loss_cap():
            self._turn_off(f"daily loss cap ₹{self.loss_cap():,.0f} reached")
        self.real.tick(now_ms)
        if getattr(self, "_disconnect_when_flat", False) and not self.real.open_trades():
            self._disconnect_when_flat = False
            try:
                if hasattr(self.real.broker, "disconnect"):
                    self.real.broker.disconnect()
                    self.log("[real] broker session deleted - connect again to trade real money")
            except Exception as e:  # noqa: BLE001
                self.log(f"[real] disconnect failed: {e!r}")
        if now_ms - self._last_reconcile >= 5000:
            self._last_reconcile = now_ms
            self._reconcile()

    def _reconcile(self) -> None:
        """Our record vs the broker's positions. Two mismatches in a row -> real money off + alert."""
        exp: dict[str, int] = {}
        for t in self.real.open_trades():
            exp[t["instrument"]] = exp.get(t["instrument"], 0) + self.real.held(t)
        try:
            bad = {k: (v, self.real.broker.position(k)) for k, v in exp.items() if self.real.broker.position(k) != v}
        except BrokerError:
            return
        settling = any(t["state"] in ("entry", "exiting") for t in self.real.open_trades())
        if bad and not settling:
            self._mismatch += 1
            if self._mismatch >= 2:
                self.log(f"[real] POSITION MISMATCH {bad} - switching real money off")
                self._turn_off(f"position mismatch with broker: {bad}")
                self._mismatch = 0
        else:
            self._mismatch = 0

    def snapshot(self) -> dict:
        """Cheap and never blocks on the network: the real side is the copy the order thread made last cycle."""
        if self._async:
            real = self._real_snap or self._real_part(empty=True)
        else:
            with self._lock:
                if self.real is not None:
                    self._connected = bool(getattr(self.real.broker, "connected", False))
                real = self._real_part()
        return {"paper": self.paper.snapshot(), **real}

    def _real_part(self, empty: bool = False) -> dict:
        b = self.real.broker if self.real else None
        if self.real and not empty:
            real = self.real.snapshot()
        else:
            real = {"trades": dict(self._skips), "realized_pnl": 0, "unrealized_pnl": 0, "open": 0, "orders_today": 0}
        return {"real": real,
                "real_status": {"on": self.real_on, "why_off": self.why_off, "connected": self._connected,
                                "broker": getattr(b, "name", None), "loss_cap": self.loss_cap(),
                                "loss_cap_max": float(self.s.real_loss_cap_max), "pnl_today": round(self.real_pnl_today(), 2),
                                "confirm_each": bool(self.s.real_confirm_each), "lots": self.s.real_lots,
                                "max_open": self.s.real_max_open, "confirm_phrase": CONFIRM_PHRASE}}
