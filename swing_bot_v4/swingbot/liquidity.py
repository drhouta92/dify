"""Liquidity screen on COMPLETED daily bars.

Amihud (2002) illiquidity: ILLIQ = mean_t( |r_t| / DollarVolume_t ), r_t close-to-close.
v3 used the intraday open-to-close return and IEX-only volume scaled by a constant 3%;
both bias the measure. v4 uses close-to-close returns on consolidated (SIP) daily bars,
which Alpaca serves free of charge once they are >15 minutes old. The IEX scaling path is
kept only as a declared fallback when SIP is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config import UniverseParams


@dataclass(frozen=True)
class LiquidityResult:
    symbol: str
    passed: bool
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)


def assess(symbol: str, daily: pd.DataFrame | None, up: UniverseParams, volume_share: float = 1.0) -> LiquidityResult:
    """``volume_share`` = fraction of consolidated volume the feed reports (1.0 for SIP)."""
    window = up.liquidity_window
    if daily is None or len(daily) < window + 1:
        return LiquidityResult(symbol, False, "INSUFFICIENT_DATA")
    d = daily.tail(window + 1).copy()
    d["ret"] = d["close"].pct_change()
    d["dvol"] = d["close"] * d["volume"] / volume_share
    d = d.iloc[1:]
    median_dvol = float(d["dvol"].median())
    metrics: dict[str, Any] = {"median_dvol": median_dvol}
    if not np.isfinite(median_dvol) or median_dvol < up.min_median_dollar_volume:
        return LiquidityResult(symbol, False, "LOW_DOLLAR_VOLUME", metrics)
    active = d[d["dvol"] > 0]
    if len(active) < up.min_active_days:
        return LiquidityResult(symbol, False, "INACTIVE", metrics)
    illiq = active["ret"].abs() / active["dvol"] * 1e6
    amihud = float(illiq.mean())
    metrics["amihud"] = amihud
    if not np.isfinite(amihud) or amihud > up.max_amihud:
        return LiquidityResult(symbol, False, "HIGH_IMPACT", metrics)
    up_days, down_days = illiq[active["ret"] > 0], illiq[active["ret"] < 0]
    if len(up_days) > 3 and len(down_days) > 3 and up_days.mean() > 0:
        ratio = float(down_days.mean() / up_days.mean())
        metrics["sell_trap_ratio"] = ratio
        if ratio > up.max_sell_trap_ratio:
            return LiquidityResult(symbol, False, "SELL_TRAP", metrics)
    return LiquidityResult(symbol, True, "PASS", metrics)
