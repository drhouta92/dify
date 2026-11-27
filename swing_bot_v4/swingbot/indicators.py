"""Pure, causal indicator functions. Value at row t depends only on rows <= t.

All bar frames are indexed by tz-aware ET bar START timestamps with columns
open, high, low, close, volume.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

ET = "America/New_York"
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
BAR_15M = pd.Timedelta(minutes=15)


def ema(series: pd.Series, n: int) -> pd.Series:
    """Standard EMA, alpha = 2 / (n + 1), seeded with the first observation."""
    return series.ewm(span=n, adjust=False, min_periods=1).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat([df["high"] - df["low"],
                        (df["high"] - prev_close).abs(),
                        (df["low"] - prev_close).abs()], axis=1)
    return ranges.max(axis=1)


def wilder_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR (RMA of true range); NaN until n true ranges exist."""
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rolling_vwap(df: pd.DataFrame, n: int, min_periods: int = 30) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).rolling(n, min_periods=min_periods).sum()
    vol = df["volume"].rolling(n, min_periods=min_periods).sum().replace(0, np.nan)
    return pv / vol


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep bars whose START is within [09:30, 16:00)."""
    if df.empty:
        return df
    return df[(df.index.time >= SESSION_OPEN) & (df.index.time < SESSION_CLOSE)]


def closed_bars(df: pd.DataFrame, asof: pd.Timestamp, bar: pd.Timedelta = BAR_15M) -> pd.DataFrame:
    """Drop every bar whose close (start + bar) is after ``asof``."""
    if df.empty:
        return df
    return df[df.index + bar <= asof]


def resample_session_1h(df15: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """Session-aligned hourly bars (09:30, 10:30, ... 15:30) that are complete at ``asof``.

    Completeness is decided by the clock, not by data presence: with the IEX feed
    a quiet 15m interval has no bar, so "the last 15m bar exists" is not evidence
    that the hour is finished. A bin is complete when its end time <= asof, where
    the end of the 15:30 bin is the 16:00 close.
    """
    if df15.empty:
        return df15.iloc[0:0]
    hourly = df15.resample("60min", offset="30min", closed="left", label="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    if hourly.empty:
        return hourly
    starts = hourly.index
    session_end = starts.normalize() + pd.Timedelta(hours=16)
    bin_end = (starts + pd.Timedelta(minutes=60)).where(starts + pd.Timedelta(minutes=60) <= session_end,
                                                        session_end)
    return hourly[bin_end <= asof]


def completed_daily(daily: pd.DataFrame, session_date) -> pd.DataFrame:
    """Daily bars strictly before ``session_date`` (today's forming bar excluded)."""
    if daily is None or daily.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    dates = pd.Index(daily.index.tz_convert(ET).date if daily.index.tz is not None else daily.index.date)
    return daily[dates < session_date]
