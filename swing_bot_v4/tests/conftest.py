from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from swingbot.config import RiskParams, RuntimeParams, StrategyParams  # noqa: E402
from swingbot.indicators import ET  # noqa: E402


def session_index(days: list[pd.Timestamp]) -> pd.DatetimeIndex:
    stamps = []
    for d in days:
        base = pd.Timestamp(d.date(), tz=ET) + pd.Timedelta(hours=9, minutes=30)
        stamps += [base + pd.Timedelta(minutes=15 * i) for i in range(26)]
    return pd.DatetimeIndex(stamps)


def make_trend_15m(n_days: int = 25, start: str = "2026-01-05", drift: float = 0.0004, seed: int = 1,
                   base: float = 20.0) -> pd.DataFrame:
    """Gently rising, mildly noisy 15m bars on business days."""
    rng = np.random.default_rng(seed)
    days = list(pd.bdate_range(start, periods=n_days))
    idx = session_index(days)
    steps = drift + rng.normal(0, 0.0015, len(idx))
    close = base * np.exp(np.cumsum(steps))
    open_ = np.concatenate([[base], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0008, len(idx))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0008, len(idx))))
    vol = rng.integers(8_000, 12_000, len(idx)).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def make_daily(n: int = 60, end: str = "2026-02-27", base: float = 18.0, drift: float = 0.004) -> pd.DataFrame:
    days = pd.bdate_range(end=end, periods=n)
    close = base * np.exp(np.arange(n) * drift)
    idx = pd.DatetimeIndex([pd.Timestamp(d.date(), tz=ET) for d in days])
    return pd.DataFrame({"open": close * 0.995, "high": close * 1.02, "low": close * 0.98, "close": close,
                         "volume": np.full(n, 2_000_000.0)}, index=idx)


def add_breakout(df: pd.DataFrame, at: pd.Timestamp, bullish: bool = True, vol_mult: float = 3.0) -> pd.DataFrame:
    df = df.copy()
    i = df.index.get_loc(at)
    prior_high = df["high"].iloc[i - 26:i].max()
    o = float(df["close"].iloc[i - 1])
    if bullish:
        c = prior_high * 1.01
        df.iloc[i, df.columns.get_loc("open")] = o
        df.iloc[i, df.columns.get_loc("close")] = c
        df.iloc[i, df.columns.get_loc("high")] = c * 1.001
        df.iloc[i, df.columns.get_loc("low")] = o * 0.999
    else:
        df.iloc[i, df.columns.get_loc("open")] = prior_high * 1.03
        df.iloc[i, df.columns.get_loc("close")] = prior_high * 1.005
        df.iloc[i, df.columns.get_loc("high")] = prior_high * 1.031
        df.iloc[i, df.columns.get_loc("low")] = prior_high * 1.004
    df.iloc[i, df.columns.get_loc("volume")] = float(df["volume"].iloc[i - 20:i].mean() * vol_mult)
    return df


@pytest.fixture
def sp() -> StrategyParams:
    return StrategyParams()


@pytest.fixture
def rp() -> RiskParams:
    return RiskParams(sizing_equity=10_000.0)


@pytest.fixture
def rt(tmp_path) -> RuntimeParams:
    return RuntimeParams(data_dir=tmp_path, fill_timeout_s=5, cancel_settle_s=5, unknown_order_grace_min=10)
