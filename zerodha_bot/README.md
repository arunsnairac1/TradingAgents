# Zerodha intraday bot

Scans Nifty 50 for movers, asks TradingAgents (Claude) for a rating on each,
and trades it through Kite Connect under fixed rules. Intraday (MIS) only.

**Dry-run is the default.** Nothing reaches the exchange until you pass `--live` and type `LIVE`.

## Setup
```bash
pip install ".[zerodha]"
export KITE_API_KEY=... KITE_API_SECRET=... ANTHROPIC_API_KEY=...
```
Each trading day, before 09:30 IST (Kite tokens expire ~6 AM):
```bash
zerodha-bot login-url            # open it, log in, copy request_token from the redirect URL
zerodha-bot login <request_token>
zerodha-bot scan                 # optional: see candidates, no agent run, no orders
zerodha-bot run                  # dry-run
zerodha-bot run --live           # real orders
zerodha-bot report
```
Alternatively export `KITE_ACCESS_TOKEN` and skip `login`. Your Zerodha password/TOTP are never handled by the bot.

## Rules (all `ZBOT_*` env overrides, see `config.py`)
| Rule | Default |
|---|---|
| Daily loss limit / profit target (net of brokerage, STT, GST, etc.) | ₹1,000 / ₹2,000, open P&L included |
| On hitting either | flatten everything, stop for the day |
| Rolling mode | `ZBOT_ROLLING_BUFFER_PCT`: loss allowance grows by that share of profit banked on earlier days (0 = off) |
| Max open positions / risk per trade | 3 / ₹400 at the stop, charges included |
| Stop / target | 0.5% / 1.0% from the actual fill |
| Opens a trade on | agent rating Buy (long) or Sell (short) only; Hold/Overweight/Underweight/REVIEW do nothing |
| Momentum filter | agent direction must match the stock's move on the day |
| Charges filter | skip if net target profit < 3x round-trip charges |
| Timing (IST) | scan 09:30, no entries after 14:30, square off 15:15 |
| Orders | marketable LIMIT (0.2% band) plus an exchange-side stop-limit as a crash safety net |

State lives in `~/.tradingagents/zerodha/days/<date>.json`; restarting mid-day resumes open positions and limits.

## Read before going live
- **Position size vs. your targets.** With `ZBOT_LEVERAGE=1`, each of 3 slots is about ₹33K, so a 1% target earns about ₹300 net. Reaching ₹2K a day needs several winners or more leverage; raising leverage raises how fast ₹1K of losses arrives, but the loss limit and per-trade risk still cap it. The bot does not tune this for you.
- **Agent output is non-deterministic** and was built around daily data, not intraday. Treat its rating as one input; the rules, not the agent, protect you. Each candidate is a full multi-agent run (minutes and real Claude spend).
- **Not tested against a real Zerodha account.** Unit tests use fakes. Do the first live days with tiny `ZBOT_CAPITAL`/`ZBOT_RISK_PER_TRADE`.
- **Gaps and slippage** can exceed your stop; limits are enforced by polling every `ZBOT_POLL_SECONDS` (20s) plus the exchange stop-limit.
- **Charges** in `costs.py` are the published schedule at time of writing; verify at zerodha.com/charges.
- **Compliance/tax:** API order rules (e.g. static IP registration with Zerodha) and tax treatment (intraday equity is generally speculative business income, not STCG) are yours to confirm with Zerodha and a CA.
- This is software, not financial advice. You can lose money.
