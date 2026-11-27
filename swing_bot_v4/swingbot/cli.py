"""Command-line entry point: ``python -m swingbot <command>``.

Commands
  run                      start the trader (systemd runs this)
  status                   read-only snapshot of positions, health and recent events
  report                   live completed-trade statistics in R-multiples
  backtest                 run the identical strategy on historical SIP bars
  backup                   consistent SQLite backup (keeps 30)
  quarantine-clear SYMBOL  re-sync a quarantined symbol with the broker after manual review
  reset-halt               clear the drawdown halt and reset the equity peak
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import threading
from datetime import date, datetime, timedelta
from logging.handlers import WatchedFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import ConfigError, Settings
from .indicators import ET, regular_session
from .report import TradeRecord, format_summary, summarize
from .store import Store, now_et

log = logging.getLogger("cli")
EXCLUDED_REASONS = ("RECONCILED",)


def setup_logging(data_dir: Path, level: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout),
                                       WatchedFileHandler(data_dir / "swingbot.log", encoding="utf-8")]
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(level.upper())
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _data_dir(settings: Settings) -> Path:
    d = Path(settings.runtime.data_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store(settings: Settings) -> Store:
    return Store(_data_dir(settings) / "state.sqlite3")


# ---------------------------------------------------------------------- run
def cmd_run(settings: Settings, _: argparse.Namespace) -> int:
    from .alpaca_broker import AlpacaBroker, AlpacaMarketData
    from .shariah import ShariahScreen
    from .trader import Trader
    from .universe import UniverseBuilder

    data = _data_dir(settings)
    setup_logging(data, settings.runtime.log_level)
    settings.runtime.require_credentials()
    lock = open(data / ".trader.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.critical("another trader holds %s; refusing to start a second instance", data / ".trader.lock")
        return 3
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    rt = settings.runtime
    broker, md, store = AlpacaBroker(rt), AlpacaMarketData(rt), Store(data / "state.sqlite3")
    shariah = ShariahScreen(data / "shariah_cache.json", data / "shariah_overrides.json", rt.shariah_cache_days,
                            rt.shariah_concurrency, rt.shariah_user_agent,
                            today=lambda: datetime.now(ZoneInfo(ET)).date())
    builder = UniverseBuilder(md, broker, shariah, settings.universe, settings.strategy, rt)
    trader = Trader(settings, broker, md, store, builder, stop)
    log.info("swingbot v4 starting (PAPER) data_dir=%s", data)
    try:
        trader.run_forever()
    finally:
        store.close()
    return 0


# ---------------------------------------------------------------------- status / report
def cmd_status(settings: Settings, _: argparse.Namespace) -> int:
    store = _store(settings)
    health_path = _data_dir(settings) / "health.json"
    if health_path.exists():
        health = json.loads(health_path.read_text(encoding="utf-8"))
        age = (now_et() - pd.Timestamp(health.get("updated_at"))).total_seconds() / 60 if health.get("updated_at") else None
        print(f"health phase={health.get('phase')} updated {age:.1f} min ago" if age is not None else "health: none")
        print(json.dumps({k: health.get(k) for k in ("last_cycle_ok", "last_cycle", "quarantined", "unresolved",
                                                     "untracked", "universe")}, indent=2, default=str))
    else:
        print("no health.json yet")
    print("\nACTIVE POSITIONS")
    for p in store.active_positions():
        print(f"  {p.symbol:6s} {p.status:11s} qty={p.qty:<5d} entry={p.entry_price} stop={p.stop} "
              f"target={p.target} be={p.be_locked} pending_exit={p.pending_exit} quarantine={p.quarantine_reason}")
    print(f"\nflags: drawdown_halt={store.get('drawdown_halt', False)} equity_peak={store.get('equity_peak')}")
    print("\nRECENT EVENTS")
    for e in reversed(store.events(15)):
        print(f"  {e['ts'][:19]} {e['kind']:18s} {e['symbol'] or '':6s} {json.dumps(e['payload'], default=str)[:120]}")
    return 0


def live_trades(store: Store) -> list[TradeRecord]:
    out: list[TradeRecord] = []
    for p in store.closed_trades():
        if (p.exit_reason or "").startswith(EXCLUDED_REASONS) or not p.entry_price or not p.initial_stop:
            continue
        out.append(TradeRecord(p.symbol, pd.Timestamp(p.entry_time).tz_convert(ET),
                               pd.Timestamp(p.closed_at).tz_convert(ET), p.entry_price, p.exit_price or 0.0,
                               p.entry_qty, p.initial_stop, p.realized_pnl or 0.0, p.exit_reason or "?"))
    return out


def cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    trades = live_trades(_store(settings))
    equity = args.initial_equity or settings.risk.sizing_equity or 100_000.0
    print(format_summary(summarize(trades, equity), f"LIVE PAPER TRADES (initial equity ${equity:,.2f})"))
    return 0


# ---------------------------------------------------------------------- backtest
def _cached_bars(md, symbols: list[str], tf: str, start: datetime, end: datetime, feed: str,
                 cache: Path) -> dict[str, pd.DataFrame]:
    cache.mkdir(parents=True, exist_ok=True)
    out, missing = {}, []
    for s in symbols:
        f = cache / f"{s}_{tf}_{feed}_{start:%Y%m%d}_{end:%Y%m%d}.csv.gz"
        if f.exists():
            df = pd.read_csv(f, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
            out[s] = df
        else:
            missing.append(s)
    if missing:
        fetched = md.bars(missing, tf, start, end, feed)
        for s, df in fetched.items():
            df.to_csv(cache / f"{s}_{tf}_{feed}_{start:%Y%m%d}_{end:%Y%m%d}.csv.gz")
            out[s] = df
    return out


def cmd_backtest(settings: Settings, args: argparse.Namespace) -> int:
    from .alpaca_broker import AlpacaMarketData
    from .backtest import CostModel, run_backtest

    setup_logging(_data_dir(settings), "INFO")
    symbols = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start >= end:
        raise ConfigError("--start must be before --end")
    tz = ZoneInfo(ET)
    fetch_start = datetime.combine(start - timedelta(days=settings.runtime.history_days_1d), datetime.min.time(), tz)
    fetch_end = min(datetime.combine(end + timedelta(days=1), datetime.min.time(), tz),
                    datetime.now(tz) - timedelta(minutes=20))
    md = AlpacaMarketData(settings.runtime)
    cache = _data_dir(settings) / "cache"
    start_15m = datetime.combine(start - timedelta(days=settings.runtime.history_days_15m + 5),
                                 datetime.min.time(), tz)
    b15 = {s: regular_session(df) for s, df in
           _cached_bars(md, symbols, "15m", start_15m, fetch_end, "sip", cache).items()}
    daily = _cached_bars(md, symbols, "1d", fetch_start, fetch_end, "sip", cache)
    res = run_backtest(b15, daily, settings.strategy, settings.risk, start, end,
                       settings.runtime.history_days_15m, CostModel(args.half_spread, args.slip),
                       args.initial_equity)
    equity = args.initial_equity or settings.risk.sizing_equity or 100_000.0
    print(format_summary(res.summary, f"BACKTEST {start}..{end} on {len(symbols)} symbols (SIP 15m)"))
    for k in ("max_mtm_dd_pct", "daily_sharpe_annualized", "signals"):
        if k in res.summary:
            print(f"  {k}: {res.summary[k]:.3f}" if isinstance(res.summary[k], float) else f"  {k}: {res.summary[k]}")
    print(f"  skipped: {res.skipped}")
    print(f"  initial equity ${equity:,.2f}; selection/survivorship bias: symbols chosen today.")
    if args.out:
        res.trades_frame().to_csv(args.out, index=False)
        print(f"  trades written to {args.out}")
    return 0


# ---------------------------------------------------------------------- maintenance
def cmd_backup(settings: Settings, _: argparse.Namespace) -> int:
    data = _data_dir(settings)
    src = data / "state.sqlite3"
    if not src.exists():
        print("no state.sqlite3 yet")
        return 0
    backups = data / "backups"
    backups.mkdir(mode=0o750, exist_ok=True)
    stamp = now_et().strftime("%Y-%m-%d")
    raw = backups / f"state-{stamp}.sqlite3"
    with sqlite3.connect(src) as s, sqlite3.connect(raw) as d:
        s.backup(d)                       # consistent online snapshot even while the trader writes
    with open(raw, "rb") as fi, gzip.open(str(raw) + ".gz.tmp", "wb") as fo:
        shutil.copyfileobj(fi, fo)
    os.replace(str(raw) + ".gz.tmp", str(raw) + ".gz")
    raw.unlink()
    for old in sorted(backups.glob("state-????-??-??.sqlite3.gz"))[:-30]:
        old.unlink()
    print(f"backup written: {raw}.gz")
    return 0


def cmd_quarantine_clear(settings: Settings, args: argparse.Namespace) -> int:
    from .alpaca_broker import AlpacaBroker
    from .execution import Execution

    store = _store(settings)
    broker = AlpacaBroker(settings.runtime)
    exe = Execution(broker, store, settings.strategy, settings.risk, settings.runtime)
    symbol = args.symbol.upper()
    targets = [p for p in store.positions("quarantined") if p.symbol == symbol]
    if not targets:
        print(f"{symbol} is not quarantined")
        return 1
    actual = broker.get_positions().get(symbol, 0)
    for pos in targets:
        exe.refresh_orders(pos)
        if actual == 0:
            pos.status, pos.qty = "closed", 0
            pos.exit_reason = "RECONCILED_FLAT"
            f = exe.fills(pos)
            pos.realized_pnl = f.sold_notional - f.entry_avg * f.sold_qty if f.entry_avg and f.sold_qty else None
            pos.closed_at = now_et().isoformat()
            store.save_position(pos)
            print(f"{symbol}: broker flat; closed as RECONCILED_FLAT (excluded from statistics)")
        else:
            pos.status, pos.qty, pos.quarantine_reason = "open", actual, None
            pos.meta["qty_overridden_by_operator"] = actual
            store.save_position(pos)
            print(f"{symbol}: adopted broker qty {actual}; protection is re-issued on the next cycle")
        store.event("quarantine_cleared", symbol, broker_qty=actual)
    return 0


def cmd_reset_halt(settings: Settings, _: argparse.Namespace) -> int:
    store = _store(settings)
    store.put("drawdown_halt", False)
    store.put("equity_peak", None)
    store.event("halt_reset")
    print("drawdown halt cleared; equity peak resets on the next cycle")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swingbot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    rep = sub.add_parser("report")
    rep.add_argument("--initial-equity", type=float)
    bt = sub.add_parser("backtest")
    bt.add_argument("--symbols", required=True, help="comma-separated tickers")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end", required=True)
    bt.add_argument("--initial-equity", type=float)
    bt.add_argument("--half-spread", type=float, default=0.0015)
    bt.add_argument("--slip", type=float, default=0.0005)
    bt.add_argument("--out")
    sub.add_parser("backup")
    qc = sub.add_parser("quarantine-clear")
    qc.add_argument("symbol")
    sub.add_parser("reset-halt")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    handlers = {"run": cmd_run, "status": cmd_status, "report": cmd_report, "backtest": cmd_backtest,
                "backup": cmd_backup, "quarantine-clear": cmd_quarantine_clear, "reset-halt": cmd_reset_halt}
    try:
        return handlers[args.cmd](settings, args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
