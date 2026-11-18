"""Tes backtester dengan candle sintetis (tanpa jaringan)."""

import sys
from pathlib import Path

import pytest

ORCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH / "src"))
sys.path.insert(0, str(ORCH))

import backtest as bt  # noqa: E402


def _trend_then_drop(n_up=40, n_down=40, start=69_000_000.0, step=0.004):
    candles = []
    price = start
    t = 1_700_000_000
    for i in range(n_up):
        price *= (1 + step)
        candles.append({"t": t + i * 60, "o": price, "h": price * 1.001,
                        "l": price * 0.999, "c": price})
    for i in range(n_down):
        price *= (1 - step * 1.2)
        candles.append({"t": t + (n_up + i) * 60, "o": price, "h": price * 1.001,
                        "l": price * 0.999, "c": price})
    return candles


def test_backtest_returns_result_structure():
    res = bt.run_backtest(_trend_then_drop(), "PAXG/IDR", cost_pct=0.0074,
                          capital=15_000.0, use_trailing=True,
                          trailing_arm=0.012, trailing_gap=0.004,
                          min_net_rr=1.5, max_spread=0.0035)
    assert isinstance(res, bt.Result)
    assert res.signals >= 0


def test_summarize_empty_is_safe():
    stats = bt.summarize(bt.Result())
    assert stats["trades"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["breakeven_wr"] == 0.0


def test_no_trade_when_price_flat():
    flat = [{"t": 1_700_000_000 + i * 60, "o": 69_000_000.0, "h": 69_000_000.0,
             "l": 69_000_000.0, "c": 69_000_000.0} for i in range(200)]
    res = bt.run_backtest(flat, "PAXG/IDR", cost_pct=0.0074, capital=15_000.0,
                          use_trailing=True, trailing_arm=0.012, trailing_gap=0.004,
                          min_net_rr=1.5, max_spread=0.0035)
    assert res.trades == []


def test_every_trade_has_valid_exit():
    res = bt.run_backtest(_trend_then_drop(), "PAXG/IDR", cost_pct=0.0074,
                          capital=15_000.0, use_trailing=True,
                          trailing_arm=0.012, trailing_gap=0.004,
                          min_net_rr=1.5, max_spread=0.0035)
    for t in res.trades:
        assert t.exit_reason in {"TAKE_PROFIT_ATR", "STOP_LOSS_ATR", "TRAILING_STOP_LOSS"}
        assert t.exit_ts > t.entry_ts
        assert t.volume > 0


def test_pnl_includes_cost():
    res = bt.run_backtest(_trend_then_drop(), "PAXG/IDR", cost_pct=0.0074,
                          capital=15_000.0, use_trailing=False,
                          trailing_arm=0.012, trailing_gap=0.004,
                          min_net_rr=1.5, max_spread=0.0035)
    for t in res.trades:
        gross = (t.exit_price - t.entry_price) * t.volume
        assert t.pnl == pytest.approx(gross - (t.entry_price + t.exit_price) * t.volume * 0.0037, abs=0.02)


def test_summarize_computes_win_rate_and_expectancy():
    res = bt.Result(trades=[
        bt.Trade(1, 100.0, 1.0, 110.0, 95.0, 100.0, exit_ts=2, exit_price=110.0,
                 exit_reason="TAKE_PROFIT_ATR", pnl=10.0),
        bt.Trade(3, 100.0, 1.0, 110.0, 95.0, 100.0, exit_ts=4, exit_price=95.0,
                 exit_reason="STOP_LOSS_ATR", pnl=-5.0),
    ], signals=2)
    stats = bt.summarize(res)
    assert stats["trades"] == 2
    assert stats["wins"] == 1
    assert stats["win_rate"] == 0.5
    assert stats["expectancy"] == 2.5
    assert stats["profit_factor"] == pytest.approx(2.0)
    assert stats["breakeven_wr"] == pytest.approx(1.0 / 3.0)
    assert stats["win_rate"] > stats["breakeven_wr"]
