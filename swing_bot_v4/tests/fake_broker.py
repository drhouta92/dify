"""In-memory broker that reproduces the Alpaca behaviours the execution layer depends on.

* client_order_id must be unique forever (duplicate -> OrderRejected, like Alpaca's 422)
* sell qty is limited to position qty minus qty held by open sell orders
* OCO: a limit (take-profit) parent with one stop leg; one fill cancels the other
* replace creates a new order id; the old one becomes 'replaced' with replaced_by set
* failure injection: the next call of a method can raise BrokerUnavailable either
  before (order NOT created) or after (order created, response lost) the side effect
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime

import pandas as pd

from swingbot.broker import (Account, BrokerUnavailable, Clock, Order, OrderNotFound, OrderRejected,
                             Quote)
from swingbot.indicators import ET


class FakeBroker:
    def __init__(self, cash: float = 100_000.0):
        self.orders: dict[str, Order] = {}
        self.by_coid: dict[str, str] = {}
        self.parent_of: dict[str, str] = {}
        self.positions: dict[str, int] = {}
        self.cost: dict[str, float] = {}
        self.cash = cash
        self.ids = itertools.count(1)
        self.fail: dict[str, str] = {}            # method -> 'before' | 'after'
        self.auto_fill_buys: float | None = None   # fill price for new buys, None = rest
        self.fill_qty: int | None = None           # partial fill size for buys
        self.reject_replace = False
        self.now = pd.Timestamp("2026-03-02 10:30:20", tz=ET)

    # -------------------------------------------------------------- helpers
    def _maybe_fail(self, method: str, stage: str) -> None:
        if self.fail.get(method) == stage:
            del self.fail[method]
            raise BrokerUnavailable(f"injected {stage} failure in {method}")

    def _new(self, **kw) -> Order:
        coid = kw["client_order_id"]
        if coid in self.by_coid:
            raise OrderRejected("HTTP 422: client_order_id must be unique")
        oid = f"o{next(self.ids)}"
        o = Order(id=oid, filled_qty=0, filled_avg_price=None, **kw)
        self.orders[oid] = o
        self.by_coid[coid] = oid
        return o

    def _held_for_sells(self, symbol: str, exclude: set[str] = frozenset()) -> int:
        seen_groups: set[str] = set()
        held = 0
        for o in self.orders.values():
            if o.symbol != symbol or o.side != "sell" or not o.is_live or o.id in exclude:
                continue
            group = self.parent_of.get(o.id, o.id)
            if group in seen_groups:
                continue
            seen_groups.add(group)
            held += o.qty - o.filled_qty
        return held

    def _set(self, o: Order, **kw) -> Order:
        o = replace(o, **kw)
        self.orders[o.id] = o
        return o

    def _with_legs(self, o: Order) -> Order:
        legs = tuple(self._with_legs(self.orders[c]) for c, p in self.parent_of.items() if p == o.id)
        return replace(self.orders[o.id], legs=legs)

    # -------------------------------------------------------------- simulation controls
    def fill(self, order_id: str, price: float, qty: int | None = None) -> None:
        o = self.orders[order_id]
        q = o.qty - o.filled_qty if qty is None else qty
        new_filled = o.filled_qty + q
        avg = ((o.filled_avg_price or 0) * o.filled_qty + price * q) / new_filled
        status = "filled" if new_filled == o.qty else "partially_filled"
        self._set(o, filled_qty=new_filled, filled_avg_price=avg, status=status, filled_at=self.now.to_pydatetime())
        sign = 1 if o.side == "buy" else -1
        self.positions[o.symbol] = self.positions.get(o.symbol, 0) + sign * q
        if self.positions[o.symbol] == 0:
            del self.positions[o.symbol]
        group = self.parent_of.get(o.id, o.id)
        if status == "filled":
            for other in [g for g in self.orders.values() if (self.parent_of.get(g.id, g.id) == group) and g.id != o.id]:
                if other.is_live:
                    self._set(other, status="canceled")

    def stop_order_for(self, symbol: str) -> Order | None:
        for o in self.orders.values():
            if o.symbol == symbol and o.type == "stop" and o.is_live:
                return o
        return None

    def live_sells(self, symbol: str) -> list[Order]:
        return [o for o in self.orders.values() if o.symbol == symbol and o.side == "sell" and o.is_live]

    # -------------------------------------------------------------- Broker protocol
    def get_clock(self) -> Clock:
        return Clock(self.now, True, self.now + pd.Timedelta(days=1), self.now.normalize() + pd.Timedelta(hours=16))

    def get_account(self) -> Account:
        return Account(self.cash, self.cash, self.cash, 0, False, "active")

    def get_positions(self) -> dict[str, int]:
        self._maybe_fail("get_positions", "before")
        return dict(self.positions)

    def is_tradable(self, symbol: str) -> bool:
        return True

    def submit_limit_buy(self, symbol: str, qty: int, limit: float, client_order_id: str) -> Order:
        self._maybe_fail("submit_limit_buy", "before")
        o = self._new(client_order_id=client_order_id, symbol=symbol, side="buy", type="limit", status="new",
                      qty=qty, limit_price=limit)
        if self.auto_fill_buys is not None:
            self.fill(o.id, self.auto_fill_buys, self.fill_qty)
            if self.fill_qty is not None and self.fill_qty < qty:
                pass
        self._maybe_fail("submit_limit_buy", "after")
        return self._with_legs(self.orders[o.id])

    def _check_sell(self, symbol: str, qty: int) -> None:
        if qty > self.positions.get(symbol, 0) - self._held_for_sells(symbol):
            raise OrderRejected("HTTP 403: insufficient qty available for order")

    def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        self._maybe_fail("submit_market_sell", "before")
        self._check_sell(symbol, qty)
        o = self._new(client_order_id=client_order_id, symbol=symbol, side="sell", type="market", status="new",
                      qty=qty)
        self._maybe_fail("submit_market_sell", "after")
        return self._with_legs(o)

    def submit_protection(self, symbol: str, qty: int, stop: float, target: float | None,
                          client_order_id: str) -> Order:
        self._maybe_fail("submit_protection", "before")
        self._check_sell(symbol, qty)
        if target is None:
            o = self._new(client_order_id=client_order_id, symbol=symbol, side="sell", type="stop", status="new",
                          qty=qty, stop_price=stop)
        else:
            o = self._new(client_order_id=client_order_id, symbol=symbol, side="sell", type="limit", status="new",
                          qty=qty, limit_price=target, order_class="oco")
            leg = self._new(client_order_id=client_order_id + "-sl", symbol=symbol, side="sell", type="stop",
                            status="held", qty=qty, stop_price=stop, order_class="oco")
            self.parent_of[leg.id] = o.id
        self._maybe_fail("submit_protection", "after")
        return self._with_legs(o)

    def get_order(self, order_id: str) -> Order:
        if order_id not in self.orders:
            raise OrderNotFound(order_id)
        return self._with_legs(self.orders[order_id])

    def get_order_by_client_id(self, client_order_id: str) -> Order:
        if client_order_id not in self.by_coid:
            raise OrderNotFound(client_order_id)
        return self.get_order(self.by_coid[client_order_id])

    def cancel_order(self, order_id: str) -> None:
        self._maybe_fail("cancel_order", "before")
        o = self.orders.get(order_id)
        if o is None:
            raise OrderNotFound(order_id)
        if not o.is_live:
            raise OrderRejected("HTTP 422: order is not cancelable")
        group = self.parent_of.get(order_id, order_id)
        for g in list(self.orders.values()):
            if self.parent_of.get(g.id, g.id) == group and g.is_live:
                self._set(g, status="canceled")

    def replace_stop(self, order_id: str, stop: float, client_order_id: str) -> Order:
        self._maybe_fail("replace_stop", "before")
        if self.reject_replace:
            raise OrderRejected("HTTP 422: replace not allowed")
        o = self.orders[order_id]
        if not o.is_live:
            raise OrderRejected("HTTP 422: order not replaceable")
        new = self._new(client_order_id=client_order_id, symbol=o.symbol, side=o.side, type=o.type,
                        status=o.status, qty=o.qty, stop_price=stop, order_class=o.order_class)
        if order_id in self.parent_of:
            self.parent_of[new.id] = self.parent_of.pop(order_id)
        self._set(o, status="replaced", replaced_by=new.id)
        self._maybe_fail("replace_stop", "after")
        return self._with_legs(new)

    def list_orders(self, symbol: str, after: datetime, status: str = "all") -> list[Order]:
        return [self._with_legs(o) for o in self.orders.values() if o.symbol == symbol and o.id not in self.parent_of]


class FakeData:
    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.frames: dict[str, pd.DataFrame] = {}

    def latest_quote(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol)

    def bars(self, symbols, timeframe, start, end, feed):
        return {s: self.frames[s] for s in symbols if s in self.frames}
