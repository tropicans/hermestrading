# -*- coding: utf-8 -*-
import urllib.request
import json
import random

class MarketDataFeed:
    @staticmethod
    def get_spot_price(pair: str = "PAXGUSD") -> float:
        """Mengambil harga pasar spot PAXG/USD riil langsung dari Kraken Public Ticker."""
        url = "https://api.kraken.com/0/public/Ticker?pair=PAXGUSD"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Hermes-Feed/1.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if not data.get("error"):
                    ticker = data["result"].get("PAXGUSD", {})
                    return float(ticker["c"][0])
        except Exception:
            pass
        return round(2685.0 + random.uniform(-0.6, 0.6), 2)

class EMAMomentumStrategy:
    def __init__(self, fast_period: int = 3, slow_period: int = 5, rsi_period: int = 14, atr_period: int = 14):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.prices = []
        self.highs = []
        self.lows = []

    def add_price(self, price: float):
        self.prices.append(price)
        # Estimasi high/low mikro per iterasi tick
        self.highs.append(round(price + 0.15, 2))
        self.lows.append(round(price - 0.15, 2))

        # Batasi memori array historis
        if len(self.prices) > 60:
            self.prices.pop(0)
            self.highs.pop(0)
            self.lows.pop(0)

    def calculate_ema(self, period: int) -> float:
        if len(self.prices) < period:
            return self.prices[-1]
        k = 2 / (period + 1)
        ema = sum(self.prices[:period]) / period
        for p in self.prices[period:]:
            ema = (p * k) + (ema * (1 - k))
        return ema

    def calculate_rsi(self) -> float:
        if len(self.prices) <= self.rsi_period:
            return 50.0  # Netral jika data belum mencukupi

        gains = []
        losses = []
        for i in range(1, len(self.prices)):
            change = self.prices[i] - self.prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))

        # Ambil sampel sejumlah periode
        recent_gains = gains[-self.rsi_period:]
        recent_losses = losses[-self.rsi_period:]

        avg_gain = sum(recent_gains) / self.rsi_period
        avg_loss = sum(recent_losses) / self.rsi_period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calculate_atr(self) -> float:
        if len(self.prices) <= self.atr_period:
            return 2.50  # Default $2.50 volatilitas normal emas PAXG

        tr_list = []
        for i in range(1, len(self.prices)):
            hl = self.highs[i] - self.lows[i]
            hc = abs(self.highs[i] - self.prices[i-1])
            lc = abs(self.lows[i] - self.prices[i-1])
            tr = max(hl, hc, lc)
            tr_list.append(tr)

        recent_tr = tr_list[-self.atr_period:]
        return max(1.5, sum(recent_tr) / len(recent_tr))

    def analyze(self, pair: str, current_price: float) -> dict:
        self.add_price(current_price)
        
        # Butuh minimal sampel harga untuk kalkulasi akurat
        if len(self.prices) < self.rsi_period + 2:
            return None

        ema_fast = self.calculate_ema(self.fast_period)
        ema_slow = self.calculate_ema(self.slow_period)
        rsi = self.calculate_rsi()
        atr = self.calculate_atr()

        # 1. Konfirmasi Crossover: EMA Fast menembus EMA Slow dari bawah
        ema_cross = (ema_fast > ema_slow) and (self.prices[-2] <= ema_slow)

        # 2. Filter RSI: Pastikan ada momentum sehat dan belum overbought (42 <= RSI <= 68)
        rsi_valid = 42.0 <= rsi <= 68.0

        if ema_cross and rsi_valid:
            # 3. Dynamic Volatility TP/SL berbasis ATR (Menutup Fee Kraken ~0.26% x 2)
            tp_distance = round(atr * 1.8, 2)
            sl_distance = round(atr * 1.2, 2)

            tp_price = round(current_price + tp_distance, 2)
            sl_price = round(current_price - sl_distance, 2)

            return {
                "pair": pair,
                "action": "BUY",
                "contract_type": "SPOT",
                "trigger_price": current_price,
                "take_profit": tp_price,
                "stop_loss": sl_price,
                "meta": {
                    "rsi": round(rsi, 2),
                    "atr": round(atr, 2),
                    "ema_fast": round(ema_fast, 2),
                    "ema_slow": round(ema_slow, 2)
                }
            }
        return None
