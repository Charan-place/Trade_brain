"""Execution: paper book (always) + real book (owner only) - normal flow and edge cases.

The fake 'real' broker is a PaperBroker with knobs for the things real brokers do: partial fills, rejections,
network errors, slow cancels, low funds, missing contracts, disconnects.
"""
import random
from datetime import datetime
from types import SimpleNamespace

import pytest

from tbrain.execution.book import BookConfig, TradeBook
from tbrain.execution.broker import CANCELLED, FILLED, OPEN, BrokerError, PaperBroker, tick_down, tick_up
from tbrain.execution.hub import CONFIRM_PHRASE, ExecutionHub
from tbrain.runtime_settings import DictSettings
from tbrain.timeutil import IST

INS = "NSE_FO|123"


def ms(h, m, s=0):
    return datetime(2026, 10, 26, h, m, s, tzinfo=IST).timestamp() * 1000


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeReal(PaperBroker):
    name = "fake-groww"

    def __init__(self, now_fn, cash=1e6):
        super().__init__(now_fn)
        self.cash, self.fail_place, self.cancel_delay, self.partial = cash, 0, 0, None
        self.mapped = None
        self._cancel_calls = {}
        self.disconnected = 0

    def funds(self):
        return self.cash

    def supports(self, instrument):
        return self.mapped is None or instrument in self.mapped

    def disconnect(self):
        self.connected = False
        self.disconnected += 1

    def place(self, **kw):
        if self.fail_place > 0 and kw["ref"] not in self.by_ref:
            self.fail_place -= 1
            raise BrokerError("network timeout")
        o = super().place(**kw)
        return o

    def _try_fill(self, o, now_ms):
        if self.partial and o.side == "BUY" and o.filled_qty == 0 and o.status == OPEN:
            q = self.quotes.get(o.instrument) or {}
            if q.get("ap1") and q["ap1"] <= (o.price or 0) + 1e-9:
                part = max(1, int(o.qty * self.partial))
                o.fill(part, q["ap1"], now_ms)
                self.pos[o.instrument] = self.position(o.instrument) + part
                return
        super()._try_fill(o, now_ms)

    def cancel(self, order_id):
        n = self._cancel_calls.get(order_id, 0) + 1
        self._cancel_calls[order_id] = n
        if n <= self.cancel_delay:
            return self.orders[order_id]          # cancel requested but not confirmed yet
        return super().cancel(order_id)


def call(cid="c1", entry=100.0, zone=(99.6, 101.0), p_sl=90.0, lot=65, ins=INS):
    return {"id": cid, "und": "NIFTY", "symbol": f"NIFTY X {cid}", "instrument_key": ins, "lot": lot, "entry": entry,
            "entry_zone": list(zone), "p_sl": p_sl, "dir": 1}


def q(bid, ask, ltp=None):
    return {"bp1": bid, "ap1": ask, "ltp": ltp if ltp is not None else (bid + ask) / 2}


def S(**kw):
    base = dict(real_lots=1, real_max_open=2, real_max_orders=20, cost_per_lot_inr=40.0, real_safety_stop_r=2.0,
                real_entry_timeout_s=20.0, real_confirm_each=False, real_daily_loss_cap=3000.0, real_loss_cap_max=10000.0,
                real_max_price_gap_pct=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


def hub_with(real=True, settings=None, t=ms(10, 0), **kw):
    clock = Clock(t)
    store = DictSettings({})
    rb = FakeReal(clock) if real else None
    h = ExecutionHub(settings or S(**kw), store, log=lambda *a: None, real_broker=rb, now_fn=clock)
    return h, store, rb, clock


def feed(h, clock, quotes, rb=None, adv_s=1):
    clock.t += adv_s * 1000
    if rb is not None:
        rb.on_quotes(quotes, clock.t)
    h.on_step(clock.t, {"NIFTY": {"quotes": quotes}})


def turn_on(h, store):
    store.set_real(on=True)
    h._poll(force=True)


# ---------------------------------------------------------------- paper book: normal flow
def test_paper_entry_stop_and_signal_exit():
    clock = Clock(ms(10, 0)); b = PaperBroker(clock)
    bk = TradeBook("paper", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)})
    t = bk.on_issue(call())
    assert t["state"] == "open" and t["entry_price"] == 100.4 and t["filled_qty"] == 65
    s = b.get(t["stop_order"])
    assert s.kind == "SL_M" and s.qty == 65 and s.trigger == tick_down(100.4 - 2 * (100.4 - 90.0))   # -2R safety stop
    b.on_quotes({INS: q(110.0, 110.4)})
    bk.on_close({"id": "c1", "outcome": "T2"})
    assert t["state"] == "closed" and t["exit_price"] == 110.0 and b.position(INS) == 0
    assert b.get(t["stop_order"]).status == CANCELLED                               # stop cancelled BEFORE selling
    assert t["pnl"] == pytest.approx((110.0 - 100.4) * 65 - 40)


def test_entry_never_chases_above_zone_and_is_missed_after_timeout():
    clock = Clock(ms(10, 0)); b = PaperBroker(clock)
    bk = TradeBook("paper", b, BookConfig(entry_timeout_s=20), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(102.0, 102.5)})                                             # ask above zone top 101
    t = bk.on_issue(call())
    assert t["state"] == "entry" and t["entry_limit"] == tick_up(101.0)
    clock.t += 21_000; b.on_quotes({INS: q(103.0, 103.5)}); bk.tick()
    assert t["state"] == "missed" and b.position(INS) == 0 and b.get(t["entry_order"]).status == CANCELLED


def test_partial_fill_then_timeout_trades_only_what_filled():
    clock = Clock(ms(10, 0)); b = FakeReal(clock); b.partial = 0.4
    bk = TradeBook("real", b, BookConfig(lots=2), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)})
    t = bk.on_issue(call())                       # 130 qty wanted, 52 filled
    assert t["filled_qty"] == 52 and t["state"] == "entry"
    clock.t += 21_000; b.on_quotes({INS: q(100.5, 101.6)}); bk.tick()  # ask above limit: rest never fills
    assert t["state"] == "open" and b.get(t["stop_order"]).qty == 52
    bk.exit(t["id"], "manual")
    assert t["state"] == "closed" and t["exit_qty"] == 52 and b.position(INS) == 0


def test_partial_fill_completing_later_resizes_the_stop():
    clock = Clock(ms(10, 0)); b = FakeReal(clock); b.partial = 0.5
    bk = TradeBook("real", b, BookConfig(lots=2), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)})
    t = bk.on_issue(call())
    assert t["filled_qty"] == 65
    b.partial = None; b.on_quotes({INS: q(100.0, 100.4)}); bk.tick()
    assert t["filled_qty"] == 130 and t["state"] == "open"
    live = [o for o in b.orders.values() if o.kind == "SL_M" and o.status == OPEN]
    assert len(live) == 1 and live[0].qty == 130


def test_safety_stop_at_broker_then_late_signal_close_does_not_sell_again():
    clock = Clock(ms(10, 0)); b = PaperBroker(clock)
    bk = TradeBook("paper", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)}); t = bk.on_issue(call())
    b.on_quotes({INS: q(70.0, 70.5)}); bk.tick()                                   # below the -2R trigger
    assert t["state"] == "closed" and "safety stop" in t["exit_reason"] and b.position(INS) == 0
    n = len(b.orders)
    bk.on_close({"id": "c1", "outcome": "SL"}); bk.tick()
    assert len(b.orders) == n and b.position(INS) == 0                              # never short


def test_slow_stop_cancel_blocks_the_exit_sell_until_confirmed():
    clock = Clock(ms(10, 0)); b = FakeReal(clock); b.cancel_delay = 2
    bk = TradeBook("real", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)}); t = bk.on_issue(call())
    bk.exit(t["id"], "manual")
    assert t["state"] == "exiting" and not t["exit_orders"]                         # stop still live: no sell yet
    bk.tick(); assert not t["exit_orders"]
    bk.tick()
    assert t["state"] == "closed" and len(t["exit_orders"]) == 1 and b.position(INS) == 0


def test_stop_fills_while_exit_is_being_arranged_no_double_sell():
    clock = Clock(ms(10, 0)); b = FakeReal(clock); b.cancel_delay = 5
    bk = TradeBook("real", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)}); t = bk.on_issue(call())
    bk.exit(t["id"], "manual")
    b.on_quotes({INS: q(60.0, 60.5)})                                               # stop triggers meanwhile
    for _ in range(8):
        bk.tick()
    assert t["state"] == "closed" and not t["exit_orders"] and b.position(INS) == 0


def test_network_errors_are_retried_without_duplicate_orders():
    clock = Clock(ms(10, 0)); b = FakeReal(clock)
    bk = TradeBook("real", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)}); t = bk.on_issue(call())
    b.fail_place = 2
    bk.exit(t["id"], "manual"); bk.tick(); bk.tick(); bk.tick()
    sells = [o for o in b.orders.values() if o.side == "SELL" and o.kind == "MARKET"]
    assert t["state"] == "closed" and len(sells) == 1 and b.position(INS) == 0 and bk.errors >= 2


def test_same_call_twice_creates_one_trade_and_one_order():
    clock = Clock(ms(10, 0)); b = PaperBroker(clock)
    bk = TradeBook("paper", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)})
    bk.on_issue(call()); bk.on_issue(call())
    assert len(bk.trades) == 1 and sum(o.side == "BUY" for o in b.orders.values()) == 1
    o1 = b.place(ref="paper:c1:E", instrument=INS, side="BUY", qty=65, kind="LIMIT", price=101)
    assert o1.id == bk.trades["paper:c1"]["entry_order"]                            # same ref -> same order


def test_t1_raises_stop_to_minus_one_r():
    clock = Clock(ms(10, 0)); b = PaperBroker(clock)
    bk = TradeBook("paper", b, BookConfig(), log=lambda *a: None, now_fn=clock)
    b.on_quotes({INS: q(100.0, 100.4)}); t = bk.on_issue(call())
    before = b.get(t["stop_order"]).trigger
    bk.on_t1({"id": "c1"})
    after = b.get(t["stop_order"]).trigger
    assert after > before and after == tick_down(90.0)


def test_limits_max_open_and_max_orders():
    clock = Clock(ms(10, 0)); b = PaperBroker(clock)
    bk = TradeBook("paper", b, BookConfig(max_open=2, max_orders=3), log=lambda *a: None, now_fn=clock)
    qs = {f"I{i}": q(100.0, 100.4) for i in range(5)}; b.on_quotes(qs)
    a, c, d = (bk.on_issue(call(f"c{i}", ins=f"I{i}")) for i in range(3))
    assert a["state"] == c["state"] == "open" and d["state"] == "skipped" and "max 2 open" in d["reason"]
    bk.exit(a["id"], "x")                          # uses an order slot (sell)
    e = bk.on_issue(call("c9", ins="I4"))
    assert e["state"] == "skipped" and "max 3 orders" in e["reason"]


# ---------------------------------------------------------------- hub: real gates and controls
def test_real_is_skipped_when_off_and_paper_still_trades():
    h, store, rb, clock = hub_with()
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    snap = h.snapshot()
    assert snap["paper"]["trades"]["c1"]["state"] == "open"
    assert snap["real"]["trades"]["c1"]["state"] == "skipped" and snap["real"]["trades"]["c1"]["reason"] == "real money is off"


def test_no_broker_at_all_is_reported_and_switch_cannot_stay_on():
    h, store, rb, clock = hub_with(real=False)
    store.set_real(on=True); h._poll(force=True)
    assert store.get_real()["on"] is False and h.snapshot()["real_status"]["why_off"] == "broker not connected"
    feed(h, clock, {INS: q(100.0, 100.4)})
    h.on_event("issue", call())
    assert h.snapshot()["real"]["trades"]["c1"]["reason"] == "broker not connected"


@pytest.mark.parametrize("setup,expect", [
    (lambda rb: setattr(rb, "connected", False), "broker not connected"),
    (lambda rb: setattr(rb, "cash", 1000.0), "needs ₹"),
    (lambda rb: setattr(rb, "mapped", {"other"}), "contract not mapped"),
])
def test_real_gate_reasons(setup, expect):
    h, store, rb, clock = hub_with()
    turn_on(h, store); setup(rb)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    r = h.snapshot()["real"]["trades"]["c1"]
    assert r["state"] == "skipped" and expect in r["reason"]
    assert h.snapshot()["paper"]["trades"]["c1"]["state"] == "open"                 # paper unaffected


def test_real_skips_when_broker_price_disagrees_with_ours():
    h, store, rb, clock = hub_with()
    turn_on(h, store)
    clock.t += 1000
    rb.on_quotes({INS: q(103.0, 103.4)}, clock.t)                                   # broker 3% higher
    h.on_step(clock.t, {"NIFTY": {"quotes": {INS: q(100.0, 100.4)}}})
    h.on_event("issue", call())
    assert "apart" in h.snapshot()["real"]["trades"]["c1"]["reason"]


def test_real_full_trade_when_everything_is_ok():
    h, store, rb, clock = hub_with()
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    assert h.snapshot()["real"]["trades"]["c1"]["state"] == "open" and rb.position(INS) == 65
    feed(h, clock, {INS: q(108.0, 108.4)}, rb)
    h.on_event("close", {"id": "c1", "outcome": "T2"}); feed(h, clock, {INS: q(108.0, 108.4)}, rb)
    r = h.snapshot()["real"]["trades"]["c1"]
    assert r["state"] == "closed" and rb.position(INS) == 0 and r["pnl"] == pytest.approx((108.0 - 100.4) * 65 - 40)


def test_confirm_each_needs_approval_and_expires():
    h, store, rb, clock = hub_with(real_confirm_each=True)
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call("c1"))
    assert h.snapshot()["real"]["trades"]["c1"]["state"] == "approval" and rb.position(INS) == 0
    store.push_command("approve", "c1"); h._poll(force=True)
    assert h.snapshot()["real"]["trades"]["c1"]["state"] == "open"
    feed(h, clock, {"I2": q(50.0, 50.2), INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call("c2", ins="I2", entry=50, zone=(49.9, 50.5), p_sl=45))
    feed(h, clock, {"I2": q(50.0, 50.2)}, rb, adv_s=46)
    assert h.snapshot()["real"]["trades"]["c2"]["state"] == "skipped" and rb.position("I2") == 0


def test_manual_exit_closes_real_only_paper_follows_its_own_rules():
    h, store, rb, clock = hub_with()
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    store.push_command("exit", "c1"); feed(h, clock, {INS: q(101.0, 101.4)}, rb, adv_s=3)
    snap = h.snapshot()
    assert snap["real"]["trades"]["c1"]["state"] == "closed" and snap["real"]["trades"]["c1"]["exit_reason"] == "manual exit"
    assert snap["paper"]["trades"]["c1"]["state"] == "open"
    h.on_event("close", {"id": "c1", "outcome": "T2"}); feed(h, clock, {INS: q(109.0, 109.4)}, rb)
    assert h.snapshot()["paper"]["trades"]["c1"]["state"] == "closed"
    assert rb.position(INS) == 0


def test_exit_all_and_switch_off_close_everything_then_disconnect():
    h, store, rb, clock = hub_with(real_max_open=3)
    turn_on(h, store)
    qs = {INS: q(100.0, 100.4), "I2": q(50.0, 50.2)}
    feed(h, clock, qs, rb)
    h.on_event("issue", call("c1")); h.on_event("issue", call("c2", ins="I2", entry=50, zone=(49.9, 50.5), p_sl=45))
    assert rb.position(INS) == 65 and rb.position("I2") == 65
    store.set_real(on=False, why_off="switched off"); feed(h, clock, qs, rb, adv_s=3)
    assert rb.position(INS) == 0 and rb.position("I2") == 0 and rb.disconnected == 1 and rb.connected is False
    assert all(t["state"] == "closed" for t in h.snapshot()["real"]["trades"].values())
    assert all(t["state"] == "open" for t in h.snapshot()["paper"]["trades"].values())


def test_loss_cap_breach_turns_real_off_and_flattens_but_paper_continues():
    h, store, rb, clock = hub_with(real_daily_loss_cap=1000.0)
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    feed(h, clock, {INS: q(84.0, 84.4)}, rb, adv_s=3)           # -16.4 x 65 = -1066 unrealized > cap
    feed(h, clock, {INS: q(84.0, 84.4)}, rb, adv_s=3)
    st = h.snapshot()
    assert st["real_status"]["on"] is False and "loss cap" in st["real_status"]["why_off"]
    assert st["real"]["trades"]["c1"]["state"] == "closed" and rb.position(INS) == 0
    assert st["paper"]["trades"]["c1"]["state"] == "open"


def test_loss_cap_from_dashboard_is_bounded_by_env_max():
    h, store, rb, clock = hub_with(real_loss_cap_max=5000.0)
    store.set_real(loss_cap=50000)
    assert h.loss_cap() == 5000.0
    store.set_real(loss_cap="junk")
    assert h.loss_cap() == 3000.0


def test_square_off_at_1520_closes_both_books():
    h, store, rb, clock = hub_with(t=ms(15, 10))
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    clock.t = ms(15, 20, 1); rb.on_quotes({INS: q(101.0, 101.4)}, clock.t)
    h.on_step(clock.t, {"NIFTY": {"quotes": {INS: q(101.0, 101.4)}}})
    h.on_step(clock.t + 500, {"NIFTY": {"quotes": {INS: q(101.0, 101.4)}}})
    s = h.snapshot()
    assert s["paper"]["trades"]["c1"]["state"] == "closed" and s["real"]["trades"]["c1"]["state"] == "closed"
    assert rb.position(INS) == 0


def test_no_new_entries_in_the_last_minutes_before_square_off():
    h, store, rb, clock = hub_with(t=ms(15, 16))
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    s = h.snapshot()
    assert s["paper"]["trades"]["c1"]["state"] == "skipped" and s["real"]["trades"]["c1"]["state"] == "skipped"


def test_position_mismatch_with_broker_turns_real_off():
    h, store, rb, clock = hub_with()
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    rb.pos[INS] = 0                                          # someone sold in the Groww app
    for _ in range(3):
        feed(h, clock, {INS: q(100.0, 100.4)}, rb, adv_s=6)
    assert h.snapshot()["real_status"]["on"] is False and "mismatch" in h.snapshot()["real_status"]["why_off"]
    assert rb.position(INS) >= 0                             # never sold into a short


def test_shadow_calls_are_ignored():
    h, store, rb, clock = hub_with()
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", dict(call(), shadow=True))
    assert h.snapshot()["paper"]["trades"] == {}


def test_stale_commands_from_an_earlier_run_are_ignored():
    h, store, rb, clock = hub_with()
    turn_on(h, store)
    feed(h, clock, {INS: q(100.0, 100.4)}, rb)
    h.on_event("issue", call())
    c = store.push_command("exit_all")
    import time as _t
    doc = store._load(); doc["_cmds"][-1]["ms"] = _t.time() * 1000 - 700_000; store._save(doc)
    h._poll(force=True)
    assert h.snapshot()["real"]["trades"]["c1"]["state"] == "open"
    assert CONFIRM_PHRASE == "yes use my real money"


# ---------------------------------------------------------------- invariants under random abuse
@pytest.mark.parametrize("seed", range(200))
def test_invariants_random_sequences(seed):
    rnd = random.Random(seed)
    h, store, rb, clock = hub_with(real_max_open=3)
    rb.partial = rnd.choice([None, 0.3, 0.6]); rb.cancel_delay = rnd.choice([0, 1, 3])
    turn_on(h, store)
    inst = ["A", "B", "C", "D"]
    px = {i: 100.0 for i in inst}
    cid = 0
    for step in range(220):
        for i in inst:
            px[i] = max(1.0, px[i] * (1 + rnd.uniform(-0.02, 0.02)))
        qs = {i: q(round(px[i], 2), round(px[i] + 0.4, 2)) for i in inst}
        if rnd.random() < 0.3:
            rb.fail_place = rnd.choice([0, 1])
        feed(h, clock, qs, rb, adv_s=rnd.choice([1, 2, 5]))
        r = rnd.random()
        if r < 0.08:
            cid += 1; i = rnd.choice(inst); p = px[i]
            h.on_event("issue", call(f"c{cid}", ins=i, entry=p + 0.4, zone=(p, p + 0.8), p_sl=p * 0.92))
        elif r < 0.13 and cid:
            h.on_event("close", {"id": f"c{rnd.randint(1, cid)}", "outcome": "TIME"})
        elif r < 0.16 and cid:
            store.push_command("exit", f"c{rnd.randint(1, cid)}")
        elif r < 0.17:
            store.push_command("exit_all")
        elif r < 0.18:
            h.on_event("t1", {"id": f"c{rnd.randint(1, max(cid, 1))}"})
        for i in inst:
            assert rb.position(i) >= 0 and h.paper.broker.position(i) >= 0          # never short
        for book, br in ((h.real, rb), (h.paper, h.paper.broker)):
            for i in inst:
                held = sum(book.held(t) for t in book.trades.values() if t["instrument"] == i and t["state"] != "closed")
                assert held >= br.position(i) - 0 or held == br.position(i)
            for t in book.trades.values():
                assert t["exit_qty"] <= t["filled_qty"]
                if t["state"] == "open" and book.held(t) > 0:
                    if t["stop_order"] is None:                                     # only while a failed place is retried
                        assert t.get("last_error")
                    else:
                        s = br.get(t["stop_order"])
                        assert s.status == OPEN or s.filled_qty > 0                  # a stop behind every position
    rb.fail_place = 0; rb.cancel_delay = 0
    clock.t = ms(15, 21)
    for _ in range(6):
        qs = {i: q(round(px[i], 2), round(px[i] + 0.4, 2)) for i in inst}
        rb.on_quotes(qs, clock.t); h.on_step(clock.t, {"X": {"quotes": qs}}); clock.t += 1000
    for i in inst:
        assert rb.position(i) == 0 and h.paper.broker.position(i) == 0               # flat after 15:20


def test_paper_results_identical_with_or_without_real_interference():
    def run(interfere):
        h, store, rb, clock = hub_with()
        turn_on(h, store)
        rnd = random.Random(7); p = 100.0
        for k in range(120):
            p *= 1 + rnd.uniform(-0.015, 0.015)
            feed(h, clock, {INS: q(round(p, 2), round(p + 0.4, 2))}, rb)
            if k == 5:
                h.on_event("issue", call("c1", entry=p + 0.4, zone=(p, p + 0.8), p_sl=p * 0.9))
            if interfere and k == 20:
                store.push_command("exit", "c1")
            if interfere and k == 40:
                store.set_real(on=False)
            if k == 90:
                h.on_event("close", {"id": "c1", "outcome": "TIME"})
        return h.snapshot()["paper"]["trades"]["c1"]
    a, b = run(False), run(True)
    assert (a["state"], a["entry_price"], a["exit_price"], a["pnl"]) == (b["state"], b["entry_price"], b["exit_price"], b["pnl"])


# ---------------------------------------------------------------- owner-only controls (web + local server)
def _web(owner=True):
    pytest.importorskip("fastapi")
    from tests import test_web as W
    c, s, db, tokens, oauth = W._client(allowed=("*",) if not owner else (W.OWNER,),
                                        userinfo=None if owner else {"email": "viewer@gmail.com", "email_verified": True})
    c.get("/login")
    return c, s


def test_viewers_never_see_paper_or_real_and_cannot_touch_real_controls():
    from tests import test_web as W
    c, s = _web(owner=False)
    st = c.get("/api/state").json()
    assert "execution" not in st
    for path, body in (("/api/real", {"on": True, "confirm": CONFIRM_PHRASE}), ("/api/real/exit", {"all": True}),
                       ("/api/real/approve", {"call_id": "x"})):
        assert c.post(path, json=body).status_code == 403
    assert c.get("/api/real").status_code == 403
    assert c.get("/api/history?book=real").status_code == 403 and c.get("/api/history?book=paper").status_code == 403
    assert c.get("/api/history").status_code == 200


def test_owner_sees_execution_and_viewer_gets_it_stripped():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from cryptography.fernet import Fernet
    from tbrain.runtime_settings import DictSettings
    from tbrain.web.app import create_app
    from tests import test_web as W

    class St:
        def latest(self):
            return {"mode": "live", "execution": {"real": {"trades": {"c1": {"state": "open"}}}}}
    for owner, expect in ((True, True), (False, False)):
        s = W._settings(allowed=("*",)); s.encryption_key = Fernet.generate_key().decode()
        oauth = W.FakeOAuth()
        if not owner:
            oauth.google.userinfo = {"email": "viewer@gmail.com", "email_verified": True}
        app = create_app(s, state_source=St(), db=None, tokens=None, oauth=oauth, settings_store=DictSettings({}))
        c = TestClient(app); c.get("/login")
        assert ("execution" in c.get("/api/state").json()) is expect


def test_owner_real_switch_needs_the_exact_phrase_and_cap_is_bounded():
    c, s = _web(owner=True)
    assert c.post("/api/real", json={"on": True}).status_code == 400
    assert c.post("/api/real", json={"on": True, "confirm": "yes use my money"}).status_code == 400
    assert c.post("/api/real", data="on=true", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    r = c.post("/api/real", json={"on": True, "confirm": "  Yes Use My Real Money "})
    assert r.status_code == 200 and r.json()["real"]["on"] is True
    assert c.post("/api/real", json={"loss_cap": 999999}).status_code == 400
    assert c.post("/api/real", json={"loss_cap": "abc"}).status_code == 400
    assert c.post("/api/real", json={"loss_cap": 2500}).json()["real"]["loss_cap"] == 2500
    r = c.post("/api/real", json={"on": False})
    assert r.json()["real"]["on"] is False and r.json()["real"]["why_off"] == "switched off by you"
    assert c.post("/api/real/exit", json={"call_id": "c1"}).json()["queued"]["kind"] == "exit"
    assert c.post("/api/real/exit", json={"all": True}).json()["queued"]["kind"] == "exit_all"
    assert c.post("/api/real/exit", json={}).status_code == 400
    assert c.post("/api/real/approve", json={"call_id": "c1"}).json()["queued"]["kind"] == "approve"


def test_local_server_real_controls_same_rules(tmp_path):
    import json as _j
    import urllib.error
    import urllib.request
    from tbrain.runtime_settings import FileSettings
    from tbrain.ui.server import Broadcaster, UIServer
    st = FileSettings(tmp_path / "s.json")
    srv = UIServer(Broadcaster(), 8793, st, max_cap=5000).start()
    try:
        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:8793{path}", data=_j.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, _j.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, _j.loads(e.read() or b"{}")
        assert post("/api/real", {"on": True, "confirm": "nope"})[0] == 400
        assert post("/api/real", {"on": True, "confirm": CONFIRM_PHRASE}) == (200, {"real": st.get_real()})
        assert post("/api/real", {"loss_cap": 6000})[0] == 400
        assert post("/api/real/exit", {"call_id": "c9"})[1]["queued"]["call_id"] == "c9"
        assert st.get_real()["on"] is True and st.commands()[-1]["kind"] == "exit"
    finally:
        srv.stop()


def test_engine_drives_the_paper_book_end_to_end():
    from tbrain.signals.engine import EngineConfig, SignalEngine
    from tests import test_signals as T
    clock = Clock(T.NOW_MS)
    h = ExecutionHub(S(), DictSettings({}), log=lambda *a: None, real_broker=None, now_fn=clock)
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    eng.on_event, eng.on_step = h.on_event, h.on_step
    out = eng.step(T.NOW_MS, {"NIFTY": T._ctx()}, T._tracker())
    (c,) = [x for x in out["calls"] if x["und"] == "NIFTY"]
    p = h.snapshot()["paper"]["trades"][c["id"]]
    assert p["state"] == "open" and p["qty"] == c["lot"]
    assert h.snapshot()["real"]["trades"][c["id"]]["reason"] == "broker not connected"
