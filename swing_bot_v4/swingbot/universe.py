"""Daily universe: TradingView discovery -> earnings blackout -> Shariah -> liquidity -> tradability.

Built once per session BEFORE the open (v3 built it at 09:45 inside the first cycle,
delaying that cycle by the time of ~100 web scrapes and ranking on a 15-minute-old
relative-volume reading). Only information available before the open is used.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from .broker import Broker, BrokerError, MarketData
from .config import RuntimeParams, StrategyParams, UniverseParams
from .indicators import ET
from .liquidity import LiquidityResult, assess
from .shariah import Compliance, ShariahScreen

log = logging.getLogger("universe")
ET_TZ = ZoneInfo(ET)
VALID_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def fetch_tradingview(up: UniverseParams) -> pd.DataFrame:
    """Columns: symbol, close, avg_volume_30d, relative_volume, earnings_date (date | None)."""
    from tvscreener import FilterOperator as Op, Market, StockField as F, StockScreener, SymbolType

    ss = StockScreener()
    ss.set_markets(Market.AMERICA)
    ss.set_symbol_types(SymbolType.COMMON_STOCK)
    ss.add_filter(F.EXCHANGE, Op.IN_RANGE, ["NASDAQ", "NYSE", "AMEX"])
    ss.add_filter(F.PRICE, Op.IN_RANGE, [up.price_min, up.price_max])
    ss.add_filter(F.AVERAGE_VOLUME_30_DAY, Op.ABOVE, up.min_avg_volume_30d)
    ss.sort_by(F.RELATIVE_VOLUME, ascending=False)
    ss.set_range(0, up.discovery_count)
    ss.select(F.NAME, F.PRICE, F.AVERAGE_VOLUME_30_DAY, F.RELATIVE_VOLUME, F.UPCOMING_EARNINGS_DATE)
    df = ss.get()
    df.set_technical_columns(only=True)
    return normalize_tradingview(pd.DataFrame(df))


def normalize_tradingview(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol", "close", "avg_volume_30d", "relative_volume", "earnings_date"])
    out = pd.DataFrame({
        "symbol": df["symbol"].astype(str).str.split(":").str[-1].str.upper().str.strip(),
        "close": pd.to_numeric(df.get("close"), errors="coerce"),
        "avg_volume_30d": pd.to_numeric(df.get("average_volume_30d_calc"), errors="coerce"),
        "relative_volume": pd.to_numeric(df.get("relative_volume_10d_calc"), errors="coerce"),
    })
    # TradingView epoch seconds. Date taken in UTC: correct for date-only values stamped at
    # 00:00 UTC and for pre-market/after-close release times (12:00-22:00 UTC).
    raw_earn = pd.to_numeric(df.get("earnings_release_next_date"), errors="coerce")
    out["earnings_date"] = [
        pd.Timestamp(v, unit="s", tz="UTC").date() if pd.notna(v) and v > 0 else None
        for v in raw_earn
    ]
    out = out[out["symbol"].str.match(VALID_TICKER) & out["close"].notna()]
    return out.drop_duplicates("symbol").reset_index(drop=True)


@dataclass
class Universe:
    session_date: date
    members: list[str]
    earnings: dict[str, date | None]
    liquidity: dict[str, LiquidityResult]
    compliance: dict[str, Compliance]
    daily: dict[str, pd.DataFrame]
    rejected: dict[str, str] = field(default_factory=dict)
    built_at: str = ""
    daily_feed_used: str = "sip"

    def summary(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for r in self.rejected.values():
            key = r.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        return {"session_date": self.session_date.isoformat(), "members": len(self.members),
                "rejected": reasons, "daily_feed": self.daily_feed_used, "built_at": self.built_at}


class UniverseBuilder:
    def __init__(self, md: MarketData, broker: Broker, shariah: ShariahScreen, up: UniverseParams,
                 sp: StrategyParams, rt: RuntimeParams,
                 screener: Callable[[UniverseParams], pd.DataFrame] = fetch_tradingview):
        self.md, self.broker, self.shariah = md, broker, shariah
        self.up, self.sp, self.rt, self.screener = up, sp, rt, screener

    def _daily(self, symbols: list[str], session_date: date) -> tuple[dict[str, pd.DataFrame], str, float]:
        end = datetime.combine(session_date, time(0, 0), tzinfo=ET_TZ)
        start = end - timedelta(days=self.rt.history_days_1d)
        feed = self.rt.daily_feed
        try:
            return self.md.bars(symbols, "1d", start, end, feed), feed, 1.0 if feed == "sip" else self.up.iex_volume_share
        except BrokerError as exc:
            if feed == "iex":
                raise
            log.error("SIP daily bars unavailable (%s); falling back to IEX with volume share %.3f",
                      exc, self.up.iex_volume_share)
            return self.md.bars(symbols, "1d", start, end, "iex"), "iex", self.up.iex_volume_share

    def build(self, session_date: date, held: list[str]) -> Universe:
        rejected: dict[str, str] = {}
        raw = self.screener(self.up)
        log.info("discovery returned %d symbols", len(raw))
        earnings = {r.symbol: r.earnings_date for r in raw.itertuples()}
        candidates: list[str] = []
        for r in raw.itertuples():
            e = r.earnings_date
            if e is not None and 0 <= (e - session_date).days <= self.up.earnings_blackout_days:
                rejected[r.symbol] = f"EARNINGS:{e}"
            else:
                candidates.append(r.symbol)
        compliance = self.shariah.screen(sorted(set(candidates) | set(held)))
        compliant = [s for s in candidates if compliance.get(s) is Compliance.COMPLIANT]
        for s in candidates:
            if s not in compliant:
                rejected[s] = f"SHARIAH:{compliance.get(s, Compliance.UNKNOWN).value}"
        daily, feed_used, share = self._daily(sorted(set(compliant) | set(held)), session_date) if (compliant or held) \
            else ({}, self.rt.daily_feed, 1.0)
        liquidity: dict[str, LiquidityResult] = {}
        passed: list[str] = []
        for s in compliant:
            res = assess(s, daily.get(s), self.up, share)
            liquidity[s] = res
            if res.passed:
                passed.append(s)
            else:
                rejected[s] = f"LIQUIDITY:{res.reason}"
        members: list[str] = []
        for s in passed:                       # discovery order = relative volume, descending
            if len(members) >= self.up.max_universe:
                rejected[s] = "UNIVERSE_CAP"
                continue
            try:
                ok = self.broker.is_tradable(s)
            except BrokerError as exc:
                log.warning("tradability check %s failed: %s", s, exc)
                ok = False
            if ok:
                members.append(s)
            else:
                rejected[s] = "NOT_TRADABLE"
        uni = Universe(session_date, members, earnings, liquidity, compliance, daily, rejected,
                       pd.Timestamp.now(tz=ET).isoformat(), feed_used)
        log.info("universe %s", uni.summary())
        return uni

