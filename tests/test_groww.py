"""Groww adapter, request signing, order server and the full real path: hub -> RemoteBroker -> order server ->
GrowwBroker -> (fake) Groww. The fake mirrors growwapi's method names, constants and response fields."""
import itertools
import time

import pytest

from tbrain.brokers.groww.broker import GrowwBroker, groww_ref
from tbrain.execution.broker import CANCELLED, FILLED, OPEN, REJECTED, BrokerError
from tbrain.orderserver.signing import Verifier, headers, sign

NIFTY = {"exchange": "NSE", "exchange_token": "35003", "trading_symbol": "NIFTY26OCT22600PE", "instrument_type": "PE",
         "strike_price": "22600", "expiry_date": "2026-10-27", "lot_size": "65", "freeze_quantity": "1800"}
SENSEX = {"exchange": "BSE", "exchange_token": "868062", "trading_symbol": "SENSEX26OCT73600PE", "instrument_type": "PE",
          "strike_price": "73600", "expiry_date": "2026-10-29", "lot_size": "20", "freeze_quantity": "1000"}


class FakeGroww:
    """Same surface as growwapi.GrowwAPI for what we use."""
    EXCHANGE_NSE, EXCHANGE_BSE = "NSE", "BSE"
    SEGMENT_FNO, PRODUCT_MIS, VALIDITY_DAY = "FNO", "MIS", "DAY"
    ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET, ORDER_TYPE_STOP_LOSS_MARKET = "LIMIT", "MARKET", "SL_M"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    ids = itertools.count(1)

    def __init__(self, token="tok"):
        self.token = token
        self.orders, self.by_ref, self.pos = {}, {}, {}
        self.bid, self.ask = 99.6, 100.0
        self.partial = None
        self.fail_place = 0
        self.lose_reply = 0          # Groww accepted the order but the reply never arrived
        self.reject = None
        self.calls = []

    def _fill(self, o):
        if o["order_status"] not in ("OPEN", "ACKED", "NEW"):
            return
        px = None
        if o["transaction_type"] == "BUY" and (o["order_type"] == "MARKET" or self.ask <= o["price"] + 1e-9):
            px = self.ask
        if o["transaction_type"] == "SELL" and o["order_type"] in ("MARKET",):
            px = self.bid
        if px is None:
            return
        want = o["quantity"] - o["filled_quantity"]
        q = int(want * self.partial) if self.partial and o["filled_quantity"] == 0 else want
        q = max(q, 0)
        if q:
            o["average_fill_price"], o["filled_quantity"] = px, o["filled_quantity"] + q
            sign_ = 1 if o["transaction_type"] == "BUY" else -1
            self.pos[o["trading_symbol"]] = self.pos.get(o["trading_symbol"], 0) + sign_ * q
        if o["filled_quantity"] >= o["quantity"]:
            o["order_status"] = "EXECUTED"

    def place_order(self, validity, exchange, order_type, product, quantity, segment, trading_symbol, transaction_type,
                    order_reference_id=None, price=0.0, trigger_price=None, timeout=None):
        self.calls.append(("place", order_type, transaction_type, quantity, price, trigger_price))
        assert segment == "FNO" and product == "MIS" and validity == "DAY" and exchange in ("NSE", "BSE")
        assert 8 <= len(order_reference_id) <= 20 and order_reference_id.isalnum()
        if self.fail_place:
            self.fail_place -= 1
            raise TimeoutError("read timed out")
        if self.reject:
            return {"remark": self.reject, "order_status": "REJECTED"}
        oid = f"GMK{next(self.ids)}"
        o = {"groww_order_id": oid, "order_status": "TRIGGER_PENDING" if order_type == "SL_M" else "OPEN",
             "quantity": quantity, "filled_quantity": 0, "average_fill_price": None, "price": price,
             "trigger_price": trigger_price, "order_type": order_type, "transaction_type": transaction_type,
             "trading_symbol": trading_symbol, "order_reference_id": order_reference_id, "remark": "ok"}
        self.orders[oid] = o; self.by_ref[order_reference_id] = oid
        self._fill(o)
        if self.lose_reply:
            self.lose_reply -= 1
            raise TimeoutError("reply lost after Groww accepted the order")
        return {"groww_order_id": oid, "order_status": o["order_status"], "order_reference_id": order_reference_id}

    def get_order_status_by_reference(self, segment, order_reference_id, timeout=None):
        if order_reference_id not in self.by_ref:
            raise RuntimeError("not found")
        o = self.orders[self.by_ref[order_reference_id]]
        return {"status": "SUCCESS", "payload": {"groww_order_id": o["groww_order_id"], "order_status": o["order_status"]}}

    def get_order_detail(self, segment, groww_order_id, timeout=None):
        o = self.orders[groww_order_id]
        if not self.partial:
            self._fill(o)
        return {"status": "SUCCESS", "payload": dict(o)}

    def cancel_order(self, groww_order_id, segment, timeout=None):
        o = self.orders[groww_order_id]
        if o["order_status"] in ("OPEN", "TRIGGER_PENDING", "ACKED"):
            o["order_status"] = "CANCELLED"
        return {"groww_order_id": groww_order_id, "order_status": o["order_status"]}

    def modify_order(self, order_type, segment, groww_order_id, quantity, price=None, trigger_price=None, timeout=None):
        o = self.orders[groww_order_id]
        if trigger_price is not None:
            o["trigger_price"] = trigger_price
        if price is not None:
            o["price"] = price
        return {"groww_order_id": groww_order_id, "order_status": o["order_status"]}

    def get_position_for_trading_symbol(self, trading_symbol, segment, timeout=None):
        return {"payload": {"positions": [{"trading_symbol": trading_symbol, "quantity": self.pos.get(trading_symbol, 0), "product": "MIS"}]}}

    def get_available_margin_details(self, timeout=None):
        return {"payload": {"fno_margin_details": {"option_buy_balance_available": 50000.0}}}

    def get_quote(self, trading_symbol, exchange, segment, timeout=None):
        return {"status": "SUCCESS", "payload": {"bid_price": self.bid, "offer_price": self.ask, "last_price": (self.bid + self.ask) / 2}}


def gb(fake=None):
    fake = fake or FakeGroww()
    b = GrowwBroker(lambda tok: fake, instruments=[NIFTY, SENSEX], log=lambda *a: None, cache_ms=0)
    b.connect("tok")
    return b, fake


def test_mapping_by_exchange_token_and_strict_contract_check():
    b, _ = gb()
    assert b.supports("NSE_FO|35003") and b.supports("BSE_FO|868062")
    assert not b.supports("NSE_FO|868062") and not b.supports("BSE_FO|35003") and not b.supports("junk")
    call = {"strike": 22600.0, "kind": "PE", "expiry": "2026-10-27", "lot": 65}
    assert b.matches("NSE_FO|35003", call) == (True, None)
    for bad, word in ((dict(call, strike=22650.0), "strike"), (dict(call, kind="CE"), "type"),
                      (dict(call, expiry="2026-11-03"), "expiry"), (dict(call, lot=75), "lot")):
        ok, why = b.matches("NSE_FO|35003", bad)
        assert not ok and word in why


def test_place_fill_cancel_modify_and_status_mapping():
    b, f = gb()
    o = b.place(ref="real:c1:E", instrument="NSE_FO|35003", side="BUY", qty=65, kind="LIMIT", price=100.0)
    assert o.status == FILLED and o.filled_qty == 65 and o.avg_price == 100.0
    s = b.place(ref="real:c1:S0", instrument="NSE_FO|35003", side="SELL", qty=65, kind="SL_M", trigger=80.0)
    assert s.status == OPEN and f.orders[s.id]["order_status"] == "TRIGGER_PENDING"
    assert b.modify(s.id, trigger=90.0).trigger == 90.0
    assert b.cancel(s.id).status == CANCELLED
    assert b.position("NSE_FO|35003") == 65 and b.funds() == 50000.0
    assert b.quote("NSE_FO|35003") == {"bp1": 99.6, "ap1": 100.0, "ltp": 99.8}


def test_lost_reply_is_recovered_by_reference_never_duplicated():
    b, f = gb()
    f.lose_reply = 1
    with pytest.raises(BrokerError):
        b.place(ref="real:c2:E", instrument="NSE_FO|35003", side="BUY", qty=65, kind="LIMIT", price=100.0)
    o = b.place(ref="real:c2:E", instrument="NSE_FO|35003", side="BUY", qty=65, kind="LIMIT", price=100.0)  # retry
    assert len([c for c in f.calls if c[0] == "place"]) == 1 and o.filled_qty == 65   # only ONE real order
    assert f.by_ref[groww_ref("real:c2:E")] == o.id


def test_rejections_lots_freeze_and_disconnected_refusals():
    b, f = gb()
    f.reject = "insufficient funds"
    o = b.place(ref="real:c3:E", instrument="NSE_FO|35003", side="BUY", qty=65, kind="LIMIT", price=100.0)
    assert o.status == REJECTED and "insufficient" in o.reason
    f.reject = None
    with pytest.raises(BrokerError, match="whole number of lots"):
        b.place(ref="x1", instrument="NSE_FO|35003", side="BUY", qty=70, kind="LIMIT", price=100.0)
    with pytest.raises(BrokerError, match="freeze"):
        b.place(ref="x2", instrument="NSE_FO|35003", side="BUY", qty=1820, kind="LIMIT", price=100.0)
    with pytest.raises(BrokerError, match="not mapped"):
        b.place(ref="x3", instrument="NSE_FO|1", side="BUY", qty=65, kind="LIMIT", price=100.0)
    b.disconnect()
    assert not b.connected
    with pytest.raises(BrokerError, match="not connected"):
        b.funds()


def test_partial_fill_is_open_until_complete():
    b, f = gb(); f.partial = 0.5; f.ask = 100.0
    o = b.place(ref="real:c4:E", instrument="BSE_FO|868062", side="BUY", qty=40, kind="LIMIT", price=100.0)
    assert o.status == OPEN and o.filled_qty == 20
    f.partial = None
    assert b.get(o.id).status == FILLED and b.get(o.id).filled_qty == 40


def test_signing_rejects_tampering_staleness_and_replay():
    v = Verifier("s" * 40)
    h = headers("s" * 40, "POST", "/broker/funds", b"{}")
    args = ("POST", "/broker/funds", b"{}", h["X-TB-Ts"], h["X-TB-Nonce"])
    assert v.check(*args, h["X-TB-Sig"]) is None
    assert v.check(*args, h["X-TB-Sig"]) == "replayed request"
    h2 = headers("s" * 40, "POST", "/broker/funds", b"{}")
    assert v.check("POST", "/broker/funds", b'{"x":1}', h2["X-TB-Ts"], h2["X-TB-Nonce"], h2["X-TB-Sig"]) == "bad signature"
    old = str(int(time.time() * 1000) - 60_000)
    assert v.check("POST", "/p", b"", old, "n1", sign("s" * 40, "POST", "/p", b"", old, "n1")) == "stale request"
    assert v.check("POST", "/p", b"", None, None, None) == "missing signature"
    with pytest.raises(RuntimeError):
        Verifier("short")


# ---------------------------------------------------------------- full path through the order server
def _stack():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tbrain.brokers.remote import RemoteBroker
    from tbrain.orderserver.app import create_order_app
    fake = FakeGroww()
    g = GrowwBroker(lambda tok: fake, instruments=[NIFTY, SENSEX], log=lambda *a: None, cache_ms=0)
    app = create_order_app(g, "k" * 40, login=lambda: "tok", log=lambda *a: None)
    client = TestClient(app)
    rb = RemoteBroker("http://testserver", "k" * 40, client=client)
    return rb, g, fake, client


def test_order_server_refuses_unsigned_and_wrongly_signed_requests():
    rb, g, fake, client = _stack()
    assert client.get("/healthz").status_code == 200
    assert client.post("/broker/funds", json={}).status_code == 401
    from tbrain.brokers.remote import RemoteBroker
    bad = RemoteBroker(
        "http://testserver", "x" * 40, client=client)
    with pytest.raises(BrokerError, match="refused"):
        bad.funds()


def test_connect_trade_and_disconnect_through_the_order_server():
    rb, g, fake, client = _stack()
    assert rb.connected is False
    assert rb.connect()["connected"] is True and rb.connected is True
    call = {"strike": 22600.0, "kind": "PE", "expiry": "2026-10-27", "lot": 65}
    assert rb.supports("NSE_FO|35003") and rb.matches("NSE_FO|35003", call) == (True, None)
    o = rb.place(ref="real:z:E", instrument="NSE_FO|35003", side="BUY", qty=65, kind="LIMIT", price=100.0)
    assert o.status == FILLED and rb.position("NSE_FO|35003") == 65 and rb.funds() == 50000.0
    with pytest.raises(BrokerError):                                 # broker errors come back as retryable errors
        rb.place(ref="real:z:X", instrument="NSE_FO|35003", side="BUY", qty=70, kind="LIMIT", price=100.0)
    rb.disconnect()
    assert rb.connected is False and g.connected is False


def test_hub_runs_real_orders_on_its_own_thread_via_the_order_server():
    from datetime import datetime
    from tbrain.execution.hub import ExecutionHub
    from tbrain.runtime_settings import DictSettings
    from tbrain.timeutil import IST
    from tests.test_execution import S
    rb, g, fake, client = _stack()
    rb.connect()
    store = DictSettings({}); store.set_real(on=True)
    t0 = datetime(2026, 10, 26, 10, 0, tzinfo=IST).timestamp() * 1000
    clock = [t0]
    h = ExecutionHub(S(), store, log=lambda *a: None, real_broker=rb, now_fn=lambda: clock[0])
    try:
        ins = "NSE_FO|35003"
        quotes = {ins: {"bp1": 99.6, "ap1": 100.0, "ltp": 99.8}}
        h.on_step(clock[0], {"NIFTY": {"quotes": quotes}})
        call = {"id": "c1", "und": "NIFTY", "symbol": "NIFTY 22600 PE", "instrument_key": ins, "lot": 65, "entry": 100.0,
                "entry_zone": [99.6, 100.4], "p_sl": 90.0, "dir": -1, "strike": 22600.0, "kind": "PE", "expiry": "2026-10-27"}
        h.on_event("issue", call)
        deadline = time.time() + 8
        while time.time() < deadline and (h.snapshot()["real"]["trades"].get("c1", {}).get("state") != "open"):
            h.on_step(clock[0], {"NIFTY": {"quotes": quotes}}); time.sleep(0.1)
        assert h.snapshot()["real"]["trades"]["c1"]["state"] == "open"
        assert fake.pos["NIFTY26OCT22600PE"] == 65
        stops = [o for o in fake.orders.values() if o["order_type"] == "SL_M"]
        assert len(stops) == 1 and stops[0]["order_status"] == "TRIGGER_PENDING"
        fake.bid = 104.0
        h.on_event("close", dict(call, outcome="T2"))
        deadline = time.time() + 8
        while time.time() < deadline and h.snapshot()["real"]["trades"]["c1"]["state"] != "closed":
            time.sleep(0.1)
        r = h.snapshot()["real"]["trades"]["c1"]
        assert r["state"] == "closed" and fake.pos["NIFTY26OCT22600PE"] == 0 and stops[0]["order_status"] == "CANCELLED"
        assert r["pnl"] == pytest.approx((104.0 - 100.0) * 65 - 40)
    finally:
        h.stop()
