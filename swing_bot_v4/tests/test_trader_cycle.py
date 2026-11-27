from __future__ import annotations

from datetime import date

import pandas as pd
from conftest import add_breakout, make_daily, make_trend_15m
from fake_broker import FakeBroker, FakeData

from swingbot.broker import Quote
from swingbot.config import RiskParams, RuntimeParams, Settings
from swingbot.liquidity import LiquidityResult
from swingbot.shariah import Compliance
from swingbot.store import Store
from swingbot.trader import Trader
from swingbot.universe import Universe


class StubBuilder:
    def __init__(self, universe: Universe):
        self.universe = universe

    def build(self, session_date: date, held: list[str]) -> Universe:
        return self.universe


def test_cycle_enters_and_protects(tmp_path):
    df = make_trend_15m(25)
    at = df.index[-14]
    df = add_breakout(df, at)
    session = at.date()
    daily = make_daily(end=str((at - pd.Timedelta(days=1)).date()))
    uni = Universe(session, ["AAA"], {"AAA": None}, {"AAA": LiquidityResult("AAA", True, "PASS")},
                   {"AAA": Compliance.COMPLIANT}, {"AAA": daily})
    settings = Settings(risk=RiskParams(sizing_equity=10_000.0),
                        runtime=RuntimeParams(data_dir=tmp_path, fill_timeout_s=5, cancel_settle_s=5))
    broker, data = FakeBroker(), FakeData()
    data.frames["AAA"] = df
    close = float(df.loc[at, "close"])
    data.quotes["AAA"] = Quote(close * 0.9995, close * 1.0005)
    broker.auto_fill_buys = close * 1.0005
    store = Store(tmp_path / "s.sqlite3")
    trader = Trader(settings, broker, data, store, StubBuilder(uni))
    trader.exe.sleep = lambda s: None
    trader.ensure_universe(session)
    res = trader.run_cycle(at + pd.Timedelta(minutes=15))
    assert res.entered == ["AAA"], res
    pos = store.positions("open")[0]
    assert pos.qty > 0 and broker.stop_order_for("AAA").qty == pos.qty
    # risk per trade honoured: (fill - stop) * qty <= 1% of 10k (+ rounding of one tick)
    assert (pos.entry_price - pos.initial_stop) * pos.qty <= 100.0 + pos.qty * 0.01
    # next bar: no duplicate entry (daily cap 1, symbol held), position managed
    res2 = trader.run_cycle(at + pd.Timedelta(minutes=30))
    assert res2.entered == [] and res2.gate in ("DAILY_TRADE_CAP", "OK")
    assert len([o for o in broker.orders.values() if o.side == "buy"]) == 1
    store.close()
