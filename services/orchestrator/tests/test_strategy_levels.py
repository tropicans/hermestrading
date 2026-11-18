"""Tes logika parameter strategi: jarak TP/SL harus menutup biaya bolak-balik."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from strategy_engine import EMAMomentumStrategy  # noqa: E402


def _strategy():
    return EMAMomentumStrategy(fast_period=5, slow_period=13, rsi_period=14, atr_period=14)


def test_idr_levels_net_rr_above_threshold():
    s = _strategy()
    price = 69_000_000.0
    levels = s._cost_aware_levels(price, atr=price * 0.002, pair="PAXG/IDR")

    tp_pct = levels["tp_distance"] / price
    sl_pct = levels["sl_distance"] / price
    net_tp = tp_pct - 0.0074
    net_sl = sl_pct + 0.0074

    assert net_tp > 0
    # R:R neto harus >= target RR (default 2.2 untuk pair IDR).
    assert net_tp / net_sl >= 2.0


def test_idr_minimum_sl_covers_roundtrip():
    s = _strategy()
    price = 69_000_000.0
    # ATR sangat kecil pun tidak boleh menghasilkan SL lebih tipis dari minimum.
    levels = s._cost_aware_levels(price, atr=1.0, pair="PAXG/IDR")
    assert levels["sl_distance"] >= price * 0.0090


def test_non_idr_levels_scale_with_atr():
    s = _strategy()
    price = 2000.0
    tiny = s._cost_aware_levels(price, atr=0.5, pair="BTC/USDT")
    wide = s._cost_aware_levels(price, atr=20.0, pair="BTC/USDT")
    assert wide["tp_distance"] > tiny["tp_distance"]
    assert wide["sl_price"] < price < wide["tp_price"]


@pytest.mark.parametrize("pair,price", [("PAXG/IDR", 69_000_000.0), ("BTC/USDT", 2000.0)])
def test_sl_below_and_tp_above_price(pair, price):
    levels = _strategy()._cost_aware_levels(price, atr=price * 0.001, pair=pair)
    assert levels["sl_price"] < price
    assert levels["tp_price"] > price
