"""Position sizing, candidate ranking and portfolio-level entry gates. Pure functions.

Ranking rationale (replaces v3's 40/25/20/-10/-5 weighted score, which had no
empirical calibration): every candidate already passed identical boolean entry
rules, so the only quantity that changes expected R per trade *by construction*
is execution cost. Round-trip entry cost measured in R units,

    cost_R = (limit - mid) / (stop_atr_mult * ATR_d),

is subtracted 1:1 from the trade's R-multiple. Candidates are ranked by lowest
cost_R, then by breakout participation (relative volume), then by symbol for
determinism. Candidates with cost_R > max_cost_R are rejected.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from .config import RiskParams, StrategyParams

PDT_EQUITY_THRESHOLD = 25_000.0


@dataclass(frozen=True)
class Candidate:
    symbol: str
    limit: float            # worst acceptable entry price (marketable limit)
    mid: float
    atr_d: float
    relative_volume: float
    spread_pct: float
    features: dict[str, Any] = field(default_factory=dict, compare=False)

    def cost_r(self, stop_atr_mult: float) -> float:
        return (self.limit - self.mid) / (stop_atr_mult * self.atr_d)


def marketable_limit(ask: float, slip_pct: float) -> float:
    return round(math.floor(ask * (1 + slip_pct) * 100 + 1e-9) / 100, 2)


def position_size(equity: float, cash: float, price: float, atr_d: float,
                  sp: StrategyParams, rp: RiskParams) -> int:
    """Shares such that a stop fill at its trigger loses <= risk_per_trade * equity."""
    if not all(math.isfinite(x) for x in (equity, cash, price, atr_d)) or min(equity, price, atr_d) <= 0:
        return 0
    by_risk = equity * rp.risk_per_trade / (sp.stop_atr_mult * atr_d)
    by_cap = equity * rp.max_position_pct / price
    by_cash = max(0.0, cash) / price
    return max(0, math.floor(min(by_risk, by_cap, by_cash) + 1e-9))


def rank(candidates: list[Candidate], sp: StrategyParams, rp: RiskParams) -> list[Candidate]:
    ok = [c for c in candidates
          if c.atr_d > 0 and 0 <= c.spread_pct <= rp.max_spread_pct and c.cost_r(sp.stop_atr_mult) <= rp.max_cost_r]
    return sorted(ok, key=lambda c: (round(c.cost_r(sp.stop_atr_mult), 6), -c.relative_volume, c.symbol))


def in_cooldown(last_exit_session: date | None, today: date, rp: RiskParams) -> bool:
    if last_exit_session is None or rp.cooldown_sessions == 0:
        return False
    return int(np.busday_count(last_exit_session, today)) < rp.cooldown_sessions


@dataclass(frozen=True)
class GateInputs:
    model_equity: float          # sizing equity incl. realized P&L
    day_start_equity: float
    unrealized_pnl: float
    realized_today: float
    equity_peak: float
    entries_today: int
    open_slots_used: int         # bot positions + unmanaged broker holdings
    unresolved_orders: int
    broker_equity: float
    daytrade_count: int
    trading_blocked: bool
    drawdown_halt: bool


def entry_gate(g: GateInputs, rp: RiskParams) -> tuple[bool, str]:
    """Return (allowed, reason). Any failure blocks NEW entries only; exits are never gated."""
    if g.trading_blocked:
        return False, "ACCOUNT_BLOCKED"
    if g.unresolved_orders:
        return False, f"UNRESOLVED_ORDERS:{g.unresolved_orders}"
    if g.drawdown_halt:
        return False, "DRAWDOWN_HALT"
    current = g.model_equity + g.unrealized_pnl
    if g.equity_peak > 0 and (g.equity_peak - current) / g.equity_peak >= rp.max_drawdown_pct:
        return False, "DRAWDOWN_LIMIT"
    if g.day_start_equity > 0 and -(g.realized_today + g.unrealized_pnl) >= rp.daily_loss_limit_pct * g.day_start_equity:
        return False, "DAILY_LOSS_LIMIT"
    if g.entries_today >= rp.max_trades_per_day:
        return False, "DAILY_TRADE_CAP"
    if g.open_slots_used >= rp.max_open_positions:
        return False, "POSITION_CAP"
    if rp.pdt_guard and g.broker_equity < PDT_EQUITY_THRESHOLD and g.daytrade_count >= 3:
        return False, "PDT_GUARD"
    return True, "OK"
