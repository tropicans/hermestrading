"""Tes indikator: RSI tanpa bias, ATR dari candle nyata, fib guard adaptif."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from strategy_engine import EMAMomentumStrategy  # noqa: E402


def _ohmlc(prices, spread=0.0005):
    out = []
    for i, p in enumerate(prices):
        out.append({"t": 1_700_000_000 + i * 60,
                    "o": p, "h": p * (1 + spread), "l": p * (1 - spread), "c": p})
    return out


def _seeded(prices, spread=0.0005):
    s = EMAMomentumStrategy()
    s.ingest_ohlc(_ohmlc(prices, spread))
    return s


# ------------------------------------------------------------------ RSI

def test_rsi_not_pinned_near_fifty_on_flat_market():
    flat = [100.0 + (0.01 if i % 2 else -0.01) for i in range(60)]
    s = _seeded(flat)
    rsi = s.calculate_rsi()
    assert 30.0 < rsi < 70.0


def test_rsi_reaches_extreme_on_pure_uptrend():
    rising = [100.0 * (1.002 ** i) for i in range(60)]
    s = _seeded(rising)
    assert s.calculate_rsi() > 90.0


def test_rsi_zero_on_pure_downtrend():
    falling = [100.0 * (0.998 ** i) for i in range(60)]
    s = _seeded(falling)
    assert s.calculate_rsi() < 10.0


def test_rsi_at_most_one_hundred():
    rising = [100.0 + i for i in range(60)]
    s = _seeded(rising)
    assert s.calculate_rsi() <= 100.0


# ------------------------------------------------------------------ ATR

def test_atr_reflects_real_candle_range():
    quiet = [69_000_000.0] * 60
    s_quiet = _seeded(quiet, spread=0.0002)
    s_wild = _seeded(quiet, spread=0.0040)
    assert s_wild.calculate_atr() > s_quiet.calculate_atr() * 3


def test_atr_not_stuck_at_old_floor():
    # Lantai lama 0.1% dari harga menutupi seluruh variasi ATR.
    price = 69_000_000.0
    s = _seeded([price] * 60, spread=0.0001)
    assert s.calculate_atr() < price * 0.001


def test_atr_grows_with_real_swings():
    swings = [69_000_000.0 + (i % 10) * 5_000 for i in range(60)]
    s = _seeded(swings, spread=0.0001)
    assert s.calculate_atr() > 0


# ------------------------------------------------------------------ Fib guard

def test_fib_allows_near_high_when_uptrend():
    # Tren naik: harga di puncak swing tapi EMA5 > EMA13.
    rising = [69_000_000.0 * (1.0015 ** i) for i in range(40)]
    s = _seeded(rising, spread=0.0002)
    fib = s.calculate_fib_pullback(rising[-1])
    assert fib["uptrend"] is True
    assert fib["zone_pos_pct"] > 76.4
    assert fib["is_valid"] is True


def test_fib_strict_when_not_uptrend():
    # Turun lalu rebound tajam ke puncak: EMA cepat belum di atas lambat.
    prices = [69_000_000.0 * (0.999 ** i) for i in range(35)]
    prices += [prices[-1] * (1.004 ** i) for i in range(1, 6)]
    s = _seeded(prices, spread=0.0002)
    s.slow_period = 40  # paksa skenario bukan uptrend
    fib = s.calculate_fib_pullback(prices[-1])
    assert fib["threshold_pct"] == pytest.approx(76.4)


def test_fib_returns_threshold_metadata():
    s = _seeded([69_000_000.0 + i * 1000 for i in range(40)])
    fib = s.calculate_fib_pullback(69_000_000.0 + 39_000)
    assert "threshold_pct" in fib
    assert "uptrend" in fib
