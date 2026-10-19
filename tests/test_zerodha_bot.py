from datetime import datetime

import pytest

from zerodha_bot import costs, risk
from zerodha_bot.broker import DryRunBroker, Fill, Quote
from zerodha_bot.config import BotConfig
from zerodha_bot.engine import IST, Engine
from zerodha_bot.signals import Signal
from zerodha_bot.state import Ledger, Position, Store

pytestmark = pytest.mark.unit


class FakeBroker:
    resting_orders = False

    def __init__(self, prices, prev=None, volume=5_000_000):
        self.prices, self.prev, self.volume = dict(prices), prev or {}, volume
        self.orders = []

    def quotes(self, symbols):
        return {s: Quote(s, self.prices[s], self.prev.get(s, self.prices[s]), self.volume) for s in symbols if s in self.prices}

    def market_order(self, symbol, side, qty):
        self.orders.append((symbol, side, qty))
        return Fill(f"o{len(self.orders)}", self.prices[symbol], qty)

    def stop_order(self, *a):
        return None

    def cancel(self, order_id):
        pass


class FakeSignals:
    def __init__(self, ratings):
        self.ratings = ratings

    def get(self, symbol, day):
        return Signal(self.ratings.get(symbol, "Hold"))


class Clock:
    def __init__(self, hhmm="09:31"):
        self.t = hhmm

    def __call__(self):
        h, m = map(int, self.t.split(":"))
        return IST.localize(datetime(2026, 10, 19, h, m))


def cfg(**kw):
    return BotConfig(universe="AAA,BBB", min_volume=1, **kw)


def test_charges_match_zerodha_schedule():
    # Rs 1L buy + sell at the same price: brokerage 2x Rs 30, STT 25 on sell side only.
    total = costs.round_trip_charges("LONG", 100, 1000, 1000)
    assert 80 < total < 100
    assert costs.order_charges("BUY", 100, 1000) < costs.order_charges("SELL", 100, 1000)


def test_sizing_respects_per_trade_risk_including_charges():
    plan = risk.size(cfg(), Ledger("d"), 0.0, "AAA", "LONG", 1000.0)
    assert not isinstance(plan, str)
    assert plan.risk <= 400
    assert plan.qty * plan.entry <= 100_000 / 3 + plan.entry
    assert plan.stop < plan.entry < plan.target


def test_short_stop_and_target_are_mirrored():
    plan = risk.size(cfg(), Ledger("d"), 0.0, "AAA", "SHORT", 1000.0)
    assert plan.target < plan.entry < plan.stop


def test_loss_budget_shrinks_with_open_risk_and_realized_loss():
    ledger = Ledger("d", realized=-700.0)
    plan = risk.size(cfg(), ledger, 0.0, "AAA", "LONG", 1000.0)
    assert plan.risk <= 300
    ledger.realized = -1000.0
    assert "budget" in risk.size(cfg(), ledger, 0.0, "AAA", "LONG", 1000.0)


def test_rolling_buffer_grows_limit_with_accumulated_profit():
    assert risk.loss_limit(cfg(), 5000) == 1000
    assert risk.loss_limit(cfg(rolling_buffer_pct=0.2), 5000) == 2000
    assert risk.loss_limit(cfg(rolling_buffer_pct=0.2), -5000) == 1000


def test_refusals():
    c = cfg(max_positions=1)
    ledger = Ledger("d", positions={"X": Position("X", "LONG", 1, 1, 1, 1, 1)})
    assert "max open" in risk.size(c, ledger, 0, "AAA", "LONG", 1000)
    assert "already traded" in risk.size(c, Ledger("d", traded=["AAA"]), 0, "AAA", "LONG", 1000)
    assert "halted" in risk.size(c, Ledger("d", halted="TARGET"), 0, "AAA", "LONG", 1000)
    # Tiny move: charges swallow the target.
    assert "charges" in risk.size(cfg(target_pct=0.05), Ledger("d"), 0, "AAA", "LONG", 1000)


def test_ratings_map_to_directions():
    c = cfg()
    assert risk.direction_for(c, "Buy") == "LONG"
    assert risk.direction_for(c, "Sell") == "SHORT"
    assert risk.direction_for(c, "Overweight") is None  # not in trade_ratings by default
    assert risk.direction_for(c, "Hold") is None
    assert risk.direction_for(c, "REVIEW") is None
    assert risk.direction_for(cfg(allow_short=False), "Sell") is None


def test_day_status_net_of_unrealized():
    c = cfg()
    assert risk.day_status(c, Ledger("d", realized=500), 0, 100) == ""
    assert risk.day_status(c, Ledger("d", realized=500), 0, 1600) == "TARGET"
    assert risk.day_status(c, Ledger("d", realized=-400), 0, -700) == "LOSS_LIMIT"


def _engine(tmp_path, broker, ratings, clock):
    return Engine(cfg(), broker, FakeSignals(ratings), Store(tmp_path), now=clock, sleep=lambda s: None)


def test_full_day_target_hit_then_squareoff(tmp_path):
    broker = FakeBroker({"AAA": 1000.0, "BBB": 1000.0}, prev={"AAA": 980.0, "BBB": 1020.0})
    clock = Clock()
    eng = _engine(tmp_path, broker, {"AAA": "Buy", "BBB": "Sell"}, clock)
    eng._scan_and_enter()
    assert set(eng.ledger.positions) == {"AAA", "BBB"}
    broker.prices["AAA"] = 1010.0  # +1% target
    eng.tick()
    assert "AAA" not in eng.ledger.positions
    assert eng.ledger.trades[0]["reason"] == "TARGET"
    clock.t = "15:16"
    eng.tick()
    assert not eng.ledger.positions and eng.ledger.halted
    assert Store(tmp_path).load("2026-10-19").realized == pytest.approx(eng.ledger.realized)


def test_stop_hit_closes_position_but_day_continues(tmp_path):
    broker = FakeBroker({"AAA": 1000.0}, prev={"AAA": 980.0})
    eng = _engine(tmp_path, broker, {"AAA": "Buy"}, Clock())
    eng._scan_and_enter()
    broker.prices["AAA"] = 994.0  # through the 0.5% stop
    assert eng.tick() is True
    assert eng.ledger.trades[0]["reason"] == "STOP"
    assert -400 < eng.ledger.realized < 0 and not eng.ledger.halted


def test_loss_limit_flattens_and_halts(tmp_path):
    broker = FakeBroker({"AAA": 1000.0}, prev={"AAA": 980.0})
    eng = _engine(tmp_path, broker, {"AAA": "Buy"}, Clock())
    eng._scan_and_enter()
    eng.ledger.realized = -950.0  # earlier losses today
    broker.prices["AAA"] = 997.0  # open loss pushes the net past -1000
    assert eng.tick() is False
    assert eng.ledger.halted == "LOSS_LIMIT" and not eng.ledger.positions
    assert broker.orders[-1][1] == "SELL"


def test_momentum_disagreement_and_hold_place_no_orders(tmp_path):
    broker = FakeBroker({"AAA": 1000.0, "BBB": 1000.0}, prev={"AAA": 1020.0, "BBB": 980.0})
    eng = _engine(tmp_path, broker, {"AAA": "Buy", "BBB": "Hold"}, Clock())  # AAA is down on the day
    eng._scan_and_enter()
    assert broker.orders == []


def test_no_entries_after_cutoff(tmp_path):
    broker = FakeBroker({"AAA": 1000.0}, prev={"AAA": 980.0})
    eng = _engine(tmp_path, broker, {"AAA": "Buy"}, Clock("14:45"))
    eng._scan_and_enter()
    assert broker.orders == []


def test_restart_resumes_open_positions(tmp_path):
    broker = FakeBroker({"AAA": 1000.0}, prev={"AAA": 980.0})
    clock = Clock()
    _engine(tmp_path, broker, {"AAA": "Buy"}, clock)._scan_and_enter()
    again = _engine(tmp_path, broker, {"AAA": "Buy"}, clock)
    assert "AAA" in again.ledger.positions and again.ledger.traded == ["AAA"]


def test_dry_run_broker_never_hits_order_api():
    data = FakeBroker({"AAA": 1000.0})
    dry = DryRunBroker(data)
    fill = dry.market_order("AAA", "BUY", 10)
    assert fill.price == 1000.0 and data.orders == []
    assert dry.stop_order("AAA", "SELL", 10, 995) is None


def test_live_mode_rejects_bad_config():
    with pytest.raises(ValueError):
        BotConfig(mode="yolo")
    with pytest.raises(ValueError):
        BotConfig(product="CNC")
