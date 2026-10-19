"""zerodha-bot: login-url | login <request_token> | scan | run | report"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import broker as brk
from .config import HOME, BotConfig


def _build_engine(cfg: BotConfig):
    from .engine import Engine
    from .signals import AgentSignals
    from .state import Store

    live = brk.KiteBroker(cfg.exchange)
    active = live if cfg.mode == "live" else brk.DryRunBroker(live)
    return Engine(cfg, active, AgentSignals(cfg), Store())


def _backtest(cfg: BotConfig, args) -> int:
    from datetime import date, datetime, timedelta

    from . import backtest as bt
    from .universe import resolve

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=28)
    if start > end:
        raise SystemExit("--from must not be after --to")
    candles = brk.KiteHistorical(cfg.exchange).fetch(resolve(cfg.universe), start, end)

    agent = None
    if args.agent:
        from .signals import AgentSignals

        days = sum(1 for d in {d for per in candles.values() for d in per} if start.isoformat() <= d <= end.isoformat())
        print(f"{days} trading days x up to {cfg.scan_top_n} agent runs = up to {days * cfg.scan_top_n} Claude runs "
              f"(cached ones are free; --max-runs caps new ones).")
        if input("Type RUN to continue: ").strip() != "RUN":
            return 1
        agent = bt.CachedAgent(AgentSignals(cfg), HOME / "signals" / cfg.quick_think_llm, args.max_runs)

    name = f"{datetime.now():%Y%m%d-%H%M%S}-{'agent' if agent else 'momentum'}"
    root = HOME / "backtests" / name
    if args.compare:
        runs = bt.compare(cfg, candles, start.isoformat(), end.isoformat(), root, agent)
        print(bt.format_comparison(runs))
        print("Judge variants on the 'rest' column (days they were not picked on), not on 'net'.")
        return 0
    results = bt.run_backtest(cfg, candles, start.isoformat(), end.isoformat(), root, agent)
    bt.write_outputs(results, root)
    print(json.dumps(bt.summarize(results), indent=2))
    print(f"Trades and summary saved in {root}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="zerodha-bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login-url", help="print the Kite login URL (open it, log in, copy request_token from the redirect)")
    login = sub.add_parser("login", help="exchange the request_token for today's access token")
    login.add_argument("request_token")
    sub.add_parser("scan", help="show today's candidates (no agent run, no orders)")
    run = sub.add_parser("run", help="run the trading day")
    run.add_argument("--live", action="store_true", help="send real orders (default is dry-run)")
    sub.add_parser("report", help="print today's ledger")
    bt = sub.add_parser("backtest", help="replay past days on Kite 1-minute candles (no orders)")
    bt.add_argument("--from", dest="start", help="first day YYYY-MM-DD (default: 4 weeks before --to)")
    bt.add_argument("--to", dest="end", help="last day YYYY-MM-DD (default: yesterday)")
    bt.add_argument("--agent", action="store_true",
                    help="use Claude agent ratings (costs tokens); default is the free momentum baseline")
    bt.add_argument("--compare", action="store_true",
                    help="run the built-in rule variants side by side on the same days")
    bt.add_argument("--max-runs", type=int, default=None, help="stop once this many NEW agent runs were made")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = BotConfig.from_env()

    if args.cmd == "login-url":
        print(brk.login_url())
    elif args.cmd == "login":
        brk.exchange_request_token(args.request_token)
        print(f"Access token saved to {brk.TOKEN_FILE} (valid until ~6 AM tomorrow).")
    elif args.cmd == "scan":
        from . import scanner
        for c in scanner.scan(cfg, brk.KiteBroker(cfg.exchange)):
            print(f"{c.symbol:12s} {c.quote.ltp:10.2f} {c.change_pct:+6.2f}%  vol {c.quote.volume:,}")
    elif args.cmd == "run":
        if args.live:
            cfg.mode = "live"
        if cfg.mode == "live":
            print(f"LIVE: real orders, capital {cfg.capital:,.0f}, loss limit {cfg.daily_loss_limit:,.0f}, "
                  f"target {cfg.daily_profit_target:,.0f}.")
            if input("Type LIVE to continue: ").strip() != "LIVE":
                return 1
        _build_engine(cfg).run()
    elif args.cmd == "backtest":
        return _backtest(cfg, args)
    elif args.cmd == "report":
        from datetime import datetime

        from .engine import IST
        from .state import Store
        day = datetime.now(IST).strftime("%Y-%m-%d")
        ledger = Store().load(day)
        print(json.dumps({"day": day, "realized_net": round(ledger.realized, 2), "halted": ledger.halted,
                          "open": list(ledger.positions), "trades": ledger.trades}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
