"""Event-driven backtest that calls the SAME strategy and risk functions as the live trader.

Timing model (mirrors the live scheduler exactly):
* A decision happens at each 15m boundary B in [09:45, 15:45] for the bar [B-15m, B).
  The bar starting 15:45 is never a decision bar (the live loop sees the market closed at 16:00).
* Market/marketable orders fill at the decision bar's close adjusted by costs (the live
  order is sent ~20 s after the close).
* Resting protective orders are simulated intrabar on subsequent bars: a stop fills at
  min(open, stop) (gap-through aware); a target fills at max(open, target). If one bar
  touches both, the stop is assumed first (conservative).
* The 15m history window passed to the strategy is the same calendar window the live
  trader fetches (runtime.history_days_15m), so indicator seeding is identical.

Known biases (reported, not hidden): the symbol list is chosen today (survivorship and
selection bias); Shariah status and earnings dates are not point-in-time; fills ignore
queue position and partial fills. Treat results as an upper bound on live performance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .config import RiskParams, StrategyParams
from .indicators import BAR_15M, ET
from .report import TradeRecord, summarize
from .risk import Candidate, GateInputs, entry_gate, in_cooldown, position_size, rank
from .strategy import (PositionView, daily_context, evaluate_entry, hwm_start_for_fill, initial_levels,
                       manage_position)

log = logging.getLogger("backtest")
LAST_DECISION_BAR = pd.Timedelta(hours=15, minutes=30)
FIRST_DECISION_BAR = pd.Timedelta(hours=9, minutes=30)


@dataclass(frozen=True)
class CostModel:
    half_spread_pct: float = 0.0015
    slip_pct: float = 0.0005


@dataclass
class _Open:
    symbol: str
    qty: int
    view: PositionView
    entry_time: pd.Timestamp


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    equity: pd.Series
    summary: dict[str, Any]
    signals: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{**t.__dict__, "r": t.r} for t in self.trades])


def _fresh_breakout_mask(df: pd.DataFrame, sp: StrategyParams) -> pd.Series:
    """Vectorized necessary condition of evaluate_entry (identical rolling definition)."""
    prior = df["high"].shift(1).rolling(sp.breakout_lookback).max()
    brk = df["close"] > prior
    return brk & ~brk.shift(1, fill_value=False)


def run_backtest(bars15: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame], sp: StrategyParams,
                 rp: RiskParams, start: date, end: date, history_days: int = 35,
                 cost: CostModel = CostModel(), initial_equity: float | None = None) -> BacktestResult:
    equity0 = float(initial_equity or rp.sizing_equity or 100_000.0)
    hist = pd.Timedelta(days=history_days)
    masks = {s: _fresh_breakout_mask(df, sp) for s, df in bars15.items()}
    timeline = sorted({t for df in bars15.values() for t in df.index if start <= t.date() <= end})
    open_pos: dict[str, _Open] = {}
    trades: list[TradeRecord] = []
    last_exit: dict[str, date] = {}
    entries_by_day: dict[date, int] = {}
    realized = 0.0
    realized_by_day: dict[date, float] = {}
    peak = equity0
    ctx_cache: dict[tuple[str, date], Any] = {}
    last_close: dict[str, float] = {}
    eq_points: dict[date, float] = {}
    n_signals = 0
    skipped: dict[str, int] = {}

    def window(sym: str, asof: pd.Timestamp) -> pd.DataFrame:
        df = bars15[sym]
        lo = df.index.searchsorted(asof - hist, side="left")
        hi = df.index.searchsorted(asof - BAR_15M, side="right")
        return df.iloc[lo:hi]

    def close_trade(o: _Open, price: float, when: pd.Timestamp, reason: str) -> None:
        nonlocal realized
        pnl = (price - o.view.entry_price) * o.qty
        realized += pnl
        realized_by_day[when.date()] = realized_by_day.get(when.date(), 0.0) + pnl
        trades.append(TradeRecord(o.symbol, o.entry_time, when, o.view.entry_price, price, o.qty,
                                  o.view.initial_stop, pnl, reason))
        last_exit[o.symbol] = when.date()
        del open_pos[o.symbol]

    for t in timeline:
        session = t.date()
        # ---- 1. resting orders on bar t
        for sym in list(open_pos):
            df = bars15[sym]
            if t not in df.index:
                continue
            o, bar = open_pos[sym], df.loc[t]
            last_close[sym] = float(bar["close"])
            if t < o.view.hwm_from:
                continue                      # protection is placed after the fill bar
            if bar["low"] <= o.view.stop:
                px = min(float(bar["open"]), o.view.stop) * (1 - cost.half_spread_pct - cost.slip_pct)
                close_trade(o, px, t + BAR_15M, "EXIT_STOP")
            elif o.view.target is not None and bar["high"] >= o.view.target:
                close_trade(o, max(float(bar["open"]), o.view.target), t + BAR_15M, "EXIT_TARGET")
        for sym, df in bars15.items():
            if sym not in open_pos and t in df.index:
                last_close[sym] = float(df.at[t, "close"])

        offset = t - t.normalize()
        if not FIRST_DECISION_BAR <= offset <= LAST_DECISION_BAR:
            continue
        asof = t + BAR_15M

        # ---- 2. manage open positions at the bar close
        for sym in list(open_pos):
            o = open_pos[sym]
            w = window(sym, asof)
            if w.empty or w.index[-1] != t:
                continue
            d = manage_position(o.view, w, t, asof, sp)
            if d.action != "HOLD":
                px = float(w["close"].iloc[-1]) * (1 - cost.half_spread_pct - cost.slip_pct)
                close_trade(o, px, asof, d.action)
            else:
                o.view.stop, o.view.hwm, o.view.be_locked = d.stop, d.hwm, d.be_locked

        # ---- 3. entries
        cands: list[Candidate] = []
        for sym, df in bars15.items():
            if sym in open_pos or t not in df.index or not bool(masks[sym].get(t, False)):
                continue
            if in_cooldown(last_exit.get(sym), session, rp):
                skipped["COOLDOWN"] = skipped.get("COOLDOWN", 0) + 1
                continue
            key = (sym, session)
            if key not in ctx_cache:
                ctx_cache[key] = daily_context(daily.get(sym), session, sp)
            dec = evaluate_entry(window(sym, asof), ctx_cache[key], t, asof, sp)
            if not dec.enter:
                continue
            n_signals += 1
            px = float(dec.features["price"])
            mid = px
            limit = round(px * (1 + cost.half_spread_pct) * (1 + rp.entry_slip_pct), 2)
            cands.append(Candidate(sym, limit, mid, dec.features["atr_d"], dec.features["relative_volume"],
                                   2 * cost.half_spread_pct, dec.features))
        for c in rank(cands, sp, rp):
            unreal = sum((last_close.get(s, o.view.entry_price) - o.view.entry_price) * o.qty
                         for s, o in open_pos.items())
            model = equity0 + realized
            peak = max(peak, model + unreal)
            g = GateInputs(model_equity=model, day_start_equity=model - realized_by_day.get(session, 0.0),
                           unrealized_pnl=unreal, realized_today=realized_by_day.get(session, 0.0),
                           equity_peak=peak, entries_today=entries_by_day.get(session, 0),
                           open_slots_used=len(open_pos), unresolved_orders=0, broker_equity=float("inf"),
                           daytrade_count=0, trading_blocked=False, drawdown_halt=False)
            ok, why = entry_gate(g, rp)
            if not ok:
                skipped[why] = skipped.get(why, 0) + 1
                break
            fill = c.mid * (1 + cost.half_spread_pct + cost.slip_pct)
            used = sum(o.view.entry_price * o.qty for o in open_pos.values())
            qty = position_size(model, model - used, fill, c.atr_d, sp, rp)
            if qty <= 0:
                skipped["SIZE_ZERO"] = skipped.get("SIZE_ZERO", 0) + 1
                continue
            stop, target = initial_levels(fill, c.atr_d, sp)
            fill_time = asof + pd.Timedelta(seconds=20)
            view = PositionView(fill, fill_time, hwm_start_for_fill(fill_time), c.atr_d, stop, stop, fill, False,
                                target)
            open_pos[c.symbol] = _Open(c.symbol, qty, view, fill_time)
            entries_by_day[session] = entries_by_day.get(session, 0) + 1

        unreal = sum((last_close.get(s, o.view.entry_price) - o.view.entry_price) * o.qty for s, o in open_pos.items())
        eq_points[session] = equity0 + realized + unreal

    for sym in list(open_pos):                  # end of data: mark out at last close
        o = open_pos[sym]
        px = last_close.get(sym, o.view.entry_price) * (1 - cost.half_spread_pct - cost.slip_pct)
        close_trade(o, px, pd.Timestamp(end, tz=ET) + pd.Timedelta(hours=16), "END_OF_DATA")

    equity = pd.Series(eq_points, dtype=float).sort_index()
    summary = summarize(trades, equity0)
    if len(equity):
        peak_curve = equity.cummax()
        summary["max_mtm_dd_pct"] = float(((peak_curve - equity) / peak_curve).max() * 100)
        daily_ret = equity.pct_change().dropna()
        summary["daily_sharpe_annualized"] = (float(daily_ret.mean() / daily_ret.std(ddof=1) * np.sqrt(252))
                                              if len(daily_ret) > 1 and daily_ret.std(ddof=1) > 0 else float("nan"))
        summary["exposure_days"] = int(len(equity))
    summary["signals"] = n_signals
    return BacktestResult(trades, equity, summary, n_signals, skipped)
