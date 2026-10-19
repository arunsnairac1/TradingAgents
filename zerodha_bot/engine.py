"""One trading day: scan -> agent signal -> rules -> orders -> monitor -> square off."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

import pytz

from . import risk, scanner
from .broker import Broker
from .config import BotConfig
from .costs import net_pnl
from .signals import SignalSource
from .state import Ledger, Position, Store

log = logging.getLogger("zbot.engine")
IST = pytz.timezone("Asia/Kolkata")


def _hhmm(now: datetime) -> str:
    return now.strftime("%H:%M")


class Engine:
    def __init__(self, cfg: BotConfig, broker: Broker, signals: SignalSource, store: Store,
                 now: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.broker, self.signals, self.store = cfg, broker, signals, store
        self.now = now or (lambda: datetime.now(IST))
        self.sleep = sleep
        self.day = self.now().strftime("%Y-%m-%d")
        self.ledger = store.load(self.day)
        self.accumulated = store.accumulated_profit(self.day)

    # --- helpers -----------------------------------------------------------
    def _save(self):
        self.store.save(self.ledger)

    def _unrealized(self) -> float:
        if not self.ledger.positions:
            return 0.0
        quotes = self.broker.quotes(list(self.ledger.positions))
        return sum(net_pnl(p.direction, p.qty, p.entry, quotes[s].ltp)
                   for s, p in self.ledger.positions.items())

    # --- entries -----------------------------------------------------------
    def enter(self, symbol: str, direction: str, entry_hint: float, rating: str) -> bool:
        plan = risk.size(self.cfg, self.ledger, self.accumulated, symbol, direction, entry_hint)
        if isinstance(plan, str):
            log.info("%s skipped: %s", symbol, plan)
            return False
        side, exit_side = ("BUY", "SELL") if direction == "LONG" else ("SELL", "BUY")
        try:
            fill = self.broker.market_order(symbol, side, plan.qty)
        except Exception as exc:  # a failed entry must never kill the loop
            log.error("%s entry failed: %s", symbol, exc)
            return False
        # Re-anchor stop/target on the real fill price.
        sign = 1 if direction == "LONG" else -1
        stop = round(fill.price * (1 - sign * self.cfg.stop_loss_pct / 100), 2)
        target = round(fill.price * (1 + sign * self.cfg.target_pct / 100), 2)
        pos = Position(symbol, direction, fill.qty, fill.price, stop, target,
                       risk.worst_loss(direction, fill.qty, fill.price, stop), rating, fill.order_id)
        try:
            pos.stop_order = self.broker.stop_order(symbol, exit_side, fill.qty, stop)
        except Exception as exc:
            log.error("%s: could not place exchange stop (%s); bot-side stop only", symbol, exc)
        self.ledger.positions[symbol] = pos
        self.ledger.traded.append(symbol)
        self._save()
        log.info("ENTER %s %s x%d @ %.2f stop %.2f target %.2f", direction, symbol, fill.qty, fill.price, stop, target)
        return True

    # --- exits -------------------------------------------------------------
    def exit(self, symbol: str, reason: str, **fill_hints) -> None:
        pos = self.ledger.positions[symbol]
        if pos.stop_order:
            try:
                self.broker.cancel(pos.stop_order)
            except Exception as exc:
                log.warning("%s: cancel of stop order failed: %s", symbol, exc)
        side = "SELL" if pos.direction == "LONG" else "BUY"
        fill = self.broker.market_order(symbol, side, pos.qty, **fill_hints)  # raises -> retried next tick
        pnl = net_pnl(pos.direction, pos.qty, pos.entry, fill.price)
        self.ledger.realized += pnl
        self.ledger.trades.append({"symbol": symbol, "direction": pos.direction, "qty": pos.qty,
                                   "entry": pos.entry, "exit": fill.price, "net_pnl": round(pnl, 2),
                                   "reason": reason, "rating": pos.rating})
        del self.ledger.positions[symbol]
        self._save()
        log.info("EXIT %s (%s) @ %.2f net %.2f | day realized %.2f", symbol, reason, fill.price, pnl, self.ledger.realized)

    def flatten(self, reason: str) -> None:
        for symbol in list(self.ledger.positions):
            try:
                self.exit(symbol, reason)
            except Exception as exc:
                log.error("%s: flatten failed (%s); will retry", symbol, exc)

    # --- loop --------------------------------------------------------------
    def check_positions(self) -> None:
        if not self.ledger.positions:
            return
        quotes = self.broker.quotes(list(self.ledger.positions))
        for symbol, pos in list(self.ledger.positions.items()):
            q = quotes[symbol]
            lo = q.ltp if q.low is None else q.low
            hi = q.ltp if q.high is None else q.high
            long_ = pos.direction == "LONG"
            stop_hit = lo <= pos.stop if long_ else hi >= pos.stop
            target_hit = hi >= pos.target if long_ else lo <= pos.target
            # A bar that touches both is scored as the stop: the pessimistic reading.
            if stop_hit:
                self._try_exit(symbol, "STOP", stop_price=pos.stop)
            elif target_hit:
                self._try_exit(symbol, "TARGET", limit_price=pos.target)

    def _try_exit(self, symbol, reason, **fill_hints):
        try:
            self.exit(symbol, reason, **fill_hints)
        except Exception as exc:
            log.error("%s: %s exit failed (%s); retrying next tick", symbol, reason, exc)

    def tick(self) -> bool:
        """One monitoring pass. Returns False once the day is over."""
        self.check_positions()
        status = risk.day_status(self.cfg, self.ledger, self.accumulated, self._unrealized())
        if status and not self.ledger.halted:
            log.warning("Day limit hit: %s. Flattening and halting.", status)
            self.ledger.halted = status
            self._save()
        if self.ledger.halted:
            self.flatten(self.ledger.halted)
        if _hhmm(self.now()) >= self.cfg.squareoff_at:
            self.ledger.halted = self.ledger.halted or "SQUAREOFF"
            self.flatten("SQUAREOFF")
        return not (self.ledger.halted and not self.ledger.positions)

    def run(self) -> Ledger:
        while _hhmm(self.now()) < self.cfg.scan_after:
            self.sleep(self.cfg.poll_seconds)

        if not self.ledger.halted and not self.ledger.traded:
            self._scan_and_enter()

        while self.tick():
            self.sleep(self.cfg.poll_seconds)
        log.info("Day done: %s | realized %.2f net | %d trades",
                 self.ledger.halted, self.ledger.realized, len(self.ledger.trades))
        return self.ledger

    def _scan_and_enter(self) -> None:
        for cand in scanner.scan(self.cfg, self.broker):
            if self.ledger.halted or len(self.ledger.positions) >= self.cfg.max_positions:
                break
            if _hhmm(self.now()) >= self.cfg.no_new_entries_after:
                break
            sym = cand.symbol
            try:
                sig = self.signals.get(sym, self.day)
            except Exception as exc:
                log.error("%s: agent run failed: %s", sym, exc)
                continue
            self.ledger.signals[sym] = {"rating": sig.rating, "summary": sig.summary}
            self._save()
            direction = risk.direction_for(self.cfg, sig.rating)
            log.info("%s: agent says %s (day %+.2f%%)", sym, sig.rating, cand.change_pct)
            if direction is None:
                continue
            if self.cfg.require_momentum_agreement and (cand.change_pct > 0) != (direction == "LONG"):
                log.info("%s: %s contradicts intraday momentum; skipped", sym, direction)
                continue
            fresh = self.broker.quotes([sym])[sym].ltp  # agent runs take minutes; price has moved
            self.enter(sym, direction, fresh, sig.rating)
            self.tick()  # keep stops honoured between slow agent runs
