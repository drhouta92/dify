"""Typed, validated configuration.

Every field can be overridden by an environment variable named ``SWING_<FIELD>``
(for example ``SWING_RISK_PER_TRADE=0.005``). Values are parsed with the field's
declared type and validated once at start-up; an invalid value aborts start-up
instead of silently falling back to a default.
"""
from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Mapping

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
PLACEHOLDER_KEYS = frozenset({"", "KEYS_HERE", "PASTE_PAPER_KEY_HERE", "PASTE_PAPER_SECRET_HERE"})


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


def _parse_time(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class StrategyParams:
    """Pure strategy parameters. Shared verbatim by the live trader and the backtester."""

    # Entry: 15m breakout inside 1h and daily uptrends
    breakout_lookback: int = 26            # 15m bars (~1 regular session)
    vol_avg_lookback: int = 20             # 15m bars for the volume baseline
    min_relative_volume: float = 1.0       # breakout bar volume / baseline, strictly greater
    body_ratio_min: float = 0.40           # (close - open) / range, bullish bodies only
    vwap_lookback: int = 130               # 15m bars (~5 sessions) for rolling VWAP
    ema_fast_1h: int = 9
    ema_slow_1h: int = 21
    ema_15m: int = 21
    daily_ema: int = 20
    atr_len: int = 14
    max_atr_pct: float = 0.15              # skip names with daily ATR > 15% of price
    first_entry_bar: time = time(9, 45)    # earliest 15m bar START eligible for entry
    last_entry_bar: time = time(15, 15)    # latest 15m bar START eligible for entry
    min_15m_bars: int = 60
    min_1h_bars: int = 25
    min_daily_bars: int = 25

    # Exit
    stop_atr_mult: float = 2.0             # initial stop = fill - k * ATR(daily)
    be_trigger_atr: float = 1.0            # high-water mark >= entry + x * ATR(daily) -> breakeven lock
    be_offset: float = 0.01                # stop >= entry + offset after the lock
    trail_atr_mult_1h: float = 3.0         # after the lock: stop >= HWM - m * ATR(1h)
    take_profit_r: float = 3.0             # resting take-profit at entry + R * initial risk; 0 disables
    max_hold_sessions: int = 5             # entry session counts as session 1
    time_exit_bar: time = time(15, 30)     # bar START on which time/event exits are decided
    trend_exit: bool = True                # exit on 1h close < EMA slow and EMA fast < EMA slow
    earnings_exit_business_days: int = 1   # exit before earnings within this many business days

    def __post_init__(self) -> None:
        positive_ints = ("breakout_lookback", "vol_avg_lookback", "vwap_lookback", "ema_fast_1h",
                         "ema_slow_1h", "ema_15m", "daily_ema", "atr_len", "max_hold_sessions",
                         "min_15m_bars", "min_1h_bars", "min_daily_bars")
        for name in positive_ints:
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be > 0")
        if self.ema_fast_1h >= self.ema_slow_1h:
            raise ConfigError("ema_fast_1h must be < ema_slow_1h")
        if not 0 <= self.body_ratio_min <= 1:
            raise ConfigError("body_ratio_min must be within [0, 1]")
        for name in ("max_atr_pct", "stop_atr_mult", "trail_atr_mult_1h"):
            if not getattr(self, name) > 0:
                raise ConfigError(f"{name} must be > 0")
        if self.be_trigger_atr < 0 or self.take_profit_r < 0 or self.be_offset < 0:
            raise ConfigError("be_trigger_atr, be_offset and take_profit_r must be >= 0")
        if self.first_entry_bar > self.last_entry_bar:
            raise ConfigError("first_entry_bar must be <= last_entry_bar")
        if self.min_15m_bars < self.breakout_lookback + 2:
            raise ConfigError("min_15m_bars must exceed breakout_lookback + 1")


@dataclass(frozen=True)
class RiskParams:
    sizing_equity: float = 1000.0          # model equity; 0 = live broker equity
    risk_per_trade: float = 0.01           # fraction of sizing equity lost if the stop fills at its price
    max_position_pct: float = 0.25         # notional cap per position, fraction of sizing equity
    max_open_positions: int = 4
    max_trades_per_day: int = 1
    max_spread_pct: float = 0.015          # (ask - bid) / mid
    entry_slip_pct: float = 0.003          # marketable limit = ask * (1 + slip)
    max_cost_r: float = 0.15               # max (half-spread + slip) as a fraction of 1R
    cooldown_sessions: int = 2             # sessions after an exit before the symbol can re-enter
    daily_loss_limit_pct: float = 0.03     # realized + unrealized loss today halts new entries
    max_drawdown_pct: float = 0.15         # peak-to-trough on sizing equity halts entries until reset
    pdt_guard: bool = True                 # block entries when equity < $25k and 3 day trades used

    def __post_init__(self) -> None:
        if self.sizing_equity < 0 or not math.isfinite(self.sizing_equity):
            raise ConfigError("sizing_equity must be finite and >= 0")
        for name in ("risk_per_trade", "max_position_pct", "max_spread_pct", "daily_loss_limit_pct",
                     "max_drawdown_pct", "max_cost_r"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ConfigError(f"{name} must be within (0, 1]")
        if not 0 <= self.entry_slip_pct < 0.05:
            raise ConfigError("entry_slip_pct must be within [0, 0.05)")
        if self.max_open_positions < 0 or self.max_trades_per_day < 0 or self.cooldown_sessions < 0:
            raise ConfigError("position/trade caps and cooldown must be >= 0")


@dataclass(frozen=True)
class UniverseParams:
    price_min: float = 3.0
    price_max: float = 50.0
    min_avg_volume_30d: int = 500_000      # consolidated shares/day from TradingView
    discovery_count: int = 150
    max_universe: int = 60                 # symbols monitored intraday after all filters
    earnings_blackout_days: int = 8        # calendar days; > max hold window incl. weekends
    min_median_dollar_volume: float = 2_000_000.0   # consolidated (SIP) median $ volume, 20 sessions
    iex_volume_share: float = 0.025        # used ONLY if SIP daily bars are unavailable
    max_amihud: float = 0.10               # mean(|r_cc| / $vol) * 1e6, consolidated
    max_sell_trap_ratio: float = 1.5       # down-day illiquidity / up-day illiquidity
    min_active_days: int = 15
    liquidity_window: int = 20

    def __post_init__(self) -> None:
        if not 0 < self.price_min < self.price_max:
            raise ConfigError("0 < price_min < price_max required")
        if not 0 < self.iex_volume_share <= 1:
            raise ConfigError("iex_volume_share must be within (0, 1]")
        if self.max_universe <= 0 or self.discovery_count <= 0:
            raise ConfigError("max_universe and discovery_count must be > 0")


@dataclass(frozen=True)
class RuntimeParams:
    api_key: str = ""
    secret_key: str = ""
    base_url: str = PAPER_ENDPOINT
    intraday_feed: str = "iex"             # real-time 15m bars; 'sip' requires a paid plan
    daily_feed: str = "sip"                # completed daily bars; SIP history is free if >15 min old
    data_dir: Path = Path("./var")
    cycle_delay_s: int = 20                # seconds after the 15m boundary before the cycle runs
    universe_build_lead_min: int = 40      # build the universe this long before the open
    history_days_15m: int = 35
    history_days_1d: int = 150
    fill_timeout_s: int = 45
    cancel_settle_s: int = 15
    protect_retries: int = 3
    unknown_order_grace_min: int = 10      # an unfound client_order_id is declared absent after this
    heartbeat_warn_min: int = 30
    shariah_cache_days: int = 7
    shariah_concurrency: int = 4
    shariah_user_agent: str = "swingbot/4.0 (+compliance lookup; contact operator)"
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.base_url.rstrip("/") != PAPER_ENDPOINT:
            raise ConfigError("Only the Alpaca PAPER endpoint is allowed")
        if self.intraday_feed not in ("iex", "sip") or self.daily_feed not in ("iex", "sip"):
            raise ConfigError("feeds must be 'iex' or 'sip'")
        if self.cycle_delay_s < 5 or self.fill_timeout_s < 5:
            raise ConfigError("cycle_delay_s and fill_timeout_s must be >= 5")

    def require_credentials(self) -> None:
        if self.api_key in PLACEHOLDER_KEYS or self.secret_key in PLACEHOLDER_KEYS:
            raise ConfigError("Set ALPACA_API_KEY / ALPACA_SECRET_KEY (paper keys); placeholders refused")


@dataclass(frozen=True)
class Settings:
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskParams = field(default_factory=RiskParams)
    universe: UniverseParams = field(default_factory=UniverseParams)
    runtime: RuntimeParams = field(default_factory=RuntimeParams)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        runtime_overrides: dict[str, Any] = {
            "api_key": env.get("ALPACA_API_KEY", ""),
            "secret_key": env.get("ALPACA_SECRET_KEY", ""),
            "base_url": env.get("APCA_API_BASE_URL", PAPER_ENDPOINT),
        }
        return cls(
            strategy=_build(StrategyParams, env),
            risk=_build(RiskParams, env),
            universe=_build(UniverseParams, env),
            runtime=_build(RuntimeParams, env, runtime_overrides),
        )


def _coerce(raw: str, target: Any, name: str) -> Any:
    try:
        if target is bool:
            lowered = raw.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            raise ValueError(raw)
        if target is int:
            return int(raw)
        if target is float:
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(raw)
            return value
        if target is time:
            return _parse_time(raw)
        if target is Path:
            return Path(raw)
        return raw.strip()
    except ValueError as exc:
        raise ConfigError(f"SWING_{name.upper()}={raw!r} is not a valid {target.__name__}") from exc


def _build(cls: type, env: Mapping[str, str], extra: Mapping[str, Any] | None = None):
    kwargs: dict[str, Any] = dict(extra or {})
    type_map = {"int": int, "float": float, "bool": bool, "str": str, "time": time, "Path": Path}
    for f in dataclasses.fields(cls):
        key = f"SWING_{f.name.upper()}"
        if key in env:
            target = type_map.get(f.type if isinstance(f.type, str) else f.type.__name__, str)
            kwargs[f.name] = _coerce(env[key], target, f.name)
    return cls(**kwargs)
