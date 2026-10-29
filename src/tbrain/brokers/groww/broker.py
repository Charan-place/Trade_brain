"""GrowwBroker: the Broker interface (place / cancel / modify / get / position / funds / quote) on Groww's SDK.

Method and constant names come from the installed `growwapi` package (GrowwAPI.place_order, modify_order,
cancel_order, get_order_detail, get_order_status_by_reference, get_position_for_trading_symbol,
get_available_margin_details, get_quote, SEGMENT_FNO, EXCHANGE_NSE/BSE, PRODUCT_MIS, ORDER_TYPE_LIMIT/MARKET/
STOP_LOSS_MARKET, TRANSACTION_TYPE_BUY/SELL, VALIDITY_DAY) and Groww's API docs (order statuses, response fields).

Safety:
  * Contracts are mapped by EXCHANGE TOKEN (the number in Upstox's key, e.g. NSE_FO|35003) and double-checked
    against strike, expiry and CE/PE before any order. Anything that does not match exactly is refused.
  * Our refs become Groww `order_reference_id`s (8-20 alphanumeric). Before placing, we look the reference up:
    if a previous attempt reached Groww (e.g. the reply timed out), we return THAT order instead of placing again.
  * Quantity must be a whole number of lots and below the exchange freeze quantity.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import date, datetime

from tbrain.execution.broker import CANCELLED, FILLED, OPEN, REJECTED, BrokerError, Order

# Groww order_status -> our status (docs: Annexures > Order Status)
_DONE_FILLED = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}
_DONE_CANCELLED = {"CANCELLED"}
_DONE_REJECTED = {"REJECTED", "FAILED"}


def groww_ref(ref: str) -> str:
    """Stable 18-char alphanumeric reference for one of our refs (Groww allows 8-20 alphanumeric chars)."""
    return "TB" + hashlib.sha1(ref.encode()).hexdigest()[:16].upper()


def _payload(r):
    if isinstance(r, dict) and isinstance(r.get("payload"), dict):
        return r["payload"]
    return r or {}


class GrowwBroker:
    name = "groww"

    def __init__(self, api_factory, instruments: list[dict] | None = None, log=print, product: str | None = None,
                 cache_ms: float = 700):
        """api_factory(access_token) -> GrowwAPI-like client. `instruments` = rows of Groww's instrument CSV."""
        self.api_factory, self.log = api_factory, log
        self.api = None
        self.session_day: date | None = None
        self._rows_by_token: dict[tuple[str, str], dict] = {}
        self._orders: dict[str, Order] = {}
        self._fetched: dict[str, float] = {}
        self._lock = threading.RLock()
        self.cache_ms = cache_ms
        self.product = product
        if instruments:
            self.load_instruments(instruments)

    # ------------------------------------------------------------------ session
    @property
    def connected(self) -> bool:
        return self.api is not None and self.session_day == datetime.now().date()

    def connect(self, access_token: str) -> None:
        self.api = self.api_factory(access_token)
        self.session_day = datetime.now().date()
        self.product = self.product or self.api.PRODUCT_MIS

    def disconnect(self) -> None:
        self.api, self.session_day = None, None

    # ------------------------------------------------------------------ contracts
    def load_instruments(self, rows: list[dict]) -> None:
        m = {}
        for r in rows:
            ex, tok = str(r.get("exchange") or "").upper(), str(r.get("exchange_token") or "").strip()
            if ex in ("NSE", "BSE") and tok and str(r.get("instrument_type") or "").upper() in ("CE", "PE"):
                m[(ex, tok)] = r
        self._rows_by_token = m

    def row(self, instrument: str) -> dict | None:
        """'NSE_FO|35003' -> Groww instrument row (NSE/BSE F&O options only)."""
        try:
            seg, tok = instrument.split("|", 1)
        except ValueError:
            return None
        ex = {"NSE_FO": "NSE", "BSE_FO": "BSE"}.get(seg)
        return self._rows_by_token.get((ex, tok.strip())) if ex else None

    def supports(self, instrument: str) -> bool:
        return self.row(instrument) is not None

    def matches(self, instrument: str, call: dict) -> tuple[bool, str | None]:
        """Same contract on both sides? Strike, expiry and CE/PE must match the call exactly."""
        r = self.row(instrument)
        if r is None:
            return False, "contract not found on Groww"
        try:
            strike = float(r.get("strike_price"))
        except (TypeError, ValueError):
            strike = None
        kind = str(r.get("instrument_type") or "").upper()
        exp = str(r.get("expiry_date") or "")[:10]
        want_strike, want_kind, want_exp = call.get("strike"), str(call.get("kind") or "").upper(), str(call.get("expiry") or "")[:10]
        if strike is None or want_strike is None or abs(strike - float(want_strike)) > 1e-6:
            return False, f"strike mismatch ({strike} vs {want_strike})"
        if kind != want_kind:
            return False, f"type mismatch ({kind} vs {want_kind})"
        if exp != want_exp:
            return False, f"expiry mismatch ({exp} vs {want_exp})"
        lot = r.get("lot_size")
        if lot and call.get("lot") and int(float(lot)) != int(call["lot"]):
            return False, f"lot size mismatch ({lot} vs {call['lot']})"
        return True, None

    def _ex(self, instrument: str) -> str:
        return self.api.EXCHANGE_NSE if instrument.startswith("NSE_") else self.api.EXCHANGE_BSE

    def _need(self):
        if not self.connected:
            raise BrokerError("Groww session not connected today")

    # ------------------------------------------------------------------ orders
    def place(self, *, ref: str, instrument: str, side: str, qty: int, kind: str,
              price: float | None = None, trigger: float | None = None) -> Order:
        with self._lock:
            self._need()
            r = self.row(instrument)
            if r is None:
                raise BrokerError(f"contract {instrument} not mapped on Groww")
            lot = int(float(r.get("lot_size") or 1))
            if qty <= 0 or qty % lot:
                raise BrokerError(f"quantity {qty} is not a whole number of lots ({lot})")
            freeze = r.get("freeze_quantity")
            if freeze and float(freeze) > 0 and qty >= float(freeze):
                raise BrokerError(f"quantity {qty} reaches the freeze limit {freeze}")
            gref = groww_ref(ref)
            api = self.api
            try:                                           # idempotency: did an earlier attempt already reach Groww?
                prev = _payload(api.get_order_status_by_reference(segment=api.SEGMENT_FNO, order_reference_id=gref))
                if prev.get("groww_order_id"):
                    return self._refresh(prev["groww_order_id"], ref, instrument, side, qty, kind, price, trigger, force=True)
            except Exception:  # noqa: BLE001 - "not found" is the normal case
                pass
            order_type = {"LIMIT": api.ORDER_TYPE_LIMIT, "MARKET": api.ORDER_TYPE_MARKET,
                          "SL_M": api.ORDER_TYPE_STOP_LOSS_MARKET}[kind]
            try:
                resp = _payload(api.place_order(
                    validity=api.VALIDITY_DAY, exchange=self._ex(instrument), order_type=order_type, product=self.product,
                    quantity=int(qty), segment=api.SEGMENT_FNO, trading_symbol=r["trading_symbol"],
                    transaction_type=api.TRANSACTION_TYPE_BUY if side == "BUY" else api.TRANSACTION_TYPE_SELL,
                    order_reference_id=gref, price=float(price) if kind == "LIMIT" and price is not None else 0.0,
                    trigger_price=float(trigger) if kind == "SL_M" and trigger is not None else None, timeout=8))
            except Exception as e:  # noqa: BLE001
                raise BrokerError(f"place failed: {type(e).__name__}: {str(e)[:160]}") from None
            oid = resp.get("groww_order_id")
            if not oid:
                o = Order(id=f"rej:{gref}", ref=ref, instrument=instrument, side=side, qty=qty, kind=kind, price=price,
                          trigger=trigger, status=REJECTED, reason=str(resp.get("remark") or resp)[:160],
                          placed_ms=time.time() * 1000, updated_ms=time.time() * 1000)
                self._orders[o.id] = o
                return o
            return self._refresh(oid, ref, instrument, side, qty, kind, price, trigger, force=True)

    def _refresh(self, oid, ref, instrument, side, qty, kind, price, trigger, force=False) -> Order:
        o = self._orders.get(oid)
        if o is None:
            o = Order(id=oid, ref=ref, instrument=instrument, side=side, qty=int(qty), kind=kind, price=price,
                      trigger=trigger, placed_ms=time.time() * 1000)
            self._orders[oid] = o
        now = time.time() * 1000
        if not force and o.status in (FILLED, CANCELLED, REJECTED):
            return o
        if not force and now - self._fetched.get(oid, 0) < self.cache_ms:
            return o
        try:
            d = _payload(self.api.get_order_detail(segment=self.api.SEGMENT_FNO, groww_order_id=oid, timeout=8))
        except Exception as e:  # noqa: BLE001
            raise BrokerError(f"order status failed: {type(e).__name__}: {str(e)[:160]}") from None
        self._fetched[oid] = now
        st = str(d.get("order_status") or "").upper()
        filled = int(d.get("filled_quantity") or 0)
        if filled:
            o.filled_qty, o.avg_price = filled, float(d.get("average_fill_price") or o.avg_price or 0) or o.avg_price
        if st in _DONE_FILLED and filled >= o.qty:
            o.status = FILLED
        elif st in _DONE_CANCELLED:
            o.status = CANCELLED
        elif st in _DONE_REJECTED:
            o.status, o.reason = REJECTED, str(d.get("remark") or st)[:160]
        else:
            o.status = OPEN                                  # NEW / ACKED / TRIGGER_PENDING / APPROVED / *_REQUESTED
        if d.get("trigger_price") is not None:
            try:
                o.trigger = float(d["trigger_price"])
            except (TypeError, ValueError):
                pass
        o.updated_ms = now
        return o

    def get(self, order_id: str) -> Order:
        with self._lock:
            o = self._orders[order_id]
            if order_id.startswith("rej:"):
                return o
            self._need()
            return self._refresh(order_id, o.ref, o.instrument, o.side, o.qty, o.kind, o.price, o.trigger)

    def cancel(self, order_id: str) -> Order:
        with self._lock:
            self._need()
            o = self._orders[order_id]
            if o.status != OPEN:
                return o
            try:
                self.api.cancel_order(groww_order_id=order_id, segment=self.api.SEGMENT_FNO, timeout=8)
            except Exception as e:  # noqa: BLE001
                raise BrokerError(f"cancel failed: {type(e).__name__}: {str(e)[:160]}") from None
            return self._refresh(order_id, o.ref, o.instrument, o.side, o.qty, o.kind, o.price, o.trigger, force=True)

    def modify(self, order_id: str, *, price: float | None = None, trigger: float | None = None) -> Order:
        with self._lock:
            self._need()
            o = self._orders[order_id]
            if o.status != OPEN:
                return o
            api = self.api
            order_type = {"LIMIT": api.ORDER_TYPE_LIMIT, "MARKET": api.ORDER_TYPE_MARKET,
                          "SL_M": api.ORDER_TYPE_STOP_LOSS_MARKET}[o.kind]
            try:
                api.modify_order(order_type=order_type, segment=api.SEGMENT_FNO, groww_order_id=order_id,
                                 quantity=o.qty - o.filled_qty, price=price, trigger_price=trigger, timeout=8)
            except Exception as e:  # noqa: BLE001
                raise BrokerError(f"modify failed: {type(e).__name__}: {str(e)[:160]}") from None
            if price is not None:
                o.price = price
            if trigger is not None:
                o.trigger = trigger
            return self._refresh(order_id, o.ref, o.instrument, o.side, o.qty, o.kind, o.price, o.trigger, force=True)

    # ------------------------------------------------------------------ account / market
    def position(self, instrument: str) -> int:
        with self._lock:
            self._need()
            r = self.row(instrument)
            if r is None:
                return 0
            try:
                p = _payload(self.api.get_position_for_trading_symbol(trading_symbol=r["trading_symbol"],
                                                                      segment=self.api.SEGMENT_FNO, timeout=8))
            except Exception as e:  # noqa: BLE001
                raise BrokerError(f"positions failed: {type(e).__name__}: {str(e)[:160]}") from None
            rows = p.get("positions") if isinstance(p, dict) else None
            return int(sum(float(x.get("quantity") or 0) for x in (rows or [])))

    def funds(self) -> float | None:
        with self._lock:
            self._need()
            try:
                m = _payload(self.api.get_available_margin_details(timeout=8))
            except Exception as e:  # noqa: BLE001
                raise BrokerError(f"margin failed: {type(e).__name__}: {str(e)[:160]}") from None
            v = ((m or {}).get("fno_margin_details") or {}).get("option_buy_balance_available")
            return float(v) if v is not None else None

    def quote(self, instrument: str) -> dict | None:
        with self._lock:
            self._need()
            r = self.row(instrument)
            if r is None:
                return None
            try:
                qd = _payload(self.api.get_quote(trading_symbol=r["trading_symbol"], exchange=self._ex(instrument),
                                                 segment=self.api.SEGMENT_FNO, timeout=5))
            except Exception as e:  # noqa: BLE001
                raise BrokerError(f"quote failed: {type(e).__name__}: {str(e)[:160]}") from None
            return {"bp1": qd.get("bid_price"), "ap1": qd.get("offer_price"), "ltp": qd.get("last_price")}
