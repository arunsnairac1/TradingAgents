import pytest

from zerodha_bot import backtest as bt
from zerodha_bot.broker import Candle
from zerodha_bot.config import BotConfig
from zerodha_bot.signals import Signal

pytestmark = pytest.mark.unit


def flat(price, start=9 * 60 + 15, end=15 * 60 + 30, vol=100_000):
    return [Candle(m, price, price, price, price, vol) for m in range(start, end)]


def with_bar(candles, minute, **kw):
    out = []
    for c in candles:
        if c.minute == minute:
            d = c._asdict() | kw
            c = Candle(**d)
        out.append(c)
    return out


def cfg(**kw):
    return BotConfig(universe="AAA", min_volume=1, backtest_slippage_pct=0.0, **kw)


def one_stock(day1, day2):
    return {"AAA": {"2026-10-15": day1, "2026-10-16": day2}}


def run(candles, agent=None, **kw):
    return bt.run_backtest(cfg(**kw), candles, "2026-10-16", "2026-10-16", None_root(), agent, log=lambda *_: None)


_root = []


@pytest.fixture(autouse=True)
def _tmp(tmp_path):
    _root.clear()
    _root.append(tmp_path)


def None_root():
    return _root[0]


def test_target_hit_inside_a_bar_fills_at_the_target():
    day1 = flat(1000.0)
    day2 = flat(1010.0)  # +1% on the day: momentum says Buy
    day2 = with_bar(day2, 11 * 60, high=1021.0, close=1020.0)  # trades through the 1% target
    [res] = run(one_stock(day1, day2))
    [t] = res.trades
    assert t["direction"] == "LONG" and t["reason"] == "TARGET"
    assert t["entry"] == 1010.0 and t["exit"] == pytest.approx(1020.1, abs=0.01)
    assert t["net_pnl"] > 0


def test_bar_touching_both_levels_counts_as_stop():
    day2 = with_bar(flat(1010.0), 11 * 60, high=1025.0, low=1000.0)
    [res] = run(one_stock(flat(1000.0), day2))
    assert res.trades[0]["reason"] == "STOP"
    assert res.trades[0]["net_pnl"] < 0


def test_gap_through_stop_fills_at_the_open():
    day2 = with_bar(flat(1010.0), 11 * 60, open=990.0, high=990.0, low=985.0, close=988.0)
    [res] = run(one_stock(flat(1000.0), day2))
    t = res.trades[0]
    assert t["reason"] == "STOP" and t["exit"] == 990.0


def test_entry_bar_extremes_before_the_entry_are_ignored():
    # The 09:29 bar has a huge low, but the entry happens after it: no phantom stop.
    day2 = with_bar(flat(1010.0), 9 * 60 + 29, low=900.0)
    [res] = run(one_stock(flat(1000.0), day2))
    assert res.trades[0]["reason"] == "SQUAREOFF"


def test_unchanged_day_ends_squared_off_with_no_trades_when_below_move_threshold():
    [res] = run(one_stock(flat(1000.0), flat(1000.0)))
    assert res.trades == [] and res.realized == 0


def test_slippage_hurts_both_sides():
    day2 = with_bar(flat(1010.0), 11 * 60, high=1030.0, close=1025.0)
    c = BotConfig(universe="AAA", min_volume=1, backtest_slippage_pct=0.1)
    [res] = bt.run_backtest(c, one_stock(flat(1000.0), day2), "2026-10-16", "2026-10-16",
                            None_root(), None, log=lambda *_: None)
    t = res.trades[0]
    assert t["entry"] == pytest.approx(1010.0 * 1.001, abs=0.01)
    assert t["exit"] == pytest.approx(1011.01 * 1.01 * 0.999, abs=0.02)


class CountingAgent:
    def __init__(self, rating="Buy"):
        self.rating, self.calls = rating, []

    def get(self, symbol, day):
        self.calls.append((symbol, day))
        return Signal(self.rating, "x")


def test_agent_is_asked_as_of_the_previous_trading_day_and_cached(tmp_path):
    inner = CountingAgent()
    cached = bt.CachedAgent(inner, tmp_path / "sig")
    candles = one_stock(flat(1000.0), flat(1010.0))
    run(candles, agent=cached)
    assert inner.calls == [("AAA", "2026-10-15")]  # not the replayed day: its close would leak
    _root[0] = tmp_path / "again"
    run(candles, agent=cached)
    assert len(inner.calls) == 1  # second pass served from disk


def test_run_cap_stops_before_the_next_day(tmp_path):
    inner = CountingAgent("Hold")
    cached = bt.CachedAgent(inner, tmp_path / "sig", max_runs=1)
    candles = {"AAA": {"2026-10-14": flat(1000.0), "2026-10-15": flat(1010.0), "2026-10-16": flat(1020.0)}}
    results = bt.run_backtest(cfg(), candles, "2026-10-15", "2026-10-16", tmp_path / "r", cached, log=lambda *_: None)
    assert [r.day for r in results] == ["2026-10-15"]


def test_summary_math():
    rs = [bt.DayResult("d1", 500.0, "", [{"net_pnl": 700.0}, {"net_pnl": -200.0}]),
          bt.DayResult("d2", -300.0, "", [{"net_pnl": -300.0}]),
          bt.DayResult("d3", 100.0, "TARGET", [{"net_pnl": 100.0}])]
    s = bt.summarize(rs)
    assert s["net_pnl"] == 300.0 and s["trades"] == 4 and s["win_rate_pct"] == 50.0
    assert s["max_drawdown"] == 300.0 and s["target_days"] == 1
    assert s["profit_factor"] == pytest.approx(800 / 500)


def test_compare_runs_each_variant_on_the_same_days(tmp_path):
    day2 = with_bar(flat(1010.0), 11 * 60, high=1021.0, close=1020.0)
    variants = {"base": {}, "wide": {"stop_loss_pct": 2.0, "target_pct": 0.8}}
    out = bt.compare(cfg(), one_stock(flat(1000.0), day2), "2026-10-16", "2026-10-16",
                     tmp_path, None, variants)
    assert set(out) == set(variants)
    assert out["base"][0].trades[0]["reason"] == "TARGET"
    table = bt.format_comparison(out)
    assert "base" in table and "wide" in table and "maxDD" in table
