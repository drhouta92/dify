"""Broker-neutral types, error taxonomy and interfaces.

Error taxonomy is the core safety property of order handling:

* ``OrderRejected``     the broker definitively did NOT create/modify the order (HTTP 4xx).
* ``OrderNotFound``     the broker definitively has no such order (HTTP 404).
* ``BrokerUnavailable`` the outcome is UNKNOWN (timeout, connection reset, 5xx, 429).
                        The order may or may not exist; callers must resolve it by
                        client_order_id before acting again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import pandas as pd

LIVE_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled", "held",
                           "pending_replace", "accepted_for_bidding", "calculated", "pending_review"})
TERMINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced",
                               "done_for_day", "stopped", "suspended"})


class BrokerError(Exception):
    pass


class OrderRejected(BrokerError):
    pass


class OrderNotFound(BrokerError):
    pass


class BrokerUnavailable(BrokerError):
    pass


@dataclass(frozen=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: str                       # buy | sell
    type: str                       # market | limit | stop
    status: str
    qty: int
    filled_qty: int
    filled_avg_price: float | None
    order_class: str = "simple"     # simple | oco
    limit_price: float | None = None
    stop_price: float | None = None
    filled_at: datetime | None = None
    replaced_by: str | None = None
    legs: tuple["Order", ...] = field(default_factory=tuple)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def all_orders(self) -> tuple["Order", ...]:
        return (self,) + tuple(o for leg in self.legs for o in leg.all_orders())

    @property
    def stop_leg(self) -> "Order | None":
        for o in self.all_orders():
            if o.type in ("stop", "stop_limit"):
                return o
        return None


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp: datetime | None = None

    @property
    def valid(self) -> bool:
        return 0 < self.bid <= self.ask

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid


@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    daytrade_count: int
    trading_blocked: bool
    status: str


@dataclass(frozen=True)
class Clock:
    timestamp: pd.Timestamp
    is_open: bool
    next_open: pd.Timestamp
    next_close: pd.Timestamp


class Broker(Protocol):
    def get_clock(self) -> Clock: ...
    def get_account(self) -> Account: ...
    def get_positions(self) -> dict[str, int]: ...
    def submit_limit_buy(self, symbol: str, qty: int, limit: float, client_order_id: str) -> Order: ...
    def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order: ...
    def submit_protection(self, symbol: str, qty: int, stop: float, target: float | None,
                          client_order_id: str) -> Order: ...
    def get_order(self, order_id: str) -> Order: ...
    def get_order_by_client_id(self, client_order_id: str) -> Order: ...
    def cancel_order(self, order_id: str) -> None: ...
    def replace_stop(self, order_id: str, stop: float, client_order_id: str) -> Order: ...
    def list_orders(self, symbol: str, after: datetime, status: str = "all") -> list[Order]: ...
    def is_tradable(self, symbol: str) -> bool: ...


class MarketData(Protocol):
    def bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime | None,
             feed: str) -> dict[str, pd.DataFrame]: ...
    def latest_quote(self, symbol: str) -> Quote | None: ...
