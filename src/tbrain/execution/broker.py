"""Broker interface + PaperBroker (simulated fills at the live bid/ask).

Order kinds: LIMIT, MARKET, SL_M (stop-loss market: SELL triggers when the bid falls to the trigger).
Every order carries a client reference (`ref`); placing the same ref twice returns the first order, so retries and
restarts can never duplicate an order.
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import asdict, dataclass, field

TICK = 0.05
OPEN, FILLED, CANCELLED, REJECTED = "OPEN", "FILLED", "CANCELLED", "REJECTED"
DONE = (FILLED, CANCELLED, REJECTED)


class BrokerError(RuntimeError):
    """Transient broker/network problem: the caller retries (refs keep retries idempotent)."""


def tick_up(p: float) -> float:
    return round(math.ceil(round(p / TICK, 6)) * TICK, 2)


def tick_down(p: float) -> float:
    return round(max(TICK, math.floor(round(p / TICK, 6)) * TICK), 2)


@dataclass
class Order:
    id: str
    ref: str
    instrument: str
    side: str            # BUY / SELL
    qty: int
    kind: str            # LIMIT / MARKET / SL_M
    price: float | None = None
    trigger: float | None = None
    status: str = OPEN
    filled_qty: int = 0
    avg_price: float | None = None
    reason: str | None = None
    placed_ms: float = 0.0
    updated_ms: float = 0.0
    triggered: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    def fill(self, qty: int, price: float, now_ms: float) -> None:
        qty = min(qty, self.qty - self.filled_qty)
        if qty <= 0:
            return
        tot = (self.avg_price or 0.0) * self.filled_qty + price * qty
        self.filled_qty += qty
        self.avg_price = round(tot / self.filled_qty, 4)
        self.status = FILLED if self.filled_qty >= self.qty else OPEN
        self.updated_ms = now_ms


class PaperBroker:
    """Fills against the latest quotes the brain sees (bp1/ap1/ltp). Buys fill at the ask, sells at the bid."""

    name = "paper"

    def __init__(self, now_fn=None):
        self.now_fn = now_fn or (lambda: time.time() * 1000)
        self.quotes: dict[str, dict] = {}
        self.orders: dict[str, Order] = {}
        self.by_ref: dict[str, str] = {}
        self.pos: dict[str, int] = {}
        self._ids = itertools.count(1)
        self.connected = True

    # ---- market data
    def on_quotes(self, quotes: dict[str, dict], now_ms: float | None = None) -> None:
        now_ms = now_ms or self.now_fn()
        for k, q in (quotes or {}).items():
            if q:
                self.quotes[k] = q
        for o in list(self.orders.values()):
            if o.status == OPEN:
                self._try_fill(o, now_ms)

    def quote(self, instrument: str) -> dict | None:
        return self.quotes.get(instrument)

    def funds(self) -> float | None:
        return None  # paper: unlimited

    def position(self, instrument: str) -> int:
        return self.pos.get(instrument, 0)

    def restore_position(self, instrument: str, qty: int) -> None:
        self.pos[instrument] = qty

    # ---- orders
    def place(self, *, ref: str, instrument: str, side: str, qty: int, kind: str,
              price: float | None = None, trigger: float | None = None) -> Order:
        if ref in self.by_ref:
            return self.orders[self.by_ref[ref]]
        now = self.now_fn()
        o = Order(id=f"P{next(self._ids)}", ref=ref, instrument=instrument, side=side, qty=int(qty), kind=kind,
                  price=price, trigger=trigger, placed_ms=now, updated_ms=now)
        self.orders[o.id], self.by_ref[ref] = o, o.id
        if qty <= 0:
            o.status, o.reason = REJECTED, "quantity must be positive"
        elif side == "SELL" and kind != "SL_M" and qty > self.position(instrument):
            o.status, o.reason = REJECTED, "would go short"
        else:
            self._try_fill(o, now)
        return o

    def cancel(self, order_id: str) -> Order:
        o = self.orders[order_id]
        if o.status == OPEN:
            o.status, o.updated_ms = CANCELLED, self.now_fn()
        return o

    def modify(self, order_id: str, *, price: float | None = None, trigger: float | None = None) -> Order:
        o = self.orders[order_id]
        if o.status != OPEN:
            return o
        if price is not None:
            o.price = price
        if trigger is not None:
            o.trigger = trigger
        o.updated_ms = self.now_fn()
        self._try_fill(o, o.updated_ms)
        return o

    def get(self, order_id: str) -> Order:
        return self.orders[order_id]

    def _try_fill(self, o: Order, now_ms: float) -> None:
        q = self.quotes.get(o.instrument) or {}
        bid, ask, ltp = q.get("bp1"), q.get("ap1"), q.get("ltp")
        rem = o.qty - o.filled_qty
        if rem <= 0:
            return
        px = None
        if o.side == "BUY":
            if ask and (o.kind == "MARKET" or (o.kind == "LIMIT" and o.price is not None and ask <= o.price + 1e-9)):
                px = ask
        else:
            if o.kind == "SL_M":
                ref_px = bid or ltp
                if ref_px and o.trigger is not None and ref_px <= o.trigger + 1e-9:
                    o.triggered = True
                if o.triggered and bid:
                    px = bid
            elif bid and (o.kind == "MARKET" or (o.kind == "LIMIT" and o.price is not None and bid >= o.price - 1e-9)):
                px = bid
            if px is not None:
                rem = min(rem, self.position(o.instrument))
                if rem <= 0:
                    o.status, o.reason, o.updated_ms = REJECTED, "no position to sell", now_ms
                    return
        if px is None:
            return
        o.fill(rem, px, now_ms)
        self.pos[o.instrument] = self.position(o.instrument) + (rem if o.side == "BUY" else -rem)
