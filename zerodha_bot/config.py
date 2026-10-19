"""Bot settings, all overridable through ZBOT_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

HOME = Path(os.getenv("ZBOT_HOME") or Path.home() / ".tradingagents" / "zerodha")


@dataclass
class BotConfig:
    # --- mode -------------------------------------------------------------
    # "dry-run" logs the orders it would place; only "live" talks to the order API.
    mode: str = "dry-run"

    # --- capital and daily limits (rupees, net of all charges incl. STT) ---
    capital: float = 100_000.0
    daily_loss_limit: float = 1_000.0     # halt and flatten at -1K net
    daily_profit_target: float = 2_000.0  # halt and flatten at +2K net
    # Rolling mode: the loss allowance grows by this share of profit accumulated on
    # previous days (0 disables). e.g. 0.25 lets 25% of banked profit be risked.
    rolling_buffer_pct: float = 0.0
    leverage: float = 1.0                 # MIS margin you are willing to use (1 = none)

    # --- per-trade rules --------------------------------------------------
    max_positions: int = 3
    risk_per_trade: float = 400.0         # max rupee loss on a single trade at its stop
    stop_loss_pct: float = 0.5            # % from entry
    target_pct: float = 1.0               # % from entry
    min_reward_to_cost: float = 3.0       # net target profit must beat round-trip costs by this
    allow_short: bool = True              # MIS permits intraday shorts

    # --- scanning ---------------------------------------------------------
    universe: str = "NIFTY50"             # NIFTY50 or a comma list of NSE symbols
    scan_top_n: int = 5                   # candidates sent to the (costly) agent run
    min_abs_change_pct: float = 0.3
    min_price: float = 50.0
    min_volume: int = 500_000
    require_momentum_agreement: bool = True

    # --- agent signal -----------------------------------------------------
    # Ratings that open a position: Buy/Sell always; Overweight/Underweight optionally.
    trade_ratings: str = "Buy,Sell"
    llm_provider: str = "anthropic"
    deep_think_llm: str = "claude-sonnet-5"
    quick_think_llm: str = "claude-sonnet-5"

    # --- timing (IST, HH:MM) ----------------------------------------------
    scan_after: str = "09:30"
    no_new_entries_after: str = "14:30"
    squareoff_at: str = "15:15"           # Zerodha auto-squares MIS around 15:20
    poll_seconds: int = 20

    exchange: str = "NSE"
    product: str = "MIS"

    def __post_init__(self):
        if self.mode not in ("dry-run", "live"):
            raise ValueError(f"ZBOT_MODE must be dry-run or live, got {self.mode!r}")
        if self.product != "MIS":
            raise ValueError("this bot is intraday only (MIS)")
        for name in ("capital", "daily_loss_limit", "daily_profit_target", "risk_per_trade",
                     "stop_loss_pct", "target_pct", "leverage"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @property
    def ratings(self) -> set[str]:
        return {r.strip().title() for r in self.trade_ratings.split(",") if r.strip()}

    @classmethod
    def from_env(cls) -> BotConfig:
        values = {}
        for f in fields(cls):
            raw = os.getenv(f"ZBOT_{f.name.upper()}")
            if raw in (None, ""):
                continue
            if f.type in ("bool", bool):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif f.type in ("int", int):
                values[f.name] = int(raw)
            elif f.type in ("float", float):
                values[f.name] = float(raw)
            else:
                values[f.name] = raw
        return cls(**values)
