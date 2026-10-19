"""The TradingAgents decision for one symbol, via Claude."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .config import BotConfig

log = logging.getLogger("zbot.signals")


@dataclass
class Signal:
    rating: str          # Buy / Overweight / Hold / Underweight / Sell / REVIEW
    summary: str = ""


class SignalSource(Protocol):
    def get(self, symbol: str, day: str) -> Signal: ...


class AgentSignals:
    """Runs the full multi-agent pipeline on `<SYMBOL>.NS`. One run costs real LLM tokens."""

    def __init__(self, cfg: BotConfig):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        config.update(
            llm_provider=cfg.llm_provider,
            deep_think_llm=cfg.deep_think_llm,
            quick_think_llm=cfg.quick_think_llm,
        )
        self.graph = TradingAgentsGraph(debug=False, config=config)

    def get(self, symbol, day):
        state, rating = self.graph.propagate(f"{symbol}.NS", day)
        return Signal(rating, str(state.get("final_trade_decision", ""))[:2000])
