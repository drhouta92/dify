from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from conftest import add_breakout, make_daily, make_trend_15m

from swingbot.backtest import CostModel, run_backtest
from swingbot.indicators import ET
from swingbot.liquidity import assess
from swingbot.config import UniverseParams
from swingbot.report import TradeRecord, bootstrap_mean_ci, summarize
from swingbot.risk import Candidate, GateInputs, entry_gate, in_cooldown, marketable_limit, position_size, rank
from swingbot.shariah import Compliance, ShariahScreen, classify_html


def test_position_size_respects_all_caps(sp, rp):
    # risk cap: 10k * 1% / (2 * 0.5) = 100 shares; notional cap 10k*25%/20 = 125; cash 1000/20 = 50
    assert position_size(10_000, 1_000, 20.0, 0.5, sp, rp) == 50
    assert position_size(10_000, 1e9, 20.0, 0.5, sp, rp) == 100
    assert position_size(10_000, 1e9, 20.0, 0.1, sp, rp) == 125
    assert position_size(10_000, 1e9, float("nan"), 0.5, sp, rp) == 0


def test_rank_by_cost_in_r_then_participation(sp, rp):
    a = Candidate("AAA", 10.05, 10.0, 0.20, 3.0, 0.002)   # cost 0.05/0.4 = 0.125 R
    b = Candidate("BBB", 10.02, 10.0, 0.20, 2.0, 0.002)   # 0.05 R
    c = Candidate("CCC", 10.02, 10.0, 0.20, 5.0, 0.002)   # 0.05 R, more participation
    d = Candidate("DDD", 10.20, 10.0, 0.20, 9.0, 0.002)   # 0.5 R -> rejected
    assert [x.symbol for x in rank([a, b, c, d], sp, rp)] == ["CCC", "BBB", "AAA"]
    assert marketable_limit(20.0, 0.003) == 20.06


def _gate(**kw):
    base = dict(model_equity=10_000, day_start_equity=10_000, unrealized_pnl=0, realized_today=0,
                equity_peak=10_000, entries_today=0, open_slots_used=0, unresolved_orders=0,
                broker_equity=100_000, daytrade_count=0, trading_blocked=False, drawdown_halt=False)
    base.update(kw)
    return GateInputs(**base)


@pytest.mark.parametrize("kw,reason", [
    ({}, "OK"),
    ({"unresolved_orders": 1}, "UNRESOLVED_ORDERS:1"),
    ({"realized_today": -200, "unrealized_pnl": -150}, "DAILY_LOSS_LIMIT"),
    ({"equity_peak": 12_000}, "DRAWDOWN_LIMIT"),
    ({"entries_today": 1}, "DAILY_TRADE_CAP"),
    ({"open_slots_used": 4}, "POSITION_CAP"),
    ({"broker_equity": 5_000, "daytrade_count": 3}, "PDT_GUARD"),
])
def test_entry_gate(rp, kw, reason):
    assert entry_gate(_gate(**kw), rp)[1] == reason


def test_cooldown_in_sessions(rp):
    assert in_cooldown(date(2026, 3, 6), date(2026, 3, 9), rp)        # Fri -> Mon: 1 session
    assert not in_cooldown(date(2026, 3, 6), date(2026, 3, 10), rp)   # Fri -> Tue: 2 sessions


def test_amihud_uses_close_to_close_and_feed_share():
    up = UniverseParams()
    d = make_daily(40)
    sip = assess("X", d, up, 1.0)
    iex = assess("X", d.assign(volume=d["volume"] * 0.025), up, 0.025)
    assert sip.passed and iex.passed
    assert sip.metrics["amihud"] == pytest.approx(iex.metrics["amihud"])


def test_bootstrap_ci_and_summary():
    t0 = pd.Timestamp("2026-01-05 10:30", tz=ET)
    trades = [TradeRecord("X", t0, t0 + pd.Timedelta(days=1), 10, 10 + r, 100, 9, r * 100, "EXIT_STOP")
              for r in [1.0, -1.0, 2.0, -1.0, 1.5, -0.5]]
    s = summarize(trades, 10_000)
    assert s["n"] == 6 and s["mean_r"] == pytest.approx(np.mean([1, -1, 2, -1, 1.5, -0.5]))
    lo, hi = s["mean_r_ci95"]
    assert lo < s["mean_r"] < hi and not s["edge_established"]
    lo2, hi2 = bootstrap_mean_ci(np.full(50, 0.3))
    assert lo2 == pytest.approx(0.3) and hi2 == pytest.approx(0.3)


def test_shariah_html_classification_and_overrides(tmp_path):
    ok = '<div class="px-3 bg-[#ECFEE7] rounded">\n<span>شرعي</span></div>'
    bad = '<span class="bg-red-50 text-xs">غير شرعي</span><div class="p-2 bg-red-50">غير شرعي</div>'
    doubt = '<div class="bg-[#FEF1C7]"> محل نظر </div>'
    assert classify_html(ok) is Compliance.COMPLIANT
    assert classify_html(bad) is Compliance.NONCOMPLIANT
    assert classify_html(doubt) is Compliance.DOUBTFUL and Compliance.DOUBTFUL.forces_exit
    assert classify_html("<html></html>") is Compliance.UNKNOWN
    (tmp_path / "o.json").write_text('{"allow": ["aaa"], "deny": ["BBB"]}')
    s = ShariahScreen(tmp_path / "c.json", tmp_path / "o.json")
    s._fetch = lambda sym: Compliance.UNKNOWN                     # no network in tests
    assert s.status("AAA") is Compliance.COMPLIANT
    assert s.status("BBB") is Compliance.NONCOMPLIANT
    assert s.status("CCC") is Compliance.UNKNOWN
    assert "CCC" not in s._cache                                  # unknown is never cached


def _bt_data():
    df = make_trend_15m(30, start="2026-01-05", drift=0.0006, seed=3)
    at = df.index[-26 * 3 - 14]
    df = add_breakout(df, at)
    daily = make_daily(150, end=str(df.index[-1].date()), base=12.0)
    return df, daily, at


def test_backtest_runs_and_trades(sp, rp):
    df, daily, at = _bt_data()
    res = run_backtest({"AAA": df}, {"AAA": daily}, sp, rp, date(2026, 2, 2), df.index[-1].date())
    assert res.signals >= 1
    assert res.summary["n"] >= 1
    cost = CostModel()
    for t in res.trades:                                          # fills happen after the signal bar closed
        boundary = t.entry_time - pd.Timedelta(seconds=20)
        assert boundary == boundary.floor("15min")
        signal_bar = df.loc[boundary - pd.Timedelta(minutes=15)]
        assert t.entry_price == pytest.approx(signal_bar["close"] * (1 + cost.half_spread_pct + cost.slip_pct))
        assert t.exit_time > t.entry_time


def test_backtest_has_no_lookahead(sp, rp):
    """Rewriting the future must not change any trade that closed before the rewrite point."""
    df, daily, at = _bt_data()
    base = run_backtest({"AAA": df}, {"AAA": daily}, sp, rp, date(2026, 2, 2), df.index[-1].date())
    cut = df.index[-26]
    future = df.copy()
    future.loc[future.index >= cut, ["open", "high", "low", "close"]] *= 0.7
    alt = run_backtest({"AAA": future}, {"AAA": daily}, sp, rp, date(2026, 2, 2), df.index[-1].date())
    early = [t for t in base.trades if t.exit_time <= cut]
    assert early == [t for t in alt.trades if t.exit_time <= cut]


def test_backtest_costs_reduce_pnl(sp, rp):
    df, daily, _ = _bt_data()
    free = run_backtest({"AAA": df}, {"AAA": daily}, sp, rp, date(2026, 2, 2), df.index[-1].date(),
                        cost=CostModel(0.0, 0.0))
    costly = run_backtest({"AAA": df}, {"AAA": daily}, sp, rp, date(2026, 2, 2), df.index[-1].date(),
                          cost=CostModel(0.003, 0.002))
    assert costly.summary["total_pnl"] < free.summary["total_pnl"]


def test_tradingview_normalization_schema():
    from swingbot.universe import normalize_tradingview
    raw = pd.DataFrame({
        "symbol": ["NASDAQ:AAA", "NYSE:BRK.B", "NYSE:bad name", "AMEX:CCC"],
        "name": ["AAA", "BRK.B", "x", "CCC"],
        "close": [10.0, 30.0, 5.0, None],
        "average_volume_30d_calc": [1e6, 2e6, 1e6, 1e6],
        "relative_volume_10d_calc": [2.0, 1.5, 1.0, 1.0],
        "earnings_release_next_date": [1772668800.0, float("nan"), 0, 1772668800.0],
        "update_mode": ["streaming"] * 4,
    })
    out = normalize_tradingview(raw)
    assert list(out["symbol"]) == ["AAA", "BRK.B"]
    assert out.loc[0, "earnings_date"] == date(2026, 3, 5)
    assert out.loc[1, "earnings_date"] is None
