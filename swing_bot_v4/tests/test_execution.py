"""Order-lifecycle tests against FakeBroker, including every v3 failure mode found in review."""
from __future__ import annotations

import dataclasses

import pytest
from fake_broker import FakeBroker

from swingbot.execution import Execution
from swingbot.risk import Candidate
from swingbot.store import Store


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def monotonic(self):
        return self.t


@pytest.fixture
def env(tmp_path, sp, rp, rt):
    broker = FakeBroker()
    store = Store(tmp_path / "s.sqlite3")
    clk = Clock()
    exe = Execution(broker, store, sp, rp, rt, sleep=clk.sleep, monotonic=clk.monotonic)
    yield broker, store, exe
    store.close()


def cand(symbol="ABC", limit=20.06, mid=20.0, atr=0.5):
    return Candidate(symbol, limit, mid, atr, 2.0, 0.002, {"price": mid})


def open_filled(broker, exe, qty=10, fill=20.05):
    broker.auto_fill_buys = fill
    pos = exe.open_position(cand(), qty, None)
    broker.auto_fill_buys = None
    return pos


def test_happy_path_entry_protect_raise_exit(env, sp):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    assert pos.status == "open" and pos.qty == 10
    assert pos.stop == pytest.approx(20.05 - 2 * 0.5)
    sells = broker.live_sells("ABC")
    assert {o.type for o in sells} == {"limit", "stop"}               # OCO: target + stop
    exe.update_stop(store.position(pos.id), 19.80, hwm=20.6, be_locked=True)
    assert broker.stop_order_for("ABC").stop_price == 19.80
    assert store.position(pos.id).stop == 19.80
    assert exe.exit_position(store.position(pos.id), "EXIT_TREND") is False   # market order rests
    mkt = [o for o in broker.live_sells("ABC") if o.type == "market"][0]
    broker.fill(mkt.id, 20.40)
    exe.reconcile()
    closed = store.position(pos.id)
    assert closed.status == "closed" and closed.exit_reason == "EXIT_TREND"
    assert closed.realized_pnl == pytest.approx((20.40 - 20.05) * 10)
    assert closed.meta["r_multiple"] == pytest.approx(0.35 / 1.0, rel=1e-3)


def test_lost_buy_response_is_recovered_without_duplicate(env):
    broker, store, exe = env
    broker.auto_fill_buys = 20.05
    broker.fail["submit_limit_buy"] = "after"          # created at broker, response lost
    pos = exe.open_position(cand(), 10, None)
    assert pos.status == "opening"
    assert exe.unresolved_count() == 1                 # entries blocked for this position only
    exe.reconcile()
    p = store.position(pos.id)
    assert p.status == "open" and p.qty == 10
    assert len([o for o in broker.orders.values() if o.side == "buy"]) == 1
    assert broker.stop_order_for("ABC") is not None
    assert exe.unresolved_count() == 0


def test_buy_that_never_reached_broker_is_released(env, rt):
    broker, store, exe = env
    exe.rt = dataclasses.replace(rt, unknown_order_grace_min=0)
    broker.fail["submit_limit_buy"] = "before"
    pos = exe.open_position(cand(), 10, None)
    assert pos.status == "opening"
    exe.reconcile()
    assert store.position(pos.id).status == "void"
    assert exe.unresolved_count() == 0


def test_unfilled_limit_is_cancelled_and_voided(env):
    broker, store, exe = env
    pos = exe.open_position(cand(), 10, None)                    # rests unfilled -> cancelled
    assert pos is None
    assert all(not o.is_live for o in broker.orders.values())
    assert exe.unresolved_count() == 0


def test_rejected_buy_voids_without_blocking(env):
    broker, store, exe = env

    def reject(*a, **k):
        from swingbot.broker import OrderRejected
        raise OrderRejected("HTTP 403: asset not tradable")
    broker.submit_limit_buy = reject
    assert exe.open_position(cand(), 10, None) is None
    assert exe.unresolved_count() == 0
    assert [p.status for p in store.positions()] == ["void"]


def test_protection_lost_response_not_duplicated(env):
    broker, store, exe = env
    broker.fail["submit_protection"] = "after"
    pos = open_filled(broker, exe)
    assert len(broker.live_sells("ABC")) == 2                    # exactly one OCO group
    assert store.position(pos.id).status in ("open", "closing")
    exe.reconcile()
    assert len(broker.live_sells("ABC")) == 2
    assert store.position(pos.id).status == "open"


def test_replace_response_lost_follows_replaced_by(env):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    broker.fail["replace_stop"] = "after"
    exe.update_stop(store.position(pos.id), 19.90, hwm=20.7, be_locked=True)
    assert store.position(pos.id).stop < 19.90                   # not acknowledged yet
    exe.reconcile()
    assert store.position(pos.id).stop == pytest.approx(19.90)
    assert broker.stop_order_for("ABC").stop_price == 19.90
    assert exe.exit_position(store.position(pos.id), "EXIT_TIME") is False
    assert not any(o.type == "stop" for o in broker.live_sells("ABC"))   # cancel hit the live order


def test_replace_rejected_falls_back_to_reissue(env):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    broker.reject_replace = True
    exe.update_stop(store.position(pos.id), 19.70, hwm=20.6, be_locked=True)
    stop = broker.stop_order_for("ABC")
    assert stop.stop_price == 19.70 and stop.qty == 10
    assert store.position(pos.id).stop == 19.70


def test_broker_stop_fill_closes_with_real_price(env):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    broker.fill(broker.stop_order_for("ABC").id, 18.97)
    exe.reconcile()
    p = store.position(pos.id)
    assert p.status == "closed" and p.exit_reason == "EXIT_STOP"
    assert p.realized_pnl == pytest.approx((18.97 - 20.05) * 10)
    assert store.get("last_exit:ABC") is not None


def test_target_fill_closes_as_target(env):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    tp = [o for o in broker.live_sells("ABC") if o.type == "limit"][0]
    broker.fill(tp.id, tp.limit_price)
    exe.reconcile()
    p = store.position(pos.id)
    assert p.exit_reason == "EXIT_TARGET" and p.realized_pnl > 0
    assert broker.live_sells("ABC") == []


def test_unexplained_qty_quarantines_symbol_only(env):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    broker.positions["ABC"] = 4                                  # manual sale outside the bot
    rep = exe.reconcile()
    assert store.position(pos.id).status == "quarantined"
    assert rep.quarantined == ["ABC"]


def test_partial_entry_fill_is_protected_for_filled_qty(env):
    broker, store, exe = env
    broker.fill_qty = 4
    pos = open_filled(broker, exe, qty=10)
    broker.fill_qty = None
    assert pos.status == "open" and pos.qty == 4
    assert broker.stop_order_for("ABC").qty == 4
    assert not any(o.side == "buy" and o.is_live for o in broker.orders.values())   # remainder cancelled


def test_exit_unknown_does_not_double_sell(env):
    broker, store, exe = env
    pos = open_filled(broker, exe)
    broker.fail["submit_market_sell"] = "after"
    assert exe.exit_position(store.position(pos.id), "EXIT_TREND") is False
    assert exe.exit_position(store.position(pos.id), "EXIT_TREND") is False      # resolves, does not resubmit
    markets = [o for o in broker.orders.values() if o.type == "market"]
    assert len(markets) == 1
    broker.fill(markets[0].id, 20.30)
    exe.reconcile()
    assert store.position(pos.id).status == "closed"


def test_unprotectable_entry_is_flattened(env, rt):
    broker, store, exe = env
    exe.rt = dataclasses.replace(rt, protect_retries=2)
    real = broker.submit_protection

    def always_reject(*a, **k):
        from swingbot.broker import OrderRejected
        raise OrderRejected("HTTP 422: invalid stop")
    broker.submit_protection = always_reject
    pos = open_filled(broker, exe)
    assert store.position(pos.id).status == "closing"
    mkt = [o for o in broker.orders.values() if o.type == "market"][0]
    broker.fill(mkt.id, 20.00)
    broker.submit_protection = real
    exe.reconcile()
    p = store.position(pos.id)
    assert p.status == "closed" and p.exit_reason == "EXIT_UNPROTECTED"


def test_restart_reuses_state(tmp_path, sp, rp, rt):
    broker = FakeBroker()
    store = Store(tmp_path / "s.sqlite3")
    exe = Execution(broker, store, sp, rp, rt, sleep=lambda s: None)
    pos = open_filled(broker, exe)
    store.close()
    store2 = Store(tmp_path / "s.sqlite3")
    exe2 = Execution(broker, store2, sp, rp, rt, sleep=lambda s: None)
    exe2.reconcile()
    assert store2.position(pos.id).status == "open"
    assert len(broker.live_sells("ABC")) == 2
    store2.close()
