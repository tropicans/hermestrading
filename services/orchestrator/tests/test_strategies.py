"""Tes strategi alternatif: level harga, filter, dan perilaku sinyal."""

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import strategies as st  # noqa: E402


def _series(prices, spread=0.0005):
    closes = list(prices)
    highs = [p * (1 + spread) for p in closes]
    lows = [p * (1 - spread) for p in closes]
    return closes, highs, lows


# ------------------------------------------------------------------ helpers

def test_cost_aware_levels_have_net_rr():
    lv = st.cost_aware_levels(69_000_000.0, 69_000_000.0 * 0.01, "PAXG/IDR")
    tp_pct = lv["tp_distance"] / 69_000_000.0
    sl_pct = lv["sl_distance"] / 69_000_000.0
    net_rr = (tp_pct - 0.0074) / (sl_pct + 0.0074)
    assert net_rr == pytest.approx(2.2, rel=0.02)


def test_cost_aware_respects_min_sl():
    lv = st.cost_aware_levels(69_000_000.0, 1.0, "PAXG/IDR")
    assert lv["sl_distance"] >= 69_000_000.0 * 0.0090


def test_rsi_extremes():
    up = [100.0 * (1.002 ** i) for i in range(40)]
    down = [100.0 * (0.998 ** i) for i in range(40)]
    assert st.rsi(up) > 90
    assert st.rsi(down) < 10


def test_sma_and_stdev():
    series = [10.0, 12.0, 14.0, 16.0, 18.0]
    assert st.sma(series, 5) == 14.0
    assert st.stdev(series, 5) > 0


# ------------------------------------------------------------------ mean reversion

def test_mean_reversion_buys_on_deep_dip():
    # Tren naik panjang lalu jatuh tajam di bawah band bawah.
    prices = [100.0 * (1.0004 ** i) for i in range(260)]
    prices += [prices[-1] * 0.97, prices[-1] * 0.965]
    s = st.MeanReversionStrategy("PAXG/IDR", trend_filter=False)
    closes, highs, lows = _series(prices)
    sig = s.signal(closes, highs, lows)
    assert sig is not None
    assert sig["action"] == "BUY"
    assert sig["stop_loss"] < sig["trigger_price"] < sig["take_profit"]


def test_mean_reversion_quiet_in_uptrend():
    prices = [100.0 * (1.001 ** i) for i in range(260)]
    s = st.MeanReversionStrategy("PAXG/IDR", trend_filter=False)
    closes, highs, lows = _series(prices)
    assert s.signal(closes, highs, lows) is None


def test_mean_reversion_trend_filter_blocks_downtrend():
    prices = [100.0 * (0.999 ** i) for i in range(260)]
    prices += [prices[-1] * 0.96]
    s = st.MeanReversionStrategy("PAXG/IDR", trend_filter=True)
    closes, highs, lows = _series(prices)
    assert s.signal(closes, highs, lows) is None


# ------------------------------------------------------------------ donchian

def test_donchian_fires_on_breakout():
    prices = [100.0 + (i % 5) * 0.1 for i in range(250)]
    prices += [105.0, 106.0]
    s = st.DonchianBreakoutStrategy("PAXG/IDR", trend_filter=False, min_atr_pct=0.0001)
    closes, highs, lows = _series(prices)
    sig = s.signal(closes, highs, lows)
    assert sig is not None
    assert sig["trigger_price"] > 100.0


def test_donchian_blocked_by_volatility_filter():
    prices = [100.0] * 250
    prices += [100.5]
    s = st.DonchianBreakoutStrategy("PAXG/IDR", trend_filter=False, min_atr_pct=0.05)
    closes, highs, lows = _series(prices)
    assert s.signal(closes, highs, lows) is None


def test_donchian_quiet_without_breakout():
    prices = [100.0 - (i % 5) * 0.1 for i in range(250)]
    s = st.DonchianBreakoutStrategy("PAXG/IDR", trend_filter=False)
    closes, highs, lows = _series(prices)
    assert s.signal(closes, highs, lows) is None


# ------------------------------------------------------------------ slow trend

def test_slow_trend_blocked_below_ema200():
    # Crossover terjadi (bar ~157) tapi harga masih di bawah EMA200 -> harus ditahan.
    prices = [100.0 * (0.999 ** i) for i in range(150)]
    prices += [prices[-1] * (1.010 ** i) for i in range(1, 200)]
    s = st.SlowTrendStrategy("PAXG/IDR")
    fired = False
    for end in range(1, len(prices)):
        closes, highs, lows = _series(prices[:end + 1])
        if s.signal(closes, highs, lows):
            fired = True
            break
    assert fired is False


def test_slow_trend_fires_when_above_ema200():
    # Siklus turun-naik-datar-naik: crossover kedua terjadi setelah harga di atas EMA200.
    prices = [100.0 * (0.999 ** i) for i in range(120)]
    prices += [prices[-1] * (1.006 ** i) for i in range(1, 120)]
    prices += [prices[-1] * (0.998 ** i) for i in range(1, 60)]
    prices += [prices[-1] * (1.006 ** i) for i in range(1, 120)]
    s = st.SlowTrendStrategy("PAXG/IDR")
    fired = False
    for end in range(1, len(prices)):
        closes, highs, lows = _series(prices[:end + 1])
        sig = s.signal(closes, highs, lows)
        if sig:
            fired = True
            assert sig["stop_loss"] < sig["trigger_price"] < sig["take_profit"]
            break
    assert fired is True


# ------------------------------------------------------------------ buy & hold

def test_buy_and_hold_only_once():
    s = st.BuyAndHoldStrategy("PAXG/IDR")
    closes, highs, lows = _series([100.0] * 30)
    first = s.signal(closes, highs, lows)
    second = s.signal(closes, highs, lows)
    assert first is not None
    assert second is None
    assert first["stop_loss"] == 0.0
