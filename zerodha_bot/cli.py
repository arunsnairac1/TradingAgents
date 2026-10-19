"""zerodha-bot: login-url | login <request_token> | scan | run | report"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import broker as brk
from .config import BotConfig


def _build_engine(cfg: BotConfig):
    from .engine import Engine
    from .signals import AgentSignals
    from .state import Store

    live = brk.KiteBroker(cfg.exchange)
    active = live if cfg.mode == "live" else brk.DryRunBroker(live)
    return Engine(cfg, active, AgentSignals(cfg), Store())


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
