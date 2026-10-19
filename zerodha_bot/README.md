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

## Backtest
```bash
zerodha-bot backtest --from 2026-09-01 --to 2026-10-16            # free momentum baseline, no LLM
zerodha-bot backtest --from 2026-09-01 --to 2026-10-16 --agent --max-runs 60   # Claude ratings
```
Replays each past day minute by minute through the same engine, rules, charges and daily limits as live
(only the broker and clock are simulated), using Kite 1-minute candles (needs the Connect plan's historical API;
candles are cached under `~/.tradingagents/zerodha/hist`). Prints win rate, profit factor, max drawdown and days
that hit the target or loss limit; trades go to `trades.csv` beside `summary.json` in `~/.tradingagents/zerodha/backtests/`.

- **Run the baseline first.** It is free and shows whether the rules make money without the agent; `--agent` is only
  interesting if it beats that.
- Agent ratings are cached per (symbol, day), so changing stops, targets or leverage and re-running costs nothing.
  `--max-runs` caps new Claude runs (checked at each day's start, so it can overshoot by up to `ZBOT_SCAN_TOP_N`).
- The agent is asked as of the *previous* trading day, because TradingAgents serves the as-of day's whole daily bar,
  which on a replayed morning would be that day's close.
- Claude may remember real market moves before its training cutoff, which flatters results on older dates.
- A bar that touches both stop and target counts as the stop; a gap through the stop fills at the open;
  `ZBOT_BACKTEST_SLIPPAGE_PCT` (0.03) is charged against you on every fill. Real fills can still be worse.
- Scan inputs are approximate: volume is cumulative to the scan time, and the universe is today's Nifty 50.

### Comparing rule variants
```bash
zerodha-bot backtest --from 2026-04-01 --to 2026-10-16 --compare
```
Runs the built-in variants (wider stops, volatility-scaled stops, breakeven stop, max 2 per side, later scan) on the
same days and prints one table. The `first 60%` / `rest` columns split the days: **pick on `rest`**, not `net`, or you
are just fitting the past. Use a long window (months, not weeks); candles are cached after the first download.
All the variant knobs are off by default, so live behaviour only changes when you turn one on:
`ZBOT_STOP_MODE=range`, `ZBOT_BREAKEVEN_TRIGGER_PCT=0.5`, `ZBOT_MAX_SAME_DIRECTION=2`, `ZBOT_SCAN_AFTER=09:45`.

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
