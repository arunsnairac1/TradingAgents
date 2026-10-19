"""Deterministic rules between the agent's rating and any order."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .broker import tick_round
from .config import BotConfig
from .costs import net_pnl, round_trip_charges
from .state import Ledger

LONG_RATINGS = {"Buy", "Overweight"}
SHORT_RATINGS = {"Sell", "Underweight"}


@dataclass
class Plan:
    symbol: str
    direction: str
    qty: int
    entry: float
    stop: float
    target: float
    risk: float


def loss_limit(cfg: BotConfig, accumulated: float) -> float:
    return cfg.daily_loss_limit + cfg.rolling_buffer_pct * max(0.0, accumulated)


def direction_for(cfg: BotConfig, rating: str) -> str | None:
    if rating not in cfg.ratings:
        return None
    if rating in LONG_RATINGS:
        return "LONG"
    if rating in SHORT_RATINGS and cfg.allow_short:
        return "SHORT"
    return None


def worst_loss(direction: str, qty: int, entry: float, stop: float) -> float:
    return -net_pnl(direction, qty, entry, stop)


def size(cfg: BotConfig, ledger: Ledger, accumulated: float, symbol: str,
         direction: str, entry: float) -> Plan | str:
    """A Plan, or a string saying why the trade is refused."""
    if ledger.halted:
        return f"day halted ({ledger.halted})"
    if symbol in ledger.traded:
        return "already traded today"
    if len(ledger.positions) >= cfg.max_positions:
        return "max open positions reached"

    budget = loss_limit(cfg, accumulated) + ledger.realized - sum(p.risk for p in ledger.positions.values())
    if budget <= 0:
        return "no daily loss budget left"
    risk = min(cfg.risk_per_trade, budget)

    sign = 1 if direction == "LONG" else -1
    entry = tick_round(entry)
    stop = tick_round(entry * (1 - sign * cfg.stop_loss_pct / 100))
    target = tick_round(entry * (1 + sign * cfg.target_pct / 100))

    qty = min(
        math.floor(risk / abs(entry - stop)),
        math.floor(cfg.capital * cfg.leverage / cfg.max_positions / entry),
    )
    while qty > 0 and worst_loss(direction, qty, entry, stop) > risk:
        qty -= 1  # shave until the loss at the stop, charges included, fits the budget
    if qty < 1:
        return "risk budget too small for one share"

    costs = round_trip_charges(direction, qty, entry, target)
    reward = net_pnl(direction, qty, entry, target)
    if reward <= 0 or reward < cfg.min_reward_to_cost * costs:
        return f"charges eat the target (net {reward:.0f} vs costs {costs:.0f})"
    return Plan(symbol, direction, qty, entry, stop, target, worst_loss(direction, qty, entry, stop))


def day_status(cfg: BotConfig, ledger: Ledger, accumulated: float, unrealized: float) -> str:
    """'' to carry on, else TARGET or LOSS_LIMIT (net of charges, open P&L included)."""
    total = ledger.realized + unrealized
    if total >= cfg.daily_profit_target:
        return "TARGET"
    if total <= -loss_limit(cfg, accumulated):
        return "LOSS_LIMIT"
    return ""
