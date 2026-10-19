"""Pre-filter the universe by liquidity and intraday movement before paying for agent runs."""

from __future__ import annotations

from dataclasses import dataclass

from .broker import Broker, Quote
from .config import BotConfig
from .universe import resolve


@dataclass
class Candidate:
    symbol: str
    quote: Quote

    @property
    def change_pct(self) -> float:
        return self.quote.change_pct


def scan(cfg: BotConfig, broker: Broker) -> list[Candidate]:
    quotes = broker.quotes(resolve(cfg.universe))
    picks = [
        Candidate(sym, q) for sym, q in quotes.items()
        if q.ltp >= cfg.min_price and q.volume >= cfg.min_volume
        and abs(q.change_pct) >= cfg.min_abs_change_pct
    ]
    # Biggest movers on the day first; they have the range to clear the charges.
    picks.sort(key=lambda c: abs(c.change_pct), reverse=True)
    return picks[: cfg.scan_top_n]
