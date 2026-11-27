from __future__ import annotations

import dataclasses
from datetime import date, time

import numpy as np
import pandas as pd
import pytest
from conftest import add_breakout, make_daily, make_trend_15m

from swingbot.config import ConfigError, Settings, StrategyParams
from swingbot.indicators import ET, closed_bars, ema, resample_session_1h, wilder_atr
from swingbot.strategy import (PositionView, daily_context, evaluate_entry, initial_levels,
                               manage_position)


def test_indicators_are_causal():
    df = make_trend_15m(10)
    cut = 150
    full_e, part_e = ema(df["close"], 21), ema(df["close"].iloc[:cut], 21)
    assert np.allclose(full_e.iloc[:cut], part_e)
    full_a, part_a = wilder_atr(df, 14), wilder_atr(df.iloc[:cut], 14)
    assert np.allclose(full_a.iloc[:cut].dropna(), part_a.dropna())


def test_hourly_bins_complete_by_clock_not_by_data():
    df = make_trend_15m(2)
    day = df.index[0].normalize()
    asof = day + pd.Timedelta(hours=10, minutes=45)          # 10:30 bin half done
    h = resample_session_1h(closed_bars(df, asof), asof)
    assert h.index[-1] == day + pd.Timedelta(hours=9, minutes=30)
    # drop the 10:15 bar (IEX gap): the 09:30 bin must still be complete at 10:30
    gappy = df.drop(day + pd.Timedelta(hours=10, minutes=15))
    asof2 = day + pd.Timedelta(hours=10, minutes=30)
    h2 = resample_session_1h(closed_bars(gappy, asof2), asof2)
    assert list(h2.index) == [day + pd.Timedelta(hours=9, minutes=30)]
    # the 15:30 bin ends at the 16:00 close
    close_asof = day + pd.Timedelta(hours=16)
    assert resample_session_1h(closed_bars(df, close_asof), close_asof).index[-1] == day + pd.Timedelta(hours=15, minutes=30)


def _entry_setup(bullish: bool = True):
    df = make_trend_15m(25)
    at = df.index[-14]                                       # a 12:15 bar on the last day
    df = add_breakout(df, at, bullish=bullish)
    asof = at + pd.Timedelta(minutes=15)
    window = closed_bars(df, asof)
    daily = make_daily(end=str((at - pd.Timedelta(days=1)).date()))
    return window, daily, at, asof


def test_entry_fires_on_fresh_bullish_breakout(sp):
    window, daily, at, asof = _entry_setup()
    ctx = daily_context(daily, at.date(), sp)
    dec = evaluate_entry(window, ctx, at, asof, sp)
    assert dec.enter, dec.reason
    assert dec.features["relative_volume"] > 1


def test_bearish_breakout_bar_rejected(sp):
    window, daily, at, asof = _entry_setup(bullish=False)
    dec = evaluate_entry(window, daily_context(daily, at.date(), sp), at, asof, sp)
    assert not dec.enter
    assert "bullish_body" in dec.reason


def test_stale_bar_and_time_window(sp):
    window, daily, at, asof = _entry_setup()
    ctx = daily_context(daily, at.date(), sp)
    assert evaluate_entry(window.iloc[:-1], ctx, at, asof, sp).reason == "STALE_BAR"
    late = dataclasses.replace(sp, last_entry_bar=time(11, 0))
    assert evaluate_entry(window, ctx, at, asof, late).reason == "LATE"


def test_daily_context_excludes_forming_day(sp):
    daily = make_daily(end="2026-03-02")
    ctx_today = daily_context(daily, date(2026, 3, 2), sp)
    ctx_next = daily_context(daily, date(2026, 3, 3), sp)
    assert ctx_today.close_d < ctx_next.close_d


def _view(entry=20.0, atr=0.5, sp=None, when="2026-02-02 10:30:20"):
    sp = sp or StrategyParams()
    t = pd.Timestamp(when, tz=ET)
    stop, target = initial_levels(entry, atr, sp)
    return PositionView(entry, t, t.floor("15min") + pd.Timedelta(minutes=15), atr, stop, stop, entry, False, target)


def test_breakeven_and_trailing_are_monotone(sp):
    df = make_trend_15m(5, start="2026-02-02", drift=0.002, base=20.0)
    pos = _view(entry=20.0, atr=0.3)
    stops = []
    for i in range(30, len(df) - 1):
        bar_start = df.index[i]
        if bar_start.time() > time(15, 30):
            continue
        asof = bar_start + pd.Timedelta(minutes=15)
        d = manage_position(pos, closed_bars(df, asof), bar_start, asof, sp)
        if d.action != "HOLD":
            break
        pos.stop, pos.hwm, pos.be_locked = d.stop, d.hwm, d.be_locked
        stops.append(d.stop)
    assert stops == sorted(stops)
    assert pos.be_locked and pos.stop >= pos.entry_price + sp.be_offset


def test_time_exit_on_last_session(sp):
    df = make_trend_15m(6, start="2026-02-02", drift=0.0, base=20.0)
    pos = _view(entry=float(df["close"].iloc[5]), atr=2.0)
    last_day = df.index[-1].normalize()
    bar = last_day + pd.Timedelta(hours=15, minutes=30)
    params = dataclasses.replace(sp, trend_exit=False)
    d = manage_position(pos, closed_bars(df, bar + pd.Timedelta(minutes=15)), bar, bar + pd.Timedelta(minutes=15), params)
    assert d.sessions_held >= params.max_hold_sessions
    assert d.action == "EXIT_TIME"


def test_earnings_exit(sp):
    df = make_trend_15m(2, start="2026-02-02", drift=0.0, base=20.0)
    pos = _view(entry=20.0, atr=2.0)
    pos.next_earnings = date(2026, 2, 4)
    bar = df.index[-1].normalize() + pd.Timedelta(hours=15, minutes=30)
    params = dataclasses.replace(sp, trend_exit=False)
    d = manage_position(pos, closed_bars(df, bar + pd.Timedelta(minutes=15)), bar, bar + pd.Timedelta(minutes=15), params)
    assert d.action == "EXIT_EVENT"


def test_settings_from_env_and_validation():
    s = Settings.from_env({"SWING_RISK_PER_TRADE": "0.005", "SWING_LAST_ENTRY_BAR": "14:45",
                           "SWING_TREND_EXIT": "false", "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert s.risk.risk_per_trade == 0.005
    assert s.strategy.last_entry_bar == time(14, 45)
    assert s.strategy.trend_exit is False
    with pytest.raises(ConfigError):
        Settings.from_env({"SWING_RISK_PER_TRADE": "nan"})
    with pytest.raises(ConfigError):
        Settings.from_env({"APCA_API_BASE_URL": "https://api.alpaca.markets"})
    with pytest.raises(ConfigError):
        Settings.from_env({}).runtime.require_credentials()
