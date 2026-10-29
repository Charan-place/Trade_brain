"""TradeBook: one book (paper or real) following each call from entry to exit.

States:  approval -> entry -> open -> exiting -> closed      (or: skipped / missed / closed early)
  approval  real only, confirm-each mode: waiting for the owner to tap Approve (expires)
  entry     LIMIT BUY working at the ask, capped at the top of the call's entry zone; never chases
  open      filled; a protective SL_M SELL rests at the broker (safety stop, default -2R on premium)
  exiting   exit requested (signal close, manual Exit, loss cap, switch off, 15:20): cancel the stop FIRST,
            then sell what is still held - so we can never sell twice or go short
Invariants (tested): never sell more than held; one exit sequence at a time; every filled position has a stop.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from tbrain.execution.broker import CANCELLED, DONE, FILLED, OPEN, REJECTED, BrokerError, tick_down, tick_up

ACTIVE = ("approval", "entry", "open", "exiting")


@dataclass
class BookConfig:
    lots: int = 1
    max_open: int = 2
    max_orders: int = 20
    entry_timeout_s: float = 20.0
    approval_timeout_s: float = 45.0
    safety_stop_r: float = 2.0           # broker-side stop distance in R (premium); exits normally follow the signal
    cost_per_lot_inr: float = 40.0
    confirm_each: bool = False


class TradeBook:
    def __init__(self, name: str, broker, cfg: BookConfig | None = None, log=print, store_dir: Path | None = None,
                 on_event=None, now_fn=None, quote_fn=None):
        self.name, self.broker, self.cfg, self.log = name, broker, cfg or BookConfig(), log
        self.quote_fn = quote_fn          # where live prices come from (real: the brain's own feed, never a network call)
        self.now_fn = now_fn or (lambda: time.time() * 1000)
        self.trades: dict[str, dict] = {}
        self.orders_today = 0
        self.store = (Path(store_dir) / f"{name}.jsonl") if store_dir else None
        self.on_event = on_event
        self.errors = 0

    # ------------------------------------------------------------------ helpers
    def _rec(self, t: dict, event: str, **extra) -> None:
        now = self.now_fn()
        t["updated_ms"] = now
        t.setdefault("events", []).append({"event": event, "ms": now, **extra})
        if self.store:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            with self.store.open("a") as f:
                f.write(json.dumps({"event": event, "ms": now, "trade": t}, default=str) + "\n")
        if self.on_event:
            try:
                self.on_event(self.name, event, t)
            except Exception:  # noqa: BLE001
                pass

    def open_trades(self) -> list[dict]:
        return [t for t in self.trades.values() if t["state"] in ACTIVE]

    def held(self, t: dict) -> int:
        return max(0, t["filled_qty"] - t["exit_qty"])

    def _call_quote(self, t: dict) -> dict:
        if self.quote_fn is not None:
            return self.quote_fn(t["instrument"]) or {}
        return self.broker.quote(t["instrument"]) or {}

    def realized_pnl(self) -> float:
        return sum(t["pnl"] for t in self.trades.values() if t["state"] == "closed" and t.get("pnl") is not None)

    def unrealized_pnl(self) -> float:
        tot = 0.0
        for t in self.open_trades():
            h = self.held(t)
            bid = (self._call_quote(t) or {}).get("bp1")
            if h and bid and t.get("entry_price"):
                tot += (bid - t["entry_price"]) * h
        return tot

    # ------------------------------------------------------------------ signal hooks
    def on_issue(self, call: dict, gate=None, now_ms: float | None = None) -> dict:
        tid = f"{self.name}:{call['id']}"
        if tid in self.trades:
            return self.trades[tid]
        lots = max(1, int(self.cfg.lots))
        t = {"id": tid, "book": self.name, "call_id": call["id"], "und": call.get("und"), "symbol": call.get("symbol"),
             "instrument": call["instrument_key"], "lot_size": int(call.get("lot") or 1), "lots": lots,
             "qty": lots * int(call.get("lot") or 1), "zone": call.get("entry_zone"), "signal_entry": call.get("entry"),
             "p_sl": call.get("p_sl"), "dir": call.get("dir"), "state": "skipped", "reason": None, "filled_qty": 0, "exit_qty": 0,
             "exit_value": 0.0, "entry_price": None, "exit_price": None, "pnl": None, "entry_order": None,
             "stop_order": None, "exit_orders": [], "created_ms": now_ms or self.now_fn(), "closed_ms": None,
             "exit_reason": None, "approval_expires_ms": None}
        self.trades[tid] = t
        if len(self.open_trades()) >= self.cfg.max_open:
            t["reason"] = f"max {self.cfg.max_open} open positions"
        elif self.orders_today >= self.cfg.max_orders:
            t["reason"] = f"max {self.cfg.max_orders} orders today"
        elif gate is not None:
            ok, why = gate(t, call)
            if not ok:
                t["reason"] = why
        if t["reason"]:
            self._rec(t, "skipped", reason=t["reason"])
            return t
        if self.cfg.confirm_each:
            t["state"] = "approval"
            t["approval_expires_ms"] = t["created_ms"] + self.cfg.approval_timeout_s * 1000
            self._rec(t, "awaiting_approval")
            return t
        self._enter(t)
        return t

    def approve(self, call_id: str) -> dict | None:
        t = self.trades.get(f"{self.name}:{call_id}")
        if t and t["state"] == "approval":
            self._rec(t, "approved")
            self._enter(t)
        return t

    def on_t1(self, call: dict) -> None:
        t = self.trades.get(f"{self.name}:{call['id']}")
        if not t or t["state"] != "open" or not t.get("stop_order") or not t.get("entry_price"):
            return
        new = tick_down(t["entry_price"] - (t["entry_price"] - (t["p_sl"] or t["entry_price"] * 0.9)))  # -1R
        try:
            o = self.broker.get(t["stop_order"])
            if o.status == OPEN and (o.trigger or 0) < new:
                self.broker.modify(o.id, trigger=new)
                self._rec(t, "stop_raised", trigger=new)
        except BrokerError as e:
            self._err(t, e)

    def on_close(self, call: dict, reason: str | None = None) -> None:
        t = self.trades.get(f"{self.name}:{call['id']}")
        if t:
            self.exit(t["id"], reason or f"signal {call.get('outcome') or 'close'}")

    # ------------------------------------------------------------------ actions
    def _enter(self, t: dict) -> None:
        q = self._call_quote(t)
        ask = q.get("ap1")
        hi = (t["zone"] or [None, None])[1] or t["signal_entry"]
        limit = tick_up(min(ask, hi) if ask and hi else (ask or hi))
        try:
            o = self.broker.place(ref=f"{t['id']}:E", instrument=t["instrument"], side="BUY", qty=t["qty"],
                                  kind="LIMIT", price=limit)
        except BrokerError as e:
            t["state"], t["reason"] = "skipped", f"broker error: {e}"
            self._rec(t, "skipped", reason=t["reason"])
            return
        self.orders_today += 1
        t["entry_order"], t["state"], t["entry_limit"] = o.id, "entry", limit
        self._rec(t, "entry_sent", limit=limit, order=o.to_json())
        if o.status == REJECTED:
            t["state"], t["reason"] = "skipped", f"entry rejected: {o.reason}"
            self._rec(t, "skipped", reason=t["reason"])
        else:
            self._sync(t)

    def exit(self, trade_id: str, reason: str) -> dict | None:
        t = self.trades.get(trade_id)
        if not t or t["state"] not in ACTIVE:
            return t
        t["exit_reason"] = t.get("exit_reason") or reason
        if t["state"] == "approval":
            t["state"], t["reason"] = "skipped", f"not approved ({reason})"
            self._rec(t, "skipped", reason=t["reason"])
            return t
        if t["state"] == "entry":
            try:
                self.broker.cancel(t["entry_order"])
            except BrokerError as e:
                self._err(t, e)
        t["state"] = "exiting"
        self._rec(t, "exit_requested", reason=reason)
        self._sync(t)
        return t

    def exit_all(self, reason: str) -> None:
        for t in self.open_trades():
            self.exit(t["id"], reason)

    # ------------------------------------------------------------------ the loop
    def tick(self, now_ms: float | None = None) -> None:
        now_ms = now_ms or self.now_fn()
        for t in self.open_trades():
            if t["state"] == "approval" and now_ms >= (t["approval_expires_ms"] or 0):
                t["state"], t["reason"] = "skipped", "approval timed out"
                self._rec(t, "skipped", reason=t["reason"])
                continue
            if t["state"] == "entry" and now_ms - t["created_ms"] >= self.cfg.entry_timeout_s * 1000:
                self._sync(t)
                if t["state"] == "entry":
                    try:
                        self.broker.cancel(t["entry_order"])
                    except BrokerError as e:
                        self._err(t, e)
                        continue
                    self._sync(t)
                    if t["state"] == "entry" and t["filled_qty"] == 0:
                        t["state"], t["reason"] = "missed", "entry not filled in time (no chasing)"
                        self._rec(t, "missed")
                    continue
            self._sync(t)

    def _count(self, t: dict) -> bool:
        """Pull fills from the broker into the trade (idempotent). Returns True when the entry order is done."""
        entry_done = True
        if t["entry_order"]:
            e = self.broker.get(t["entry_order"])
            if e.filled_qty > t["filled_qty"]:
                t["filled_qty"], t["entry_price"] = e.filled_qty, e.avg_price
                self._rec(t, "filled", qty=e.filled_qty, price=e.avg_price)
            entry_done = e.status in DONE
            t["_entry_status"], t["_entry_reason"] = e.status, e.reason
        for oid in t.get("stop_history", []):
            so = self.broker.get(oid)
            counted = t.setdefault("stop_counted_by", {}).get(oid, 0)
            if so.filled_qty > counted:
                t["stop_counted_by"][oid] = so.filled_qty
                t["exit_qty"] += so.filled_qty - counted
                t["exit_value"] += (so.filled_qty - counted) * (so.avg_price or 0)
                t["exit_reason"] = t.get("exit_reason") or "safety stop at broker"
                self._rec(t, "stop_filled", qty=so.filled_qty - counted, price=so.avg_price)
        for oid in t["exit_orders"]:
            x = self.broker.get(oid)
            counted = t.setdefault("exit_counted", {}).get(oid, 0)
            if x.filled_qty > counted:
                t["exit_counted"][oid] = x.filled_qty
                t["exit_qty"] += x.filled_qty - counted
                t["exit_value"] += (x.filled_qty - counted) * (x.avg_price or 0)
                self._rec(t, "exit_filled", qty=x.filled_qty - counted, price=x.avg_price)
        return entry_done

    def _sync(self, t: dict) -> None:
        """Advance one trade from the broker's view of its orders. Safe to call repeatedly."""
        try:
            entry_done = self._count(t)
            if t["state"] == "entry":
                if entry_done and t["filled_qty"] == 0:
                    why = (t.get("_entry_status") or "").lower()
                    t["state"], t["reason"] = "missed", f"entry {why}{': ' + t['_entry_reason'] if t.get('_entry_reason') else ''}"
                    self._rec(t, "missed")
                    return
                if entry_done and t["filled_qty"] > 0:
                    t["state"] = "open"
            if t["state"] == "exiting" and entry_done and t["filled_qty"] == 0:
                t["state"], t["reason"] = "missed", f"cancelled before fill ({t.get('exit_reason')})"
                self._rec(t, "missed")
                return
            if t["state"] == "open" and self.held(t) > 0:
                self._ensure_stop(t)
            if t["state"] == "exiting":
                self._advance_exit(t)
                entry_done = self._count(t)
            if t["state"] in ("open", "exiting") and t["filled_qty"] > 0 and self.held(t) == 0 and entry_done:
                self._finish(t)
        except BrokerError as e:
            self._err(t, e)

    def _ensure_stop(self, t: dict) -> None:
        if t["state"] == "exiting":
            return
        held = self.held(t)
        if t["stop_order"]:
            s = self.broker.get(t["stop_order"])
            if s.status == OPEN and s.qty - s.filled_qty == held:
                return
            if s.status == OPEN:                       # entry topped up (partial fills): resize the stop
                self.broker.cancel(s.id)
        entry = t["entry_price"]
        risk = max(entry - (t["p_sl"] or entry * 0.9), 0.05)
        trig = tick_down(entry - self.cfg.safety_stop_r * risk)
        n = len(t.setdefault("stop_history", []))
        o = self.broker.place(ref=f"{t['id']}:S{n}", instrument=t["instrument"], side="SELL", qty=held, kind="SL_M",
                              trigger=trig)
        t["stop_history"].append(o.id)
        t["stop_order"] = o.id
        self._rec(t, "stop_placed", trigger=trig, qty=held, order=o.to_json())

    def _advance_exit(self, t: dict) -> None:
        if t["entry_order"]:
            e = self.broker.get(t["entry_order"])
            if e.status == OPEN:
                self.broker.cancel(e.id)                # keep asking until the broker confirms the cancel
                if self.broker.get(e.id).status == OPEN:
                    return                              # nothing is sold while the entry can still fill
                self._count(t)
        if t["stop_order"]:
            s = self.broker.get(t["stop_order"])
            if s.status == OPEN:
                self.broker.cancel(s.id)
                s = self.broker.get(s.id)
                if s.status == OPEN:
                    return                              # cancel not confirmed yet: never sell while the stop is live
        if any(self.broker.get(o).status == OPEN for o in t["exit_orders"]):
            return
        held = self.held(t)
        held = min(held, max(0, self.broker.position(t["instrument"])))
        if held <= 0:
            return
        n = len(t["exit_orders"])
        o = self.broker.place(ref=f"{t['id']}:X{n}", instrument=t["instrument"], side="SELL", qty=held, kind="MARKET")
        t["exit_orders"].append(o.id)
        self.orders_today += 1
        self._rec(t, "exit_sent", qty=held, order=o.to_json())

    def _finish(self, t: dict) -> None:
        t["state"], t["closed_ms"] = "closed", self.now_fn()
        t["exit_price"] = round(t["exit_value"] / t["exit_qty"], 4) if t["exit_qty"] else None
        cost = self.cfg.cost_per_lot_inr * t["lots"]
        t["pnl"] = round((t["exit_value"] - (t["entry_price"] or 0) * t["filled_qty"]) - cost, 2)
        self._rec(t, "closed", pnl=t["pnl"], exit_price=t["exit_price"], reason=t.get("exit_reason"))

    def _err(self, t: dict, e: Exception) -> None:
        self.errors += 1
        t["last_error"] = str(e)[:200]
        self.log(f"[{self.name}] broker problem on {t['symbol']}: {e}")

    def snapshot(self) -> dict:
        keep = ("id", "call_id", "book", "und", "symbol", "state", "reason", "lots", "qty", "filled_qty", "exit_qty",
                "entry_price", "exit_price", "pnl", "exit_reason", "created_ms", "closed_ms", "entry_limit",
                "approval_expires_ms", "last_error")
        return {"trades": {t["call_id"]: {k: t.get(k) for k in keep} for t in self.trades.values()},
                "realized_pnl": round(self.realized_pnl(), 2), "unrealized_pnl": round(self.unrealized_pnl(), 2),
                "open": len(self.open_trades()), "orders_today": self.orders_today}
