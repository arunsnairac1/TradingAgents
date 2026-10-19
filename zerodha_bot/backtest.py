"""Replay past days through the real Engine on 1-minute candles.

The Engine, rules, charges and daily limits are the live code; only the broker
(SimBroker) and the clock (SimClock) are simulated, so a backtest cannot drift from
what the bot does. Agent ratings use the *previous* trading day as the as-of date:
TradingAgents serves the as-of day's full daily bar, which on a replayed morning
would be the day's closing price.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .broker import Candle, Fill, Quote
from .config import BotConfig
from .engine import IST, Engine
from .signals import Signal, SignalSource
from .state import Store


class SimClock:
    """Advances one minute per sleep() so the Engine's poll loop walks the day bar by bar."""

    def __init__(self, day: str):
        self.day, self.minute = day, 9 * 60 + 15

    def __call__(self) -> datetime:
        h, m = divmod(self.minute, 60)
        return IST.localize(datetime.strptime(self.day, "%Y-%m-%d").replace(hour=h, minute=m))

    def sleep(self, _seconds: float) -> None:
        self.minute += 1
        if self.minute > 15 * 60 + 30:
            raise RuntimeError("simulated day ran past the close without finishing")


class SimBroker:
    resting_orders = False

    def __init__(self, today: dict[str, list[Candle]], prev_close: dict[str, float],
                 clock: SimClock, slippage_pct: float = 0.0):
        self.today, self.prev_close, self.clock = today, prev_close, clock
        self.slip = slippage_pct / 100
        self.entry_minute: dict[str, int] = {}
        self._n = 0

    def quotes(self, symbols):
        out = {}
        for s in symbols:
            seen = [c for c in self.today.get(s, ()) if c.minute < self.clock.minute]
            if not seen or s not in self.prev_close:
                continue
            last = seen[-1]
            # Only bars that completed since the previous step and after entry: a stop that has
            # since moved (breakeven) must not be tested against dips from before it moved.
            first_new = max(self.entry_minute.get(s, 10**6), self.clock.minute - 1)
            since = [c for c in seen if c.minute >= first_new]
            out[s] = Quote(s, last.close, self.prev_close[s], sum(c.volume for c in seen),
                           low=min(c.low for c in since) if since else None,
                           high=max(c.high for c in since) if since else None,
                           open=last.open,
                           day_high=max(c.high for c in seen), day_low=min(c.low for c in seen))
        return out

    def market_order(self, symbol, side, qty, *, stop_price=None, limit_price=None):
        q = self.quotes([symbol]).get(symbol)
        if q is None:
            raise RuntimeError(f"{symbol}: no candle at this time")
        if stop_price is not None:  # a gap through the stop fills at the open, not the stop
            price = min(stop_price, q.open) if side == "SELL" else max(stop_price, q.open)
        elif limit_price is not None:
            price = limit_price
        else:
            price = q.ltp
        price = round(price * (1 - self.slip if side == "SELL" else 1 + self.slip), 2)
        self.entry_minute[symbol] = self.clock.minute
        self._n += 1
        return Fill(f"sim-{self._n}", price, qty)

    def stop_order(self, *_a):
        return None

    def cancel(self, _order_id):
        return None


class MomentumSignals:
    """Free baseline with no LLM: long what is up on the day, short what is down."""

    def __init__(self, sim: SimBroker):
        self.sim = sim

    def get(self, symbol, day):
        q = self.sim.quotes([symbol])[symbol]
        return Signal("Buy" if q.change_pct > 0 else "Sell", "momentum baseline")


class CachedAgent:
    """Disk-cached agent ratings: each (symbol, as-of day) is paid for once."""

    def __init__(self, agent: SignalSource, cache_dir: Path, max_runs: int | None = None):
        self.agent, self.dir, self.max_runs, self.new_runs = agent, Path(cache_dir), max_runs, 0
        self.dir.mkdir(parents=True, exist_ok=True)

    def exhausted(self) -> bool:
        return self.max_runs is not None and self.new_runs >= self.max_runs

    def get(self, symbol: str, asof: str) -> Signal:
        path = self.dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', symbol)}_{asof}.json"
        if path.exists():
            return Signal(**json.loads(path.read_text()))
        sig = self.agent.get(symbol, asof)
        self.new_runs += 1
        path.write_text(json.dumps({"rating": sig.rating, "summary": sig.summary}))
        return sig


class AsOfSignals:
    def __init__(self, cached: CachedAgent, asof: str):
        self.cached, self.asof = cached, asof

    def get(self, symbol, day):
        return self.cached.get(symbol, self.asof)


@dataclass
class DayResult:
    day: str
    realized: float
    halted: str
    trades: list[dict]


def run_backtest(cfg: BotConfig, candles: dict[str, dict[str, list[Candle]]], start: str, end: str,
                 store_root: Path, agent: CachedAgent | None = None, log=print) -> list[DayResult]:
    all_days = sorted({d for per_symbol in candles.values() for d in per_symbol})
    results: list[DayResult] = []
    for i, day in enumerate(all_days):
        if i == 0 or not (start <= day <= end):
            continue
        if agent is not None and agent.exhausted():
            log(f"Agent run cap reached ({agent.max_runs}); stopping before {day}.")
            break
        prev = all_days[i - 1]
        today = {s: per[day] for s, per in candles.items() if day in per}
        prev_close = {s: per[prev][-1].close for s, per in candles.items() if prev in per}
        clock = SimClock(day)
        sim = SimBroker(today, prev_close, clock, cfg.backtest_slippage_pct)
        signals = MomentumSignals(sim) if agent is None else AsOfSignals(agent, prev)
        ledger = Engine(cfg, sim, signals, Store(store_root), now=clock, sleep=clock.sleep).run()
        results.append(DayResult(day, ledger.realized, ledger.halted, ledger.trades))
        log(f"{day}  net {ledger.realized:+9.2f}  trades {len(ledger.trades)}  ended {ledger.halted}")
    return results


def summarize(results: list[DayResult]) -> dict:
    trades = [t for r in results for t in r.trades]
    pnl = [t["net_pnl"] for t in trades]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    peak = equity = drawdown = 0.0
    for r in results:
        equity += r.realized
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "days": len(results),
        "days_with_trades": sum(1 for r in results if r.trades),
        "trades": len(trades),
        "net_pnl": round(sum(pnl), 2),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
        "max_drawdown": round(drawdown, 2),
        "target_days": sum(1 for r in results if r.halted == "TARGET"),
        "loss_limit_days": sum(1 for r in results if r.halted == "LOSS_LIMIT"),
        "best_day": round(max((r.realized for r in results), default=0.0), 2),
        "worst_day": round(min((r.realized for r in results), default=0.0), 2),
    }


def write_outputs(results: list[DayResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summarize(results), indent=2))
    with open(out_dir / "trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "symbol", "direction", "qty", "entry", "exit", "net_pnl", "reason", "rating"])
        for r in results:
            for t in r.trades:
                w.writerow([r.day, t["symbol"], t["direction"], t["qty"], t["entry"], t["exit"],
                            t["net_pnl"], t["reason"], t["rating"]])


# --- side-by-side comparison -------------------------------------------------

VARIANTS: dict[str, dict] = {
    "baseline (0.5/1.0)": {},
    "wider 0.8/1.6": {"stop_loss_pct": 0.8, "target_pct": 1.6},
    "range stops": {"stop_mode": "range"},
    "breakeven at +0.5%": {"breakeven_trigger_pct": 0.5},
    "max 2 per side": {"max_same_direction": 2},
    "scan at 09:45": {"scan_after": "09:45"},
    "range+breakeven+2/side": {"stop_mode": "range", "breakeven_trigger_pct": 0.5, "max_same_direction": 2},
}


def compare(cfg: BotConfig, candles, start: str, end: str, root: Path, agent: CachedAgent | None = None,
            variants: dict[str, dict] | None = None, log=print) -> dict[str, list[DayResult]]:
    """Run every variant over the same candles (and, with an agent, the same cached ratings)."""
    from dataclasses import replace

    out = {}
    for i, (name, overrides) in enumerate((variants or VARIANTS).items()):
        log(f"--- {name}")
        out[name] = run_backtest(replace(cfg, **overrides), candles, start, end, Path(root) / f"v{i}",
                                 agent, log=lambda *_: None)
    return out


def format_comparison(results: dict[str, list[DayResult]], train_frac: float = 0.6) -> str:
    """Table with the first `train_frac` of days vs. the rest, to expose a rule that only fits the past."""
    header = f"{'variant':26s} {'net':>9s} {'first '+str(round(train_frac*100))+'%':>9s} {'rest':>9s} " \
             f"{'win%':>6s} {'PF':>5s} {'maxDD':>8s} {'trades':>6s}"
    lines = [header, "-" * len(header)]
    for name, rs in results.items():
        cut = int(len(rs) * train_frac)
        s = summarize(rs)
        first, rest = sum(r.realized for r in rs[:cut]), sum(r.realized for r in rs[cut:])
        pf = "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
        lines.append(f"{name:26s} {s['net_pnl']:9.0f} {first:9.0f} {rest:9.0f} "
                     f"{s['win_rate_pct']:6.1f} {pf:>5s} {s['max_drawdown']:8.0f} {s['trades']:6d}")
    return "\n".join(lines)
