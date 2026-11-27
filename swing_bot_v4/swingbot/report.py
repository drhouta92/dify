"""Trade statistics in R-multiples with uncertainty, shared by live reporting and backtests.

R = realized P&L / initial dollar risk (entry - initial stop) x shares. R normalizes for
position size and volatility, so trades of different names and sizes are comparable.
The decisive statistic is mean R (expectancy); it is reported with a percentile
bootstrap 95% CI. A CI that includes 0 means the sample does not establish an edge.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .indicators import ET


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    initial_stop: float
    pnl: float
    exit_reason: str

    @property
    def risk(self) -> float:
        return (self.entry_price - self.initial_stop) * self.qty

    @property
    def r(self) -> float:
        return self.pnl / self.risk if self.risk > 0 else float("nan")


def entry_bucket(ts: pd.Timestamp) -> str:
    m = ts.tz_convert(ET).hour * 60 + ts.tz_convert(ET).minute
    if 570 <= m < 600:
        return "09:30-10:00"
    if m < 690:
        return "10:00-11:30"
    if m < 840:
        return "11:30-14:00"
    if m < 960:
        return "14:00-16:00"
    return "outside_session"


def bootstrap_mean_ci(values: np.ndarray, n_boot: int = 10_000, alpha: float = 0.05,
                      seed: int = 7) -> tuple[float, float]:
    if len(values) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(n_boot, len(values)))
    means = values[idx].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def summarize(trades: Iterable[TradeRecord], initial_equity: float) -> dict[str, Any]:
    ts = sorted(trades, key=lambda t: t.exit_time)
    n = len(ts)
    if n == 0:
        return {"n": 0}
    r = np.array([t.r for t in ts if math.isfinite(t.r)])
    pnl = np.array([t.pnl for t in ts])
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    equity = initial_equity + np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[initial_equity], equity]))[1:]
    dd = (peak - equity) / peak
    lo, hi = bootstrap_mean_ci(r)
    hold_days = np.array([(t.exit_time - t.entry_time).total_seconds() / 86400 for t in ts])
    out: dict[str, Any] = {
        "n": n,
        "win_rate": float((pnl > 0).mean()),
        "mean_r": float(r.mean()) if len(r) else float("nan"),
        "mean_r_ci95": (lo, hi),
        "median_r": float(np.median(r)) if len(r) else float("nan"),
        "std_r": float(r.std(ddof=1)) if len(r) > 1 else float("nan"),
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("inf"),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "total_pnl": float(pnl.sum()),
        "return_pct": float(pnl.sum() / initial_equity * 100),
        "max_closed_dd_pct": float(dd.max() * 100),
        "avg_hold_days": float(hold_days.mean()),
        "edge_established": bool(lo > 0) if math.isfinite(lo) else False,
    }
    by_reason: dict[str, list[float]] = defaultdict(list)
    by_bucket: dict[str, list[float]] = defaultdict(list)
    for t in ts:
        by_reason[t.exit_reason.split(":")[0]].append(t.r)
        by_bucket[entry_bucket(t.entry_time)].append(t.r)
    out["by_exit_reason"] = {k: {"n": len(v), "mean_r": float(np.nanmean(v))} for k, v in sorted(by_reason.items())}
    out["by_entry_bucket"] = {k: {"n": len(v), "mean_r": float(np.nanmean(v))} for k, v in sorted(by_bucket.items())}
    return out


def format_summary(s: dict[str, Any], title: str) -> str:
    if s.get("n", 0) == 0:
        return f"{title}\n  no completed trades"
    lo, hi = s["mean_r_ci95"]
    lines = [
        title,
        f"  trades {s['n']} | win rate {s['win_rate']:.1%} | profit factor {s['profit_factor']:.2f}",
        f"  expectancy {s['mean_r']:+.3f} R  (95% bootstrap CI {lo:+.3f} .. {hi:+.3f})  median {s['median_r']:+.3f} R",
        f"  edge established at 95%: {'YES' if s['edge_established'] else 'NO'}",
        f"  P&L ${s['total_pnl']:+,.2f} ({s['return_pct']:+.2f}%) | closed-trade max DD {s['max_closed_dd_pct']:.2f}%",
        f"  avg win ${s['avg_win']:,.2f} | avg loss ${s['avg_loss']:,.2f} | avg hold {s['avg_hold_days']:.2f} days",
        "  by exit reason: " + ", ".join(f"{k} n={v['n']} {v['mean_r']:+.2f}R" for k, v in s["by_exit_reason"].items()),
        "  by entry time:  " + ", ".join(f"{k} n={v['n']} {v['mean_r']:+.2f}R" for k, v in s["by_entry_bucket"].items()),
        "  Gross of commissions/fees; paper fills are optimistic vs live (queue position, partial fills).",
    ]
    return "\n".join(lines)
