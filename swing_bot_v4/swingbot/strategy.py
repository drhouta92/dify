"""Strategy logic: pure functions, no I/O, no clock reads.

Every function takes an explicit ``bar_start`` (start of the most recent CLOSED 15m bar
the caller expects) and ``asof`` (the decision time). The live trader and the backtester
call exactly these functions, so a backtest measures the code that trades.

ENTRY on the closed 15m bar starting at ``bar_start``, all of:
  daily trend  last completed daily close > EMA(daily_ema)
  1h trend     EMA(fast) > EMA(slow) and close > EMA(slow), complete session-aligned 1h bars
  breakout     close > max(high of previous ``breakout_lookback`` bars); previous bar did not
               already close above its own breakout level (first bar of a fresh push)
  participation  volume > min_relative_volume * mean(volume of previous vol_avg_lookback bars)
  bullish body (close - open) / (high - low) >= body_ratio_min
  location     close > EMA(15m) and close > rolling VWAP
  sanity       bar start within [first_entry_bar, last_entry_bar]; daily ATR / price <= max_atr_pct

EXIT management per closed 15m bar:
  initial stop  fill - stop_atr_mult * ATR(daily)                   (resting broker stop)
  target        fill + take_profit_r * initial risk                 (resting broker limit, OCO)
  breakeven     HWM >= fill + be_trigger_atr * ATR(daily) -> stop >= fill + be_offset
  trail         after breakeven: stop >= HWM - trail_atr_mult_1h * ATR(1h)
  stop is monotone non-decreasing
  time          on the time_exit_bar of session max_hold_sessions
  event         on the time_exit_bar when earnings are within earnings_exit_business_days
  trend         complete 1h close < EMA(slow) and EMA(fast) < EMA(slow)
  compliance    Shariah status turned non-compliant
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .config import StrategyParams
from .indicators import (BAR_15M, completed_daily, ema, resample_session_1h, rolling_vwap,
                         wilder_atr)

TICK = 0.01


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


@dataclass(frozen=True)
class DailyContext:
    session_date: date
    atr_d: float
    ema_d: float
    close_d: float


def daily_context(daily: pd.DataFrame | None, session_date: date, p: StrategyParams) -> DailyContext | None:
    d = completed_daily(daily, session_date)
    if len(d) < max(p.min_daily_bars, p.atr_len + 1):
        return None
    atr_d = float(wilder_atr(d, p.atr_len).iloc[-1])
    if not np.isfinite(atr_d) or atr_d <= 0:
        return None
    return DailyContext(session_date, atr_d, float(ema(d["close"], p.daily_ema).iloc[-1]),
                        float(d["close"].iloc[-1]))


@dataclass(frozen=True)
class EntryDecision:
    enter: bool
    reason: str
    features: dict[str, Any] = field(default_factory=dict)


def evaluate_entry(df15: pd.DataFrame | None, ctx: DailyContext | None, bar_start: pd.Timestamp,
                   asof: pd.Timestamp, p: StrategyParams) -> EntryDecision:
    """``df15`` must hold only bars closed at ``asof`` (see indicators.closed_bars)."""
    if df15 is None or len(df15) < p.min_15m_bars:
        return EntryDecision(False, "NO_DATA")
    if ctx is None:
        return EntryDecision(False, "NO_DAILY")
    if df15.index[-1] != bar_start:
        return EntryDecision(False, "STALE_BAR")      # no print in the expected bar (common on IEX)
    t = bar_start.time()
    if t < p.first_entry_bar:
        return EntryDecision(False, "EARLY")
    if t > p.last_entry_bar:
        return EntryDecision(False, "LATE")

    h1 = resample_session_1h(df15, asof)
    if len(h1) < p.min_1h_bars:
        return EntryDecision(False, "NO_1H")
    h_fast = ema(h1["close"], p.ema_fast_1h).iloc[-1]
    h_slow = ema(h1["close"], p.ema_slow_1h).iloc[-1]
    atr_1h = float(wilder_atr(h1, p.atr_len).iloc[-1])

    close, high, open_ = df15["close"], df15["high"], df15["open"]
    prior_high = high.shift(1).rolling(p.breakout_lookback).max()
    breakout = close > prior_high
    vol_base = df15["volume"].shift(1).rolling(p.vol_avg_lookback).mean()
    vwap = rolling_vwap(df15, p.vwap_lookback)
    ema15 = ema(close, p.ema_15m)

    last = df15.iloc[-1]
    rng = float(last["high"] - last["low"])
    price = float(last["close"])
    base = float(vol_base.iloc[-1]) if pd.notna(vol_base.iloc[-1]) else float("nan")
    rel_vol = float(last["volume"]) / base if base and np.isfinite(base) and base > 0 else 0.0
    body_ratio = float(last["close"] - last["open"]) / rng if rng > 0 else 0.0
    close_loc = float(last["close"] - last["low"]) / rng if rng > 0 else 0.0
    last_vwap = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else float("nan")

    flags = {
        "daily_trend": ctx.close_d > ctx.ema_d,
        "h1_trend": bool(h_fast > h_slow and h1["close"].iloc[-1] > h_slow),
        "breakout": bool(breakout.iloc[-1]) and not bool(breakout.iloc[-2]),
        "participation": rel_vol > p.min_relative_volume,
        "bullish_body": body_ratio >= p.body_ratio_min,
        "above_ema15": price > float(ema15.iloc[-1]),
        "above_vwap": np.isfinite(last_vwap) and price > last_vwap,
    }
    features: dict[str, Any] = {
        **flags,
        "price": price, "atr_d": ctx.atr_d, "atr_1h": atr_1h if np.isfinite(atr_1h) else None,
        "atr_pct": ctx.atr_d / price, "relative_volume": rel_vol, "body_ratio": body_ratio,
        "close_location": close_loc, "vwap": last_vwap if np.isfinite(last_vwap) else None,
        "breakout_level": float(prior_high.iloc[-1]) if pd.notna(prior_high.iloc[-1]) else None,
        "bar_start": bar_start.isoformat(), "open": float(open_.iloc[-1]),
    }
    if ctx.atr_d / price > p.max_atr_pct:
        return EntryDecision(False, "ATR_TOO_WIDE", features)
    failed = [k for k, v in flags.items() if not v]
    if failed:
        return EntryDecision(False, "FAILED:" + ",".join(failed), features)
    return EntryDecision(True, "ENTRY", features)


def initial_levels(fill: float, atr_d: float, p: StrategyParams) -> tuple[float, float | None]:
    """(stop, target) for a fill. Target is None when take_profit_r == 0."""
    stop = round_tick(fill - p.stop_atr_mult * atr_d)
    target = round_tick(fill + p.take_profit_r * (fill - stop)) if p.take_profit_r > 0 else None
    return stop, target


@dataclass
class PositionView:
    entry_price: float
    entry_time: pd.Timestamp
    hwm_from: pd.Timestamp          # first bar START whose high counts toward the high-water mark
    atr_d: float
    initial_stop: float
    stop: float
    hwm: float
    be_locked: bool
    target: float | None = None
    next_earnings: date | None = None


@dataclass(frozen=True)
class ManageDecision:
    action: str                     # HOLD | EXIT_STOP_BACKUP | EXIT_TIME | EXIT_EVENT | EXIT_TREND | EXIT_SHARIAH
    stop: float
    hwm: float
    be_locked: bool
    price: float | None
    sessions_held: int
    reason: str = ""


def manage_position(pos: PositionView, df15: pd.DataFrame | None, bar_start: pd.Timestamp,
                    asof: pd.Timestamp, p: StrategyParams, shariah_exit: bool = False) -> ManageDecision:
    if df15 is None or df15.empty:
        action = "EXIT_SHARIAH" if shariah_exit else "HOLD"
        return ManageDecision(action, pos.stop, pos.hwm, pos.be_locked, None, 0, "NO_DATA")

    since = df15[df15.index >= pos.hwm_from]
    hwm = max(pos.hwm, float(since["high"].max())) if not since.empty else pos.hwm
    be = pos.be_locked or hwm >= pos.entry_price + p.be_trigger_atr * pos.atr_d

    h1 = resample_session_1h(df15, asof)
    atr_1h = float(wilder_atr(h1, p.atr_len).iloc[-1]) if len(h1) > p.atr_len else float("nan")
    stop = pos.stop
    if be:
        stop = max(stop, pos.entry_price + p.be_offset)
        if np.isfinite(atr_1h):
            stop = max(stop, hwm - p.trail_atr_mult_1h * atr_1h)
    stop = max(round_tick(stop), pos.stop)            # monotone after rounding

    price = float(df15["close"].iloc[-1])
    entry_day = pos.entry_time.date()
    sessions = len({d for d in df15.index.date if d >= entry_day} | {bar_start.date()})
    decision_bar = bar_start.time() >= p.time_exit_bar

    def out(action: str, reason: str = "") -> ManageDecision:
        return ManageDecision(action, stop, hwm, be, price, sessions, reason)

    if shariah_exit:
        return out("EXIT_SHARIAH")
    if price <= stop:
        return out("EXIT_STOP_BACKUP", "closed at/below stop; broker stop did not trigger")
    if decision_bar and sessions >= p.max_hold_sessions:
        return out("EXIT_TIME")
    if decision_bar and pos.next_earnings is not None:
        if int(np.busday_count(bar_start.date(), pos.next_earnings)) <= p.earnings_exit_business_days:
            return out("EXIT_EVENT", f"earnings {pos.next_earnings}")
    if p.trend_exit and len(h1) >= p.ema_slow_1h + 1:
        fast = ema(h1["close"], p.ema_fast_1h).iloc[-1]
        slow = ema(h1["close"], p.ema_slow_1h).iloc[-1]
        if h1["close"].iloc[-1] < slow and fast < slow:
            return out("EXIT_TREND")
    return out("HOLD")


def hwm_start_for_fill(fill_time: pd.Timestamp) -> pd.Timestamp:
    """Live fills land inside a bar; that bar's high may predate the fill, so start at the next bar."""
    return fill_time.floor("15min") + BAR_15M
