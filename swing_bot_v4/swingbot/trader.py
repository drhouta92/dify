"""Scheduling and per-bar orchestration.

Cycle order per closed 15m bar (boundary B, bar_start = B - 15m, asof = B):
  1. reconcile with the broker (entries, fills, protection, quarantine)
  2. fetch 15m bars once for universe + held symbols, keep bars closed at B
  3. manage held positions (exits first, then stop ratchets)
  4. evaluate the entry gate; evaluate entry signals; rank; size; submit
  5. persist health

The universe is built before the open. Failures in discovery never stop position
management: held positions are managed even with no universe.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .broker import Broker, BrokerError, MarketData
from .config import Settings
from .execution import Execution
from .indicators import BAR_15M, ET, closed_bars, regular_session
from .risk import Candidate, GateInputs, entry_gate, in_cooldown, marketable_limit, position_size, rank
from .shariah import Compliance
from .store import Store, now_et
from .strategy import daily_context, evaluate_entry, manage_position
from .universe import Universe, UniverseBuilder

log = logging.getLogger("trader")


def atomic_write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class CycleResult:
    bar_start: str
    gate: str
    evaluated: int
    signals: list[str]
    entered: list[str]
    exits: list[str]
    reasons: dict[str, int]


class Trader:
    def __init__(self, settings: Settings, broker: Broker, md: MarketData, store: Store,
                 builder: UniverseBuilder, stop_event: threading.Event | None = None):
        self.s = settings
        self.broker, self.md, self.store, self.builder = broker, md, store, builder
        self.exe = Execution(broker, store, settings.strategy, settings.risk, settings.runtime)
        self.universe: Universe | None = None
        self.stop_event = stop_event or threading.Event()
        self.health_path = Path(settings.runtime.data_dir) / "health.json"
        self.health: dict[str, Any] = {"started_at": now_et().isoformat(), "phase": "starting"}
        self._universe_retry_at = 0.0

    # ------------------------------------------------------------------ health
    def _health(self, **updates: Any) -> None:
        self.health.update(updates, updated_at=now_et().isoformat())
        try:
            atomic_write_json(self.health_path, self.health)
        except OSError as exc:
            log.error("health write failed: %s", exc)

    # ------------------------------------------------------------------ preflight
    def preflight(self) -> None:
        acct = self.broker.get_account()
        if acct.status != "active" or acct.trading_blocked:
            raise RuntimeError(f"account not tradable: status={acct.status} blocked={acct.trading_blocked}")
        clock = self.broker.get_clock()
        end = clock.timestamp.normalize()
        start = (end - pd.Timedelta(days=10)).to_pydatetime()
        for feed in dict.fromkeys((self.s.runtime.daily_feed, self.s.runtime.intraday_feed)):
            try:
                spy = self.md.bars(["SPY"], "1d", start, end.to_pydatetime(), feed).get("SPY")
            except BrokerError as exc:
                log.error("preflight: %s daily bars failed: %s", feed, exc)
                continue
            if spy is not None and not spy.empty:
                log.info("preflight: %s daily bars OK", feed)
                break
        else:
            raise RuntimeError("no market data feed returned SPY daily bars; check keys/subscription")
        if self.md.latest_quote("SPY") is None:
            raise RuntimeError(f"{self.s.runtime.intraday_feed} quote for SPY unavailable")
        report = self.exe.reconcile()
        log.info("preflight ok: equity=%.2f cash=%.2f positions=%s unresolved=%d quarantined=%s",
                 acct.equity, acct.cash, report.broker_positions, report.unresolved, report.quarantined)

    # ------------------------------------------------------------------ universe
    def ensure_universe(self, session_date: date) -> None:
        if self.universe is not None and self.universe.session_date == session_date:
            return
        if time.monotonic() < self._universe_retry_at:
            return
        held = [p.symbol for p in self.store.active_positions()]
        try:
            self.universe = self.builder.build(session_date, held)
            self.store.put("universe", self.universe.summary() | {"members_list": self.universe.members})
            self._health(universe=self.universe.summary())
        except Exception as exc:  # noqa: BLE001 - discovery must never stop position management
            self._universe_retry_at = time.monotonic() + 300
            log.error("universe build failed (%s); held positions are still managed; retry in 5 min",
                      exc, exc_info=True)
            self.store.event("universe_failed", error=str(exc))

    # ------------------------------------------------------------------ accounting helpers
    def model_equity(self, broker_equity: float) -> float:
        if self.s.risk.sizing_equity <= 0:
            return broker_equity
        realized = sum(p.realized_pnl or 0.0 for p in self.store.closed_trades())
        return self.s.risk.sizing_equity + realized

    def _gate_inputs(self, prices: dict[str, float], untracked: list[str], unresolved: int,
                     session: date) -> tuple[GateInputs, float, float]:
        acct = self.broker.get_account()
        model = self.model_equity(acct.equity)
        active = self.store.active_positions()
        unrealized = sum((prices.get(p.symbol, p.entry_price or 0) - (p.entry_price or 0)) * p.qty
                         for p in active if p.entry_price)
        realized_today = sum(p.realized_pnl or 0.0 for p in self.store.closed_trades()
                             if p.closed_at and pd.Timestamp(p.closed_at).tz_convert(ET).date() == session)
        peak = max(float(self.store.get("equity_peak") or model), model + unrealized)
        self.store.put("equity_peak", peak)
        halt = bool(self.store.get("drawdown_halt", False))
        if peak > 0 and (peak - model - unrealized) / peak >= self.s.risk.max_drawdown_pct and not halt:
            self.store.put("drawdown_halt", True)
            self.store.event("drawdown_halt", peak=peak, equity=model + unrealized)
            log.critical("DRAWDOWN HALT: equity %.2f vs peak %.2f; entries stopped until `swingbot reset-halt`",
                         model + unrealized, peak)
            halt = True
        entries_today = sum(1 for p in self.store.positions()
                            if p.status != "void" and pd.Timestamp(p.created_at).tz_convert(ET).date() == session)
        slots = len({p.symbol for p in active} | set(untracked))
        used = sum((p.entry_price or 0) * p.qty for p in active)
        cash = min(acct.cash, model - used) if self.s.risk.sizing_equity > 0 else acct.cash
        g = GateInputs(model_equity=model, day_start_equity=model - realized_today, unrealized_pnl=unrealized,
                       realized_today=realized_today, equity_peak=peak, entries_today=entries_today,
                       open_slots_used=slots, unresolved_orders=unresolved, broker_equity=acct.equity,
                       daytrade_count=acct.daytrade_count, trading_blocked=acct.trading_blocked,
                       drawdown_halt=halt)
        return g, model, cash

    # ------------------------------------------------------------------ cycle
    def fetch_frames(self, symbols: list[str], asof: pd.Timestamp) -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        start = (asof - pd.Timedelta(days=self.s.runtime.history_days_15m)).to_pydatetime()
        raw = self.md.bars(symbols, "15m", start, None, self.s.runtime.intraday_feed)
        return {s: closed_bars(regular_session(df), asof) for s, df in raw.items()}

    def run_cycle(self, boundary: pd.Timestamp) -> CycleResult:
        sp, rp = self.s.strategy, self.s.risk
        bar_start, asof = boundary - BAR_15M, boundary
        session = bar_start.date()
        self._health(phase="cycle_running", cycle_bar=bar_start.isoformat())
        report = self.exe.reconcile()

        held = [p for p in self.store.positions("open", "closing")]
        members = self.universe.members if self.universe and self.universe.session_date == session else []
        symbols = sorted(set(members) | {p.symbol for p in held})
        frames = self.fetch_frames(symbols, asof)
        prices = {s: float(df["close"].iloc[-1]) for s, df in frames.items() if not df.empty}
        missing_held = [p.symbol for p in held if p.symbol not in frames or frames[p.symbol].empty]
        if missing_held:
            log.warning("no 15m data for held %s; broker-side protection only this bar", missing_held)

        # ---- exits and stop management
        exits: list[str] = []
        compliance = self.universe.compliance if self.universe else {}
        for pos in self.store.positions("closing"):
            if self.exe.exit_position(pos, pos.pending_exit or "EXIT_RETRY"):
                exits.append(pos.symbol)
        for pos in self.store.positions("open"):
            status = compliance.get(pos.symbol, Compliance.UNKNOWN)
            d = manage_position(pos.view(), frames.get(pos.symbol), bar_start, asof, sp,
                                shariah_exit=status.forces_exit)
            if d.action != "HOLD":
                log.info("EXIT SIGNAL %s %s %s", pos.symbol, d.action, d.reason)
                if self.exe.exit_position(pos, d.action):
                    exits.append(pos.symbol)
            else:
                self.exe.update_stop(pos, d.stop, d.hwm, d.be_locked)

        # ---- entries
        unresolved = self.exe.unresolved_count()
        gate, model, cash = self._gate_inputs(prices, report.untracked, unresolved, session)
        allowed, gate_reason = entry_gate(gate, rp)
        active_symbols = {p.symbol for p in self.store.active_positions()} | set(report.untracked)
        reasons: dict[str, int] = {}
        signals: list[Candidate] = []
        evaluated = 0
        for sym in members:
            if sym in active_symbols:
                continue
            last_exit = self.store.get(f"last_exit:{sym}")
            if in_cooldown(date.fromisoformat(last_exit) if last_exit else None, session, rp):
                reasons["COOLDOWN"] = reasons.get("COOLDOWN", 0) + 1
                continue
            ctx = daily_context(self.universe.daily.get(sym) if self.universe else None, session, sp)
            dec = evaluate_entry(frames.get(sym), ctx, bar_start, asof, sp)
            evaluated += 1
            key = dec.reason.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
            if dec.enter:
                signals.append(Candidate(sym, 0.0, 0.0, dec.features["atr_d"], dec.features["relative_volume"],
                                         0.0, dec.features))
        entered: list[str] = []
        if signals and allowed:
            quoted: list[Candidate] = []
            for c in signals:
                q = self.md.latest_quote(c.symbol)
                if q is None or not q.valid:
                    reasons["NO_QUOTE"] = reasons.get("NO_QUOTE", 0) + 1
                    continue
                quoted.append(Candidate(c.symbol, marketable_limit(q.ask, rp.entry_slip_pct), q.mid, c.atr_d,
                                        c.relative_volume, q.spread_pct, c.features))
            for cand in rank(quoted, sp, rp):
                gate, model, cash = self._gate_inputs(prices, report.untracked, self.exe.unresolved_count(), session)
                ok, gate_reason = entry_gate(gate, rp)
                if not ok:
                    break
                qty = position_size(model, cash, cand.limit, cand.atr_d, sp, rp)
                if qty <= 0:
                    log.info("ENTRY %s skipped: size 0 (model %.2f cash %.2f)", cand.symbol, model, cash)
                    continue
                earn = self.universe.earnings.get(cand.symbol) if self.universe else None
                pos = self.exe.open_position(cand, qty, earn.isoformat() if earn else None)
                if pos is not None:
                    entered.append(cand.symbol)
        elif signals:
            log.info("ENTRY gate closed (%s); %d signal(s) not taken", gate_reason, len(signals))

        result = CycleResult(bar_start.isoformat(), gate_reason, evaluated, [c.symbol for c in signals],
                             entered, exits, reasons)
        self.store.put("last_cycle", result.__dict__)
        self._health(phase="cycle_done", last_cycle_ok=now_et().isoformat(), last_cycle=result.__dict__,
                     open_positions=len(self.store.positions("open", "closing")),
                     quarantined=report.quarantined, unresolved=unresolved, untracked=report.untracked)
        log.info("CYCLE %s gate=%s evaluated=%d signals=%s entered=%s exits=%s reasons=%s",
                 bar_start.strftime("%H:%M"), gate_reason, evaluated, result.signals, entered, exits, reasons)
        return result

    # ------------------------------------------------------------------ loop
    def _sleep(self, seconds: float) -> bool:
        return self.stop_event.wait(max(0.0, seconds))

    def run_forever(self) -> None:
        self.preflight()
        rt = self.s.runtime
        last_boundary: pd.Timestamp | None = None
        failures = 0
        while not self.stop_event.is_set():
            try:
                clock = self.broker.get_clock()
                now = now_et()
                if not clock.is_open:
                    lead = pd.Timedelta(minutes=rt.universe_build_lead_min)
                    if clock.next_open - now <= lead:
                        self.ensure_universe(clock.next_open.date())
                    wait = (clock.next_open - lead - now).total_seconds()
                    self._health(phase="market_closed", next_open=clock.next_open.isoformat())
                    self._sleep(min(300.0, max(30.0, wait)))
                    continue
                self.ensure_universe(now.date())
                boundary = now.floor("15min")
                due = boundary + pd.Timedelta(seconds=rt.cycle_delay_s)
                session_open = boundary.normalize() + pd.Timedelta(hours=9, minutes=45)
                if boundary != last_boundary and now >= due and boundary >= session_open:
                    try:
                        self.run_cycle(boundary)
                        failures = 0
                    except BrokerError as exc:
                        failures += 1
                        log.error("cycle %s aborted by broker error: %s", boundary, exc)
                        self.store.event("cycle_failed", error=str(exc))
                    last_boundary = boundary
                self._heartbeat(now)
                self._sleep(2.0 if failures == 0 else min(60.0, 5.0 * failures))
            except Exception as exc:  # noqa: BLE001 - the loop must survive; systemd restarts on exit
                failures += 1
                log.error("loop error: %s", exc, exc_info=True)
                self._health(phase="loop_error", last_error=str(exc), last_error_at=now_et().isoformat())
                self._sleep(min(120.0, 10.0 * failures))
        log.info("stop requested; trader exiting cleanly")
        self._health(phase="stopped")

    def _heartbeat(self, now: pd.Timestamp) -> None:
        last_ok = self.health.get("last_cycle_ok")
        if not last_ok:
            return
        stale = (now - pd.Timestamp(last_ok)).total_seconds() / 60
        if stale > self.s.runtime.heartbeat_warn_min:
            last_warn = self.health.get("heartbeat_warned_at")
            if not last_warn or (now - pd.Timestamp(last_warn)).total_seconds() > 600:
                log.critical("HEARTBEAT: no successful cycle for %.0f min during market hours", stale)
                self._health(heartbeat_warned_at=now.isoformat())

