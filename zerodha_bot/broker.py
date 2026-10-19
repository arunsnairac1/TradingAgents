"""Broker layer: Kite Connect (live), a dry-run wrapper, and the daily-login helpers.

``kiteconnect`` is imported lazily so the rest of the bot (and its tests) run
without it installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NamedTuple, Protocol

from .config import HOME

log = logging.getLogger("zbot.broker")
TOKEN_FILE = HOME / "access_token.json"


@dataclass
class Quote:
    symbol: str
    ltp: float
    prev_close: float
    volume: int
    # Backtest only: extremes since entry and the latest bar's open, so stops and
    # targets that trade inside a bar are seen. None (live) means "use ltp".
    low: float | None = None
    high: float | None = None
    open: float | None = None
    day_high: float | None = None
    day_low: float | None = None

    @property
    def range_pct(self) -> float | None:
        if self.day_high is None or self.day_low is None or not self.ltp:
            return None
        return (self.day_high - self.day_low) / self.ltp * 100

    @property
    def change_pct(self) -> float:
        return (self.ltp - self.prev_close) / self.prev_close * 100 if self.prev_close else 0.0


@dataclass
class Fill:
    order_id: str
    price: float
    qty: int


class Candle(NamedTuple):
    minute: int   # minutes since midnight IST of the bar's start
    open: float
    high: float
    low: float
    close: float
    volume: int


class Broker(Protocol):
    resting_orders: bool

    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def market_order(self, symbol: str, side: str, qty: int, *,
                     stop_price: float | None = None, limit_price: float | None = None) -> Fill: ...
    def stop_order(self, symbol: str, side: str, qty: int, trigger: float) -> str | None: ...
    def cancel(self, order_id: str) -> None: ...


def tick_round(price: float) -> float:
    return round(round(price / 0.05) * 0.05, 2)


# --- daily login -----------------------------------------------------------

def _kite_class():
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("kiteconnect is not installed: pip install 'tradingagents[zerodha]'") from exc
    return KiteConnect


def login_url() -> str:
    return _kite_class()(api_key=os.environ["KITE_API_KEY"]).login_url()


def exchange_request_token(request_token: str) -> str:
    """Trade the one-time request_token from the Kite redirect for today's access token."""
    kite = _kite_class()(api_key=os.environ["KITE_API_KEY"])
    session = kite.generate_session(request_token, api_secret=os.environ["KITE_API_SECRET"])
    token = session["access_token"]
    HOME.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"date": date.today().isoformat(), "access_token": token}))
    TOKEN_FILE.chmod(0o600)
    return token


def todays_access_token() -> str:
    """KITE_ACCESS_TOKEN wins; otherwise the token saved by `login` earlier today."""
    if os.getenv("KITE_ACCESS_TOKEN"):
        return os.environ["KITE_ACCESS_TOKEN"]
    try:
        saved = json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    if saved.get("date") == date.today().isoformat():
        return saved["access_token"]
    raise SystemExit("No Kite access token for today. Run: zerodha-bot login-url, then zerodha-bot login <request_token>")


# --- brokers ---------------------------------------------------------------

class KiteBroker:
    """Live quotes and orders. Entries/exits are marketable LIMIT orders: Kite's API
    restricts plain MARKET orders on many equities."""

    resting_orders = True

    def __init__(self, exchange: str = "NSE", limit_buffer_pct: float = 0.2, fill_timeout: float = 15.0):
        kite = _kite_class()(api_key=os.environ["KITE_API_KEY"])
        kite.set_access_token(todays_access_token())
        self.kite, self.exchange = kite, exchange
        self.buffer, self.fill_timeout = limit_buffer_pct / 100, fill_timeout

    def quotes(self, symbols):
        raw = self.kite.quote([f"{self.exchange}:{s}" for s in symbols])
        out = {}
        for key, q in raw.items():
            sym = key.split(":", 1)[1]
            out[sym] = Quote(sym, float(q["last_price"]), float(q["ohlc"]["close"]), int(q.get("volume", 0)),
                             day_high=float(q["ohlc"]["high"]), day_low=float(q["ohlc"]["low"]))
        return out

    def _place(self, symbol, side, qty, order_type, **kw):
        return self.kite.place_order(
            variety="regular", exchange=self.exchange, tradingsymbol=symbol,
            transaction_type=side, quantity=qty, product="MIS", order_type=order_type,
            validity="DAY", tag="zbot", **kw,
        )

    def market_order(self, symbol, side, qty, *, stop_price=None, limit_price=None):
        ltp = self.quotes([symbol])[symbol].ltp
        limit = ltp * (1 + self.buffer) if side == "BUY" else ltp * (1 - self.buffer)
        order_id = self._place(symbol, side, qty, "LIMIT", price=tick_round(limit))
        deadline = time.monotonic() + self.fill_timeout
        while time.monotonic() < deadline:
            last = self.kite.order_history(order_id)[-1]
            if last["status"] == "COMPLETE":
                return Fill(order_id, float(last["average_price"]), int(last["filled_quantity"]))
            if last["status"] in ("REJECTED", "CANCELLED"):
                raise RuntimeError(f"{symbol} {side} {last['status']}: {last.get('status_message')}")
            time.sleep(0.5)
        self.kite.cancel_order(variety="regular", order_id=order_id)
        raise RuntimeError(f"{symbol} {side} not filled within {self.fill_timeout}s; cancelled")

    def stop_order(self, symbol, side, qty, trigger):
        # Exchange-side safety net in case this process dies; stop-limit with a 0.3% band.
        band = trigger * 0.003
        price = trigger - band if side == "SELL" else trigger + band
        return self._place(symbol, side, qty, "SL", price=tick_round(price), trigger_price=tick_round(trigger))

    def cancel(self, order_id):
        self.kite.cancel_order(variety="regular", order_id=order_id)


class DryRunBroker:
    """Real quotes (from `data`), simulated fills at last price, nothing sent to the exchange."""

    resting_orders = False

    def __init__(self, data: Broker):
        self.data, self._n = data, 0

    def quotes(self, symbols):
        return self.data.quotes(symbols)

    def market_order(self, symbol, side, qty, *, stop_price=None, limit_price=None):
        self._n += 1
        price = self.data.quotes([symbol])[symbol].ltp
        log.info("[dry-run] %s %s x%d @ %.2f", side, symbol, qty, price)
        return Fill(f"dry-{self._n}", price, qty)

    def stop_order(self, symbol, side, qty, trigger):
        return None

    def cancel(self, order_id):
        return None


class KiteHistorical:
    """Past 1-minute candles from Kite, cached on disk (needs the Connect plan's historical API)."""

    OPEN, CLOSE = 9 * 60 + 15, 15 * 60 + 30

    def __init__(self, exchange: str = "NSE", pause: float = 0.4, window_days: int = 55):
        kite = _kite_class()(api_key=os.environ["KITE_API_KEY"])
        kite.set_access_token(todays_access_token())
        self.kite, self.exchange, self.pause, self.window = kite, exchange, pause, window_days
        self.cache = HOME / "hist" / exchange
        self._tokens: dict[str, int] | None = None

    def _token(self, symbol: str) -> int | None:
        if self._tokens is None:
            self._tokens = {i["tradingsymbol"]: i["instrument_token"]
                            for i in self.kite.instruments(self.exchange) if i.get("instrument_type") == "EQ"}
        return self._tokens.get(symbol)

    def _window(self, symbol: str, token: int, lo: date, hi: date) -> dict[str, list[list]]:
        path = self.cache / f"{re.sub(r'[^A-Za-z0-9_-]', '_', symbol)}_{lo}_{hi}.json"
        if path.exists():
            return json.loads(path.read_text())
        out: dict[str, list[list]] = {}
        for attempt in range(3):
            try:
                rows = self.kite.historical_data(token, datetime.combine(lo, datetime.min.time()),
                                                 datetime.combine(hi, datetime.max.time()), "minute")
                break
            except Exception as exc:  # rate limit / transient
                log.warning("%s %s..%s attempt %d failed: %s", symbol, lo, hi, attempt + 1, exc)
                time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"historical data for {symbol} {lo}..{hi} failed")
        time.sleep(self.pause)
        for r in rows:
            d = r["date"]
            minute = d.hour * 60 + d.minute
            if self.OPEN <= minute < self.CLOSE:
                out.setdefault(d.strftime("%Y-%m-%d"), []).append(
                    [minute, r["open"], r["high"], r["low"], r["close"], r.get("volume", 0)])
        if hi < date.today():  # never freeze a still-forming day
            self.cache.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out))
        return out

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, dict[str, list[Candle]]]:
        """{symbol: {day: [Candle, ...]}} covering a week before `start` (for prior closes) to `end`."""
        result: dict[str, dict[str, list[Candle]]] = {}
        for symbol in symbols:
            token = self._token(symbol)
            if token is None:
                log.warning("%s: no NSE equity instrument; skipped", symbol)
                continue
            days: dict[str, list[Candle]] = {}
            cur = start - timedelta(days=7)
            while cur <= end:
                hi = min(cur + timedelta(days=self.window - 1), end)
                for day, rows in self._window(symbol, token, cur, hi).items():
                    days[day] = [Candle(*r) for r in rows]
                cur = hi + timedelta(days=1)
            result[symbol] = days
            log.info("%s: %d days of candles", symbol, len(days))
        return result
