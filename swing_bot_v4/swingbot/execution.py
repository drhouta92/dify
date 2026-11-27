"""Order execution state machine.

Invariants (each one fixes a v3 failure mode, see REVIEW.md):

I1  Write-ahead: an order intent row (unique client_order_id) is committed BEFORE the
    network call. Every order the broker might hold is therefore resolvable after a crash.
I2  Unique ids: client_order_id = prefix + position id + monotone sequence. A retry never
    reuses an id (Alpaca rejects duplicates, which v3 misread as "submit failed").
I3  Unknown is not failure: a submit whose outcome is unknown blocks further submits of that
    purpose for that position until resolved by client_order_id (or declared absent after a
    grace period). It never blocks the whole bot forever and never needs an env-var restart.
I4  Accounting is recomputed, not accumulated: held qty = sum(entry fills) - sum(sell fills)
    over every broker order recorded for the position, so replaying a cycle cannot double-book.
I5  Broker is the source of truth: a quantity the bot cannot explain quarantines that one
    symbol; no exit price is ever fabricated.
I6  Replaced orders are followed through ``replaced_by``; a lost replace response cannot
    strand the bot on a dead order id.
I7  A filled position without a confirmed protective order is flattened.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from .broker import Broker, BrokerUnavailable, Order, OrderNotFound, OrderRejected
from .config import RiskParams, RuntimeParams, StrategyParams
from .indicators import ET
from .risk import Candidate
from .store import OrderRow, Position, Store, now_et
from .strategy import hwm_start_for_fill, initial_levels, round_tick

log = logging.getLogger("execution")
UNRESOLVED = ("intent", "unknown")
PREFIX = "sb4"
PURPOSE_CODE = {"entry": "b", "protect": "p", "exit": "x"}
CODE_PURPOSE = {v: k for k, v in PURPOSE_CODE.items()}


@dataclass
class Fills:
    entry_qty: int = 0
    entry_notional: float = 0.0
    entry_time: pd.Timestamp | None = None
    sold_qty: int = 0
    sold_notional: float = 0.0
    last_sell_time: pd.Timestamp | None = None
    sell_sources: set[str] = field(default_factory=set)

    @property
    def held(self) -> int:
        return self.entry_qty - self.sold_qty

    @property
    def entry_avg(self) -> float | None:
        return self.entry_notional / self.entry_qty if self.entry_qty else None


@dataclass
class ReconcileReport:
    broker_positions: dict[str, int]
    untracked: list[str]
    unresolved: int
    quarantined: list[str]


class Execution:
    def __init__(self, broker: Broker, store: Store, sp: StrategyParams, rp: RiskParams, rt: RuntimeParams,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
        self.broker, self.store, self.sp, self.rp, self.rt = broker, store, sp, rp, rt
        self.sleep, self.monotonic = sleep, monotonic

    # ================================================================== primitives
    def _coid(self, pos: Position, purpose: str) -> str:
        return f"{PREFIX}-{PURPOSE_CODE[purpose]}-{pos.symbol}-{pos.id[:12]}-{self.store.next_seq(pos.id)}"

    def _submit(self, pos: Position, purpose: str, side: str, send: Callable[[str], Order]) -> tuple[Order | None, str]:
        """Returns (order, state) with state in {'ok', 'rejected', 'unknown'}."""
        coid = self._coid(pos, purpose)
        self.store.order_intent(coid, pos.id, pos.symbol, purpose, side)           # I1
        try:
            order = send(coid)
        except OrderRejected as exc:
            self.store.set_order_state(coid, "rejected", active=False)
            self.store.event("order_rejected", pos.symbol, coid=coid, purpose=purpose, error=str(exc))
            log.error("%s %s order rejected: %s", pos.symbol, purpose, exc)
            return None, "rejected"
        except BrokerUnavailable as exc:
            self.store.set_order_state(coid, "unknown")
            self.store.event("order_unknown", pos.symbol, coid=coid, purpose=purpose, error=str(exc))
            log.critical("%s %s order outcome UNKNOWN (%s); will resolve by client_order_id", pos.symbol, purpose, exc)
            return None, "unknown"
        self.store.record_order(order, pos.id, purpose)
        self.store.event("order_submitted", pos.symbol, coid=coid, purpose=purpose, broker_id=order.id,
                         status=order.status)
        return order, "ok"

    def _age_min(self, row: OrderRow) -> float:
        return (now_et() - pd.Timestamp(row.created_at)).total_seconds() / 60

    def _resolve(self, row: OrderRow) -> Order | None:
        """Bring one recorded order up to date with the broker. Returns the latest snapshot."""
        if row.state in ("absent", "rejected"):
            return None
        try:
            if row.broker_id is None:
                order = self.broker.get_order_by_client_id(row.client_order_id)
            elif row.snapshot is not None and row.snapshot.is_terminal and row.snapshot.status != "replaced":
                return row.snapshot
            else:
                order = self.broker.get_order(row.broker_id)
        except OrderNotFound:
            if row.broker_id is None and self._age_min(row) >= self.rt.unknown_order_grace_min:
                self.store.set_order_state(row.client_order_id, "absent", active=False)
                self.store.event("order_absent", row.symbol, coid=row.client_order_id)
                log.warning("%s order %s never reached the broker; released", row.symbol, row.client_order_id)
            return None
        except BrokerUnavailable as exc:
            log.warning("%s cannot refresh %s: %s", row.symbol, row.client_order_id, exc)
            return row.snapshot
        self.store.record_order(order, row.position_id, row.purpose,
                                parent_broker_id=row.parent_broker_id)
        if order.status == "replaced" and order.replaced_by:                          # I6
            self.store.set_order_state(row.client_order_id, "replaced", active=False)
            try:
                successor = self.broker.get_order(order.replaced_by)
            except (OrderNotFound, BrokerUnavailable):
                return order
            self.store.record_order(successor, row.position_id, row.purpose,
                                    parent_broker_id=row.parent_broker_id, active=True)
            return successor
        return order

    def refresh_orders(self, pos: Position) -> list[OrderRow]:
        for row in self.store.orders(pos.id):
            if row.state in UNRESOLVED or (row.snapshot is not None and not row.snapshot.is_terminal) \
                    or (row.snapshot is not None and row.snapshot.status == "replaced"):
                self._resolve(row)
        return self.store.orders(pos.id)

    def fills(self, pos: Position) -> Fills:                                          # I4
        f = Fills()
        for row in self.store.orders(pos.id):
            o = row.snapshot
            if o is None or o.filled_qty <= 0 or o.filled_avg_price is None:
                continue
            ts = pd.Timestamp(o.filled_at).tz_convert(ET) if o.filled_at else None
            if o.side == "buy":
                f.entry_qty += o.filled_qty
                f.entry_notional += o.filled_qty * o.filled_avg_price
                f.entry_time = ts if f.entry_time is None or (ts and ts < f.entry_time) else f.entry_time
            else:
                f.sold_qty += o.filled_qty
                f.sold_notional += o.filled_qty * o.filled_avg_price
                if ts is not None and (f.last_sell_time is None or ts > f.last_sell_time):
                    f.last_sell_time = ts
                f.sell_sources.add(f"{row.purpose}:{o.type}")
        return f

    def unresolved_count(self) -> int:
        """Positions whose broker state is not yet known (opening, or an order of unknown outcome)."""
        return sum(1 for pos in self.store.active_positions()
                   if pos.status == "opening"
                   or any(r.state in UNRESOLVED for r in self.store.orders(pos.id, active_only=True)))

    def _wait_terminal(self, order_id: str, timeout_s: float, poll_s: float = 1.0) -> Order | None:
        deadline = self.monotonic() + timeout_s
        last: Order | None = None
        while True:
            try:
                last = self.broker.get_order(order_id)
            except (OrderNotFound, BrokerUnavailable) as exc:
                log.warning("poll %s failed: %s", order_id, exc)
            if (last is not None and last.is_terminal) or self.monotonic() >= deadline:
                return last
            self.sleep(poll_s)

    # ================================================================== entry
    def open_position(self, cand: Candidate, qty: int, next_earnings: str | None) -> Position | None:
        if qty <= 0:
            return None
        pos = self.store.new_position(cand.symbol, cand.atr_d, qty, {
            "limit": cand.limit, "mid": cand.mid, "spread_pct": cand.spread_pct,
            "cost_r": cand.cost_r(self.sp.stop_atr_mult), "features": cand.features})
        pos.next_earnings = next_earnings
        self.store.save_position(pos)
        order, state = self._submit(pos, "entry", "buy",
                                    lambda coid: self.broker.submit_limit_buy(cand.symbol, qty, cand.limit, coid))
        if state == "rejected":
            self._void(pos, "entry rejected")
            return None
        if order is None:
            return pos                                   # unknown: reconcile() resolves it (I3)
        final = self._wait_terminal(order.id, self.rt.fill_timeout_s)
        if final is not None and not final.is_terminal:
            self._cancel(final.id)
            final = self._wait_terminal(order.id, self.rt.cancel_settle_s)
        if final is not None:
            self.store.record_order(final, pos.id, "entry")
        return self.settle_entry(self.store.position(pos.id))

    def settle_entry(self, pos: Position) -> Position | None:
        """Finalize an 'opening' position once every entry order is terminal."""
        rows = self.refresh_orders(pos)
        entries = [r for r in rows if r.purpose == "entry"]
        for r in entries:
            if r.state in UNRESOLVED:
                return pos                                # still unknown; keep the slot reserved
            if r.snapshot is not None and not r.snapshot.is_terminal:
                if self._age_min(r) * 60 > self.rt.fill_timeout_s + self.rt.cancel_settle_s:
                    self._cancel(r.snapshot.id)           # stale DAY limit from a crash: stop it
                return pos
        f = self.fills(pos)
        if f.entry_qty <= 0:
            self._void(pos, "no fill")
            return None
        fill = float(f.entry_avg)  # type: ignore[arg-type]
        stop, target = initial_levels(fill, pos.atr_d, self.sp)
        fill_time = f.entry_time or now_et()
        pos.entry_price, pos.entry_qty, pos.qty = fill, f.entry_qty, f.held
        pos.entry_time, pos.hwm_from = fill_time.isoformat(), hwm_start_for_fill(fill_time).isoformat()
        pos.initial_stop, pos.stop, pos.target, pos.hwm = stop, stop, target, fill
        pos.status = "open"
        pos.meta["entry_slippage_pct"] = round((fill / pos.meta["mid"] - 1) * 100, 4) if pos.meta.get("mid") else None
        self.store.save_position(pos)
        self.store.event("entry_filled", pos.symbol, qty=f.entry_qty, fill=fill, stop=stop, target=target)
        log.info("ENTRY %s qty=%d fill=%.4f stop=%.2f target=%s risk=$%.2f", pos.symbol, f.entry_qty, fill,
                 stop, target, (fill - stop) * f.entry_qty)
        if pos.qty > 0 and not self.ensure_protection(pos):                                 # I7
            log.critical("FLATTEN %s: protective order could not be confirmed", pos.symbol)
            self.exit_position(self.store.position(pos.id), "EXIT_UNPROTECTED")
        return self.store.position(pos.id)

    def _void(self, pos: Position, why: str) -> None:
        pos.status, pos.closed_at = "void", now_et().isoformat()
        pos.meta["void_reason"] = why
        self.store.save_position(pos)
        self.store.event("position_void", pos.symbol, reason=why)
        log.info("ENTRY %s not taken: %s", pos.symbol, why)

    def _cancel(self, order_id: str) -> None:
        try:
            self.broker.cancel_order(order_id)
        except (OrderNotFound, OrderRejected) as exc:
            log.info("cancel %s: %s (already final?)", order_id, exc)
        except BrokerUnavailable as exc:
            log.warning("cancel %s outcome unknown: %s", order_id, exc)

    # ================================================================== protection
    def _protect_rows(self, pos: Position) -> list[OrderRow]:
        return [r for r in self.store.orders(pos.id, "protect", active_only=True)]

    def _live_protection(self, pos: Position) -> tuple[list[Order], bool]:
        """(live protective orders, unresolved?) after refreshing every active protect row."""
        live: list[Order] = []
        unresolved = False
        for row in self._protect_rows(pos):
            order = self._resolve(row)
            if order is None:
                fresh = self.store.orders(pos.id, "protect")
                if any(r.client_order_id == row.client_order_id and r.state in UNRESOLVED for r in fresh):
                    unresolved = True
                continue
            if order.is_live:
                live.append(order)
            elif order.is_terminal:
                self.store.set_order_state(row.client_order_id, order.status, active=False)
        uniq = {o.id: o for o in live}
        return list(uniq.values()), unresolved

    def _sync_qty(self, pos: Position) -> Position:
        f = self.fills(pos)
        if f.held != pos.qty:
            pos.qty = f.held
            self.store.save_position(pos)
        return pos

    def ensure_protection(self, pos: Position) -> bool:
        """Guarantee one live protective order covering exactly pos.qty at >= pos.stop."""
        live, unresolved = self._live_protection(pos)
        pos = self._sync_qty(self.store.position(pos.id))
        if pos.qty <= 0:
            self.finalize_close(pos)
            return True
        if unresolved:
            return False                                       # I3: never stack a second order
        stops = [o for o in live if o.type in ("stop", "stop_limit")]
        if live and stops:
            stop_order = stops[0]
            covered = stop_order.qty - stop_order.filled_qty
            if covered == pos.qty and stop_order.stop_price is not None:
                if stop_order.stop_price > pos.stop + 1e-9:   # a replace we lost the response for
                    pos.stop = round_tick(stop_order.stop_price)
                    self.store.save_position(pos)
                elif stop_order.stop_price < pos.stop - 1e-9:
                    return self._raise_stop(pos, stop_order, pos.stop)
                return True
            log.warning("%s protective qty %d != held %d; re-issuing", pos.symbol, covered, pos.qty)
            if not self._cancel_protection(pos):
                return False
            return self.ensure_protection(self.store.position(pos.id))
        if live:                                              # e.g. OCO limit live but stop leg missing
            if not self._cancel_protection(pos):
                return False
            pos = self._sync_qty(self.store.position(pos.id))
            if pos.qty <= 0:
                self.finalize_close(pos)
                return True
        for attempt in range(1, self.rt.protect_retries + 1):
            order, state = self._submit(pos, "protect", "sell", lambda coid: self.broker.submit_protection(
                pos.symbol, pos.qty, pos.stop, pos.target, coid))
            if state == "unknown":
                live_now, _ = self._live_protection(pos)       # one immediate lookup by client_order_id
                return any(o.type in ("stop", "stop_limit") for o in live_now) or \
                    any(leg.type in ("stop", "stop_limit") for o in live_now for leg in o.legs)
            if order is not None:
                check = self._wait_terminal(order.id, 3.0) or order
                self.store.record_order(check, pos.id, "protect")
                if check.is_live or check.status == "filled" or any(o.is_live for o in check.all_orders()):
                    self.store.event("protection_live", pos.symbol, stop=pos.stop, target=pos.target, qty=pos.qty)
                    return True
                self.store.set_order_state(check.client_order_id, check.status, active=False)
            log.error("%s protection attempt %d/%d failed", pos.symbol, attempt, self.rt.protect_retries)
            self.sleep(1.0)
        self.store.event("unprotected", pos.symbol, qty=pos.qty, stop=pos.stop)
        return False

    def _raise_stop(self, pos: Position, stop_order: Order, new_stop: float) -> bool:
        parent = next((r.parent_broker_id for r in self._protect_rows(pos) if r.broker_id == stop_order.id), None)
        coid = self._coid(pos, "protect")
        self.store.order_intent(coid, pos.id, pos.symbol, "protect", "sell")              # I1
        try:
            replaced = self.broker.replace_stop(stop_order.id, new_stop, coid)
        except OrderRejected as exc:
            self.store.set_order_state(coid, "rejected", active=False)
            log.warning("%s replace rejected (%s); cancel and re-issue", pos.symbol, exc)
            if not self._cancel_protection(pos):
                return False
            pos = self.store.position(pos.id)
            pos.stop = new_stop
            self.store.save_position(pos)
            return self.ensure_protection(pos)
        except (BrokerUnavailable, OrderNotFound) as exc:
            self.store.set_order_state(coid, "unknown")
            log.warning("%s replace outcome unknown (%s); resolved next cycle", pos.symbol, exc)
            return True        # the old stop (or its replacement) still protects the position
        for row in self._protect_rows(pos):
            if row.broker_id == stop_order.id:
                self.store.set_order_state(row.client_order_id, "replaced", active=False)
        self.store.record_order(replaced, pos.id, "protect", parent_broker_id=parent, active=True)
        pos.stop = new_stop
        self.store.save_position(pos)
        self.store.event("stop_raised", pos.symbol, stop=new_stop, broker_id=replaced.id)
        log.info("STOP %s raised to %.2f", pos.symbol, new_stop)
        return True

    def update_stop(self, pos: Position, desired_stop: float, hwm: float, be_locked: bool) -> None:
        pos.hwm, pos.be_locked = hwm, be_locked
        self.store.save_position(pos)
        if desired_stop <= pos.stop + 0.005:
            return
        live, unresolved = self._live_protection(pos)
        stops = [o for o in live if o.type in ("stop", "stop_limit")]
        if unresolved or not stops:
            pos.stop = desired_stop            # applied when protection is (re)issued
            self.store.save_position(pos)
            self.ensure_protection(pos)
            return
        self._raise_stop(pos, stops[0], desired_stop)

    def _cancel_protection(self, pos: Position) -> bool:
        """Cancel every live protective order and wait until all are terminal."""
        live, unresolved = self._live_protection(pos)
        if unresolved:
            return False
        for o in live:
            if o.order_class == "oco" or not any(o.id in {l.id for l in p.legs} for p in live):
                self._cancel(o.id)
        ok = True
        for o in live:
            final = self._wait_terminal(o.id, self.rt.cancel_settle_s)
            if final is None or not final.is_terminal:
                ok = False
                continue
            row = next((r for r in self.store.orders(pos.id, "protect") if r.broker_id == final.id), None)
            self.store.record_order(final, pos.id, "protect",
                                    parent_broker_id=row.parent_broker_id if row else None, active=False)
        if ok:
            self.store.deactivate_orders(pos.id, "protect")
        return ok

    # ================================================================== exit
    def exit_position(self, pos: Position, reason: str) -> bool:
        """Idempotent market exit. Returns True when flat. Safe to call every cycle."""
        if pos.status not in ("open", "closing"):
            return False
        pos.pending_exit = pos.pending_exit or reason
        pos.status = "closing"
        self.store.save_position(pos)
        self.refresh_orders(pos)
        exits = self.store.orders(pos.id, "exit", active_only=True)
        if any(r.state in UNRESOLVED for r in exits):
            return False
        live_exit = [r.snapshot for r in exits if r.snapshot is not None and r.snapshot.is_live]
        if not live_exit and not self._cancel_protection(pos):
            log.error("%s exit deferred: protective order not confirmed cancelled", pos.symbol)
            return False
        pos = self._sync_qty(self.store.position(pos.id))
        if pos.qty <= 0:
            self.finalize_close(pos)
            return True
        if live_exit:
            order = live_exit[0]
        else:
            order, state = self._submit(pos, "exit", "sell",
                                        lambda coid: self.broker.submit_market_sell(pos.symbol, pos.qty, coid))
            if order is None:
                if state == "rejected":
                    pos.status, pos.pending_exit = "open", None
                    self.store.save_position(pos)
                    self.ensure_protection(pos)
                return False
        final = self._wait_terminal(order.id, self.rt.fill_timeout_s)
        if final is not None:
            self.store.record_order(final, pos.id, "exit", active=not final.is_terminal)
        pos = self._sync_qty(self.store.position(pos.id))
        if pos.qty <= 0:
            self.finalize_close(pos)
            return True
        if final is not None and final.is_terminal:
            log.error("%s exit order ended %s with %d shares left; re-protecting", pos.symbol, final.status, pos.qty)
            self.ensure_protection(pos)
        return False

    def finalize_close(self, pos: Position) -> None:
        f = self.fills(pos)
        if f.sold_qty <= 0 or f.entry_qty <= 0 or f.held != 0:
            return
        exit_px = f.sold_notional / f.sold_qty
        pnl = f.sold_notional - f.entry_notional
        risk = (pos.entry_price - pos.initial_stop) * pos.entry_qty if pos.entry_price and pos.initial_stop else None
        if f.sell_sources <= {"protect:stop", "protect:stop_limit"}:
            reason = "EXIT_STOP"
        elif f.sell_sources == {"protect:limit"}:
            reason = "EXIT_TARGET"
        elif f.sell_sources <= {"exit:market"}:
            reason = pos.pending_exit or "EXIT_MARKET"
        else:
            reason = "EXIT_MIXED:" + "+".join(sorted(f.sell_sources))
        pos.status, pos.qty, pos.exit_price = "closed", 0, exit_px
        pos.exit_reason, pos.realized_pnl = reason, pnl
        pos.closed_at = (f.last_sell_time or now_et()).isoformat()
        pos.meta["r_multiple"] = pnl / risk if risk and risk > 0 else None
        self.store.save_position(pos)
        self.store.deactivate_orders(pos.id, "protect")
        self.store.deactivate_orders(pos.id, "exit")
        self.store.put(f"last_exit:{pos.symbol}", pd.Timestamp(pos.closed_at).tz_convert(ET).date().isoformat())
        self.store.event("exit_filled", pos.symbol, reason=reason, exit_price=exit_px, pnl=pnl,
                         r_multiple=pos.meta["r_multiple"])
        log.info("EXIT %s %s qty=%d avg=%.4f pnl=$%.2f R=%s", pos.symbol, reason, f.sold_qty, exit_px, pnl,
                 None if pos.meta["r_multiple"] is None else round(pos.meta["r_multiple"], 3))

    # ================================================================== reconciliation
    def _adopt_unknown_orders(self, pos: Position) -> None:
        """Pull in broker orders carrying this position's client_order_id prefix we never recorded."""
        if not pos.created_at:
            return
        marker = pos.id[:12]
        known = {r.broker_id for r in self.store.orders(pos.id)}
        try:
            orders = self.broker.list_orders(pos.symbol, pd.Timestamp(pos.created_at).to_pydatetime())
        except (BrokerUnavailable, OrderRejected) as exc:
            log.warning("%s order listing failed: %s", pos.symbol, exc)
            return
        for o in orders:
            for sub in o.all_orders():
                if sub.id not in known and marker in sub.client_order_id:
                    code = sub.client_order_id[len(PREFIX) + 1:len(PREFIX) + 2]
                    purpose = CODE_PURPOSE.get(code, "protect")
                    self.store.record_order(sub, pos.id, purpose, active=sub.is_live)
                    self.store.event("order_adopted", pos.symbol, broker_id=sub.id, coid=sub.client_order_id)

    def quarantine(self, pos: Position, reason: str) -> None:
        if pos.status != "quarantined":
            pos.status, pos.quarantine_reason = "quarantined", reason
            self.store.save_position(pos)
            self.store.event("quarantined", pos.symbol, reason=reason)
        log.critical("QUARANTINE %s: %s. Entries blocked for this symbol; manual reconciliation required "
                     "(swingbot quarantine-clear %s).", pos.symbol, reason, pos.symbol)

    def reconcile(self) -> ReconcileReport:
        broker_pos = self.broker.get_positions()          # raises BrokerUnavailable -> cycle aborts
        for pos in self.store.active_positions():
            if pos.status == "opening":
                settled = self.settle_entry(pos)
                if settled is None or settled.status != "open":
                    continue
                pos = settled
            self.refresh_orders(pos)
            pos = self._sync_qty(self.store.position(pos.id))
            actual = broker_pos.get(pos.symbol, 0)
            if pos.status == "quarantined":
                continue
            if actual != pos.qty:
                self._adopt_unknown_orders(pos)
                self.refresh_orders(pos)
                pos = self._sync_qty(self.store.position(pos.id))
            if actual != pos.qty:
                self.quarantine(pos, f"bot qty {pos.qty} != broker qty {actual}")
                continue
            if pos.qty == 0:
                self.finalize_close(pos)
            elif pos.status == "open":
                if not self.ensure_protection(pos):
                    log.critical("UNPROTECTED %s qty=%d; retrying every cycle", pos.symbol, pos.qty)
        managed = {p.symbol for p in self.store.active_positions()}
        untracked = sorted(s for s in broker_pos if s not in managed)
        if untracked:
            log.warning("broker holds unmanaged symbols (counted against slots, never traded): %s", untracked)
        quarantined = [p.symbol for p in self.store.positions("quarantined")]
        return ReconcileReport(broker_pos, untracked, self.unresolved_count(), quarantined)
