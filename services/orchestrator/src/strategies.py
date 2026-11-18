# -*- coding: utf-8 -*-
"""Strategi alternatif yang bisa diuji lewat backtest.

Setiap strategi mengekspos antarmuka yang sama agar backtester dapat memakainya:

    on_candle(closes, highs, lows, opens) -> None   (perbarui state internal)
    signal(price) -> dict | None

Semua strategi hanya long (spot syariah, tanpa short). Level TP/SL dihitung dengan
rumus cost-aware yang sama seperti produksi, sehingga biaya bolak-balik selalu
diperhitungkan.
"""

import os


# ---------------------------------------------------------------- helpers

def cost_aware_levels(price: float, sl_distance: float, pair: str) -> dict:
    """TP/SL dengan R:R neto yang eksplisit (sama dengan strategi produksi).

    Net R:R = (TP% - c) / (SL% + c). TP = RR * (SL% + c) + c.
    """
    round_nd = 0 if "IDR" in pair.upper() else 2

    if "IDR" in pair.upper():
        rr_target = float(os.getenv("TP_RR_TARGET_IDR", "2.2"))
        min_sl_pct = float(os.getenv("MIN_SL_PCT_IDR", "0.0090"))
    else:
        rr_target = float(os.getenv("TP_RR_TARGET", "2.0"))
        min_sl_pct = float(os.getenv("MIN_SL_PCT", "0.0040"))

    sl_distance = max(sl_distance, price * min_sl_pct)
    cost_pct = min(float(os.getenv("ROUNDTRIP_COST_PCT", "0.0074")),
                   float(os.getenv("MAX_ROUNDTRIP_COST_PCT", "0.0200")))
    net_sl_pct = (sl_distance / price) + cost_pct
    tp_distance = price * ((rr_target * net_sl_pct) + cost_pct)

    return {
        "tp_distance": round(tp_distance, 2),
        "sl_distance": round(sl_distance, 2),
        "tp_price": round(price + tp_distance, round_nd),
        "sl_price": round(price - sl_distance, round_nd),
    }


def true_ranges(closes, highs, lows, period: int) -> float:
    """ATR sederhana dari true range terakhir."""
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(len(closes) - period, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        trs.append(max(hl, hc, lc))
    return sum(trs) / len(trs)


def sma(series, period: int) -> float:
    if len(series) < period:
        return 0.0
    return sum(series[-period:]) / period


def stdev(series, period: int) -> float:
    if len(series) < period:
        return 0.0
    window = series[-period:]
    mean = sum(window) / period
    var = sum((x - mean) ** 2 for x in window) / period
    return var ** 0.5


def rsi(closes, period: int = 14) -> float:
    """RSI Wilder tanpa noise floor."""
    if len(closes) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------- strategies

class BaseStrategy:
    name = "base"

    def __init__(self, pair: str):
        self.pair = pair
        self.skipped = 0

    def _build(self, price: float, atr: float, reason: str = "") -> dict:
        levels = cost_aware_levels(price, atr, self.pair)
        return {
            "pair": self.pair,
            "action": "BUY",
            "contract_type": "SPOT",
            "trigger_price": price,
            "take_profit": levels["tp_price"],
            "stop_loss": levels["sl_price"],
            "reason": reason,
            "meta": {"atr": round(atr, 2)},
        }


class MeanReversionStrategy(BaseStrategy):
    """Bollinger + RSI: beli saat harga jatuh ke bawah band bawah dan RSI oversold.

    Logika terbalik dari crossover. Cocok untuk pasar sideways yang mendominasi
    pair ini; gagal di tren turun kuat, sehingga ditambah filter tren EMA200.
    """

    name = "mean_reversion"

    def __init__(self, pair, bb_period=20, bb_mult=2.0, rsi_period=14,
                 rsi_buy=30.0, trend_filter=True):
        super().__init__(pair)
        self.bb_period = bb_period
        self.bb_mult = bb_mult
        self.rsi_period = rsi_period
        self.rsi_buy = rsi_buy
        self.trend_filter = trend_filter

    def signal(self, closes, highs, lows):
        if len(closes) < max(self.bb_period, self.rsi_period, 200) + 2:
            return None
        price = closes[-1]
        mid = sma(closes, self.bb_period)
        sd = stdev(closes, self.bb_period)
        if mid <= 0 or sd <= 0:
            return None
        lower = mid - (self.bb_mult * sd)
        r = rsi(closes, self.rsi_period)

        if price > lower or r > self.rsi_buy:
            return None
        # Filter tren: jangan beli jatuh di bawah EMA200 (pasar sedang turun besar).
        if self.trend_filter and price < sma(closes, 200):
            self.skipped += 1
            return None

        atr = true_ranges(closes, highs, lows, 14) or (price * 0.002)
        return self._build(price, atr, reason=f"BB lower {lower:,.0f}, RSI {r:.1f}")


class DonchianBreakoutStrategy(BaseStrategy):
    """Breakout high N-bar dengan filter volatilitas ATR.

    Membeli hanya ketika harga menembus puncak N candle terakhir DAN volatilitas
    cukup tinggi, sehingga menghindari breakout palsu di pasar mati.
    """

    name = "donchian_breakout"

    def __init__(self, pair, lookback=20, atr_period=14, min_atr_pct=0.004,
                 trend_filter=True):
        super().__init__(pair)
        self.lookback = lookback
        self.atr_period = atr_period
        self.min_atr_pct = min_atr_pct
        self.trend_filter = trend_filter

    def signal(self, closes, highs, lows):
        if len(closes) < max(self.lookback, 200) + 2:
            return None
        price = closes[-1]
        prior_high = max(highs[-self.lookback - 1:-1])
        atr = true_ranges(closes, highs, lows, self.atr_period)
        if atr <= 0:
            return None
        if price <= prior_high:
            return None
        if (atr / price) < self.min_atr_pct:
            self.skipped += 1
            return None
        if self.trend_filter and price < sma(closes, 200):
            self.skipped += 1
            return None
        return self._build(price, atr, reason=f"Break {prior_high:,.0f}")


class SlowTrendStrategy(BaseStrategy):
    """EMA 20/50 + filter EMA200: hanya ikut tren besar.

    Trade jauh lebih sedikit, tapi menghindari entry di noise seperti EMA5/13.
    """

    name = "slow_trend"

    def __init__(self, pair, fast=20, slow=50, trend=200, atr_period=14):
        super().__init__(pair)
        self.fast = fast
        self.slow = slow
        self.trend = trend
        self.atr_period = atr_period
        self._prev_diff = None

    @staticmethod
    def _ema(series, period):
        if len(series) < period:
            return series[-1] if series else 0.0
        k = 2 / (period + 1)
        ema = sum(series[:period]) / period
        for p in series[period:]:
            ema = (p * k) + (ema * (1 - k))
        return ema

    def signal(self, closes, highs, lows):
        if len(closes) < self.trend + 2:
            return None
        price = closes[-1]
        fast = self._ema(closes, self.fast)
        slow = self._ema(closes, self.slow)
        diff = fast - slow
        prev = self._prev_diff
        self._prev_diff = diff
        if prev is None or not (prev <= 0 < diff):
            return None
        if price < self._ema(closes, self.trend):
            self.skipped += 1
            return None
        atr = true_ranges(closes, highs, lows, self.atr_period) or (price * 0.002)
        return self._build(price, atr, reason=f"EMA{self.fast}/{self.slow} cross")


class BuyAndHoldStrategy(BaseStrategy):
    """Pembanding: beli sekali di awal, tahan sampai akhir. Tidak pernah menjual."""

    name = "buy_hold"

    def __init__(self, pair):
        super().__init__(pair)
        self.bought = False

    def signal(self, closes, highs, lows):
        if self.bought or len(closes) < 2:
            return None
        self.bought = True
        price = closes[-1]
        return {
            "pair": self.pair,
            "action": "BUY",
            "contract_type": "SPOT",
            "trigger_price": price,
            "take_profit": price * 1e9,   # praktis tidak pernah kena
            "stop_loss": 0.0,             # tidak pernah stop
            "reason": "buy and hold",
            "meta": {"atr": 0.0},
        }


STRATEGIES = {
    "mean_reversion": MeanReversionStrategy,
    "donchian_breakout": DonchianBreakoutStrategy,
    "slow_trend": SlowTrendStrategy,
    "buy_hold": BuyAndHoldStrategy,
}
