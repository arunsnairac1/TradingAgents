"""Per-day ledger persisted as JSON so a restart resumes open positions and limits."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import HOME


@dataclass
class Position:
    symbol: str
    direction: str            # LONG | SHORT
    qty: int
    entry: float
    stop: float
    target: float
    risk: float               # rupees lost if the stop is hit, charges included
    rating: str = ""
    entry_order: str = ""
    stop_order: str | None = None


@dataclass
class Ledger:
    day: str
    realized: float = 0.0     # net of charges
    halted: str = ""          # "", TARGET, LOSS_LIMIT, SQUAREOFF
    positions: dict[str, Position] = field(default_factory=dict)
    traded: list[str] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    signals: dict[str, dict] = field(default_factory=dict)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or HOME)
        (self.root / "days").mkdir(parents=True, exist_ok=True)

    def _day_file(self, day: str) -> Path:
        return self.root / "days" / f"{day}.json"

    def load(self, day: str) -> Ledger:
        try:
            raw = json.loads(self._day_file(day).read_text())
        except (OSError, ValueError):
            return Ledger(day)
        raw["positions"] = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return Ledger(**raw)

    def save(self, ledger: Ledger) -> None:
        tmp = self._day_file(ledger.day).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(ledger), indent=2))
        tmp.replace(self._day_file(ledger.day))

    def accumulated_profit(self, before_day: str) -> float:
        """Net realized P&L across all earlier days (feeds the rolling loss allowance)."""
        total = 0.0
        for path in sorted((self.root / "days").glob("*.json")):
            if path.stem < before_day:
                try:
                    total += json.loads(path.read_text()).get("realized", 0.0)
                except (OSError, ValueError):
                    continue
        return total
