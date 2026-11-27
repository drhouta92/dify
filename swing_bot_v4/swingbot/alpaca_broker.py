"""Alpaca adapter (alpaca-py 0.45.0). PAPER only, enforced by construction.

Fixes over v3:
* alpaca-py issues requests with NO timeout; a half-open TCP connection could block the
  trading loop forever. Every request now carries (connect, read) timeouts.
* Errors are classified (rejected / not found / unknown) instead of collapsing to ``None``.
* Any failed submit is resolved by client_order_id before it is reported as failed, so a
  retried POST (alpaca-py retries 429/504) or a lost response cannot become a phantom order.
"""
from __future__ import annotations

import functools
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrderByIdRequest, GetOrdersRequest, LimitOrderRequest,
                                     MarketOrderRequest,
                                     ReplaceOrderRequest, StopLossRequest, StopOrderRequest,
                                     TakeProfitRequest)

from .broker import (Account, BrokerUnavailable, Clock, Order, OrderNotFound, OrderRejected,
                     Quote)
from .config import RuntimeParams
from .indicators import ET

log = logging.getLogger("alpaca")
T = TypeVar("T")
REQUEST_TIMEOUT = (5.0, 20.0)       # (connect, read) seconds


def _install_timeout(client: Any) -> None:
    session: requests.Session = client._session
    session.request = functools.partial(session.request, timeout=REQUEST_TIMEOUT)  # type: ignore[method-assign]


def _classify(exc: Exception) -> Exception:
    if isinstance(exc, APIError):
        code = exc.status_code
        try:
            message = exc.message
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            message = str(exc)
        if code == 404:
            return OrderNotFound(message)
        if code is not None and 400 <= code < 500 and code not in (408, 429):
            return OrderRejected(f"HTTP {code}: {message}")
        return BrokerUnavailable(f"HTTP {code}: {message}")
    if isinstance(exc, (requests.RequestException, ConnectionError, TimeoutError, OSError)):
        return BrokerUnavailable(f"{type(exc).__name__}: {exc}")
    return exc


def _call(fn: Callable[[], T]) -> T:
    try:
        return fn()
    except Exception as exc:          # noqa: BLE001 - re-raised as a classified error
        classified = _classify(exc)
        if classified is exc:
            raise
        raise classified from exc


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def to_order(o: Any) -> Order:
    return Order(
        id=str(o.id), client_order_id=str(o.client_order_id), symbol=str(o.symbol),
        side=_enum_value(o.side), type=_enum_value(o.type or o.order_type), status=_enum_value(o.status),
        qty=int(float(o.qty or 0)), filled_qty=int(float(o.filled_qty or 0)),
        filled_avg_price=_float(o.filled_avg_price), order_class=_enum_value(o.order_class) or "simple",
        limit_price=_float(o.limit_price), stop_price=_float(o.stop_price), filled_at=o.filled_at,
        replaced_by=str(o.replaced_by) if o.replaced_by else None,
        legs=tuple(to_order(leg) for leg in (o.legs or ())),
    )


class AlpacaBroker:
    def __init__(self, rt: RuntimeParams):
        rt.require_credentials()
        self.trading = TradingClient(rt.api_key, rt.secret_key, paper=True)
        _install_timeout(self.trading)

    # ------------------------------------------------------------------ account
    def get_clock(self) -> Clock:
        c = _call(self.trading.get_clock)
        return Clock(pd.Timestamp(c.timestamp).tz_convert(ET), bool(c.is_open),
                     pd.Timestamp(c.next_open).tz_convert(ET), pd.Timestamp(c.next_close).tz_convert(ET))

    def get_account(self) -> Account:
        a = _call(self.trading.get_account)
        return Account(equity=float(a.equity), cash=float(a.cash), buying_power=float(a.buying_power),
                       daytrade_count=int(a.daytrade_count or 0),
                       trading_blocked=bool(a.trading_blocked or a.account_blocked),
                       status=_enum_value(a.status))

    def get_positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in _call(self.trading.get_all_positions):
            qty = float(p.qty)
            if qty != int(qty):
                raise OrderRejected(f"fractional position {p.symbol} {qty}; manual handling required")
            out[str(p.symbol)] = int(qty)
        return out

    def is_tradable(self, symbol: str) -> bool:
        try:
            asset = _call(lambda: self.trading.get_asset(symbol))
        except (OrderNotFound, OrderRejected):
            return False
        return bool(asset.tradable) and _enum_value(asset.status) == "active"

    # ------------------------------------------------------------------ orders
    def _submit(self, request: Any, client_order_id: str) -> Order:
        try:
            return to_order(_call(lambda: self.trading.submit_order(request)))
        except (OrderRejected, BrokerUnavailable) as exc:
            # A 504 retry or a lost response can leave the order created; a duplicate
            # client_order_id then returns 4xx. Only a lookup decides what happened.
            try:
                existing = self.get_order_by_client_id(client_order_id)
            except OrderNotFound:
                if isinstance(exc, OrderRejected):
                    raise
                raise BrokerUnavailable(f"submit outcome unknown for {client_order_id}: {exc}") from exc
            except BrokerUnavailable:
                raise BrokerUnavailable(f"submit outcome unknown for {client_order_id}: {exc}") from exc
            log.warning("submit for %s raised %s but the order exists (%s); adopting it",
                        client_order_id, type(exc).__name__, existing.status)
            return existing

    def submit_limit_buy(self, symbol: str, qty: int, limit: float, client_order_id: str) -> Order:
        req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                limit_price=round(limit, 2), client_order_id=client_order_id)
        return self._submit(req, client_order_id)

    def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                 client_order_id=client_order_id)
        return self._submit(req, client_order_id)

    def submit_protection(self, symbol: str, qty: int, stop: float, target: float | None,
                          client_order_id: str) -> Order:
        if target is None:
            req: Any = StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                        time_in_force=TimeInForce.GTC, stop_price=round(stop, 2),
                                        client_order_id=client_order_id)
        else:
            req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                    order_class=OrderClass.OCO,
                                    take_profit=TakeProfitRequest(limit_price=round(target, 2)),
                                    stop_loss=StopLossRequest(stop_price=round(stop, 2)),
                                    client_order_id=client_order_id)
        return self._submit(req, client_order_id)

    def get_order(self, order_id: str) -> Order:
        return to_order(_call(lambda: self.trading.get_order_by_id(order_id, GetOrderByIdRequest(nested=True))))

    def get_order_by_client_id(self, client_order_id: str) -> Order:
        return to_order(_call(lambda: self.trading.get_order_by_client_id(client_order_id)))

    def cancel_order(self, order_id: str) -> None:
        _call(lambda: self.trading.cancel_order_by_id(order_id))

    def replace_stop(self, order_id: str, stop: float, client_order_id: str) -> Order:
        return to_order(_call(lambda: self.trading.replace_order_by_id(
            order_id, ReplaceOrderRequest(stop_price=round(stop, 2), client_order_id=client_order_id))))

    def list_orders(self, symbol: str, after: datetime, status: str = "all") -> list[Order]:
        req = GetOrdersRequest(status=QueryOrderStatus(status), symbols=[symbol], after=after,
                               limit=500, nested=True)
        return [to_order(o) for o in _call(lambda: self.trading.get_orders(req))]


class AlpacaMarketData:
    def __init__(self, rt: RuntimeParams):
        rt.require_credentials()
        self.client = StockHistoricalDataClient(rt.api_key, rt.secret_key)
        _install_timeout(self.client)
        self.intraday_feed = rt.intraday_feed

    def bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime | None,
             feed: str) -> dict[str, pd.DataFrame]:
        tf = {"15m": TimeFrame(15, TimeFrameUnit.Minute), "1d": TimeFrame.Day}[timeframe]
        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=tf, start=start, end=end,
                                   adjustment=Adjustment.SPLIT, feed=DataFeed(feed))
            res = _call(lambda req=req: self.client.get_stock_bars(req))
            for sym, bars in res.data.items():
                if not bars:
                    continue
                df = pd.DataFrame([{"timestamp": b.timestamp, "open": b.open, "high": b.high, "low": b.low,
                                    "close": b.close, "volume": b.volume} for b in bars]).set_index("timestamp")
                idx = pd.DatetimeIndex(df.index)
                df.index = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(ET)
                out[str(sym)] = df.sort_index().astype(float)
            time.sleep(0.25)          # stay far below the 200 req/min data limit
        return out

    def latest_quote(self, symbol: str) -> Quote | None:
        try:
            res = _call(lambda: self.client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed(self.intraday_feed))))
        except (BrokerUnavailable, OrderRejected, OrderNotFound) as exc:
            log.warning("quote %s failed: %s", symbol, exc)
            return None
        q = res.get(symbol)
        if q is None:
            return None
        return Quote(float(q.bid_price or 0), float(q.ask_price or 0), q.timestamp)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
