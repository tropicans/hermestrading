# -*- coding: utf-8 -*-
import os
import time
import urllib.request
import json
import ccxt

class MarketDataFeed:
    _exchange = None
    _last_valid_price = None
    _last_spread_pct = 0.0

    @classmethod
    def _get_public_exchange(cls):
        if cls._exchange is None:
            exchange_id = os.getenv("EXCHANGE_ID", "indodax").lower()
            exchange_class = getattr(ccxt, exchange_id, ccxt.indodax)
            cls._exchange = exchange_class({'enableRateLimit': True})
        return cls._exchange

    @classmethod
    def get_spot_price(cls, pair: str = None) -> float:
        """Mengambil Mid-Price (rata-rata Bid & Ask aktif) dari Indodax agar harga selalu hidup."""
        target_pair = pair or os.getenv("TRADING_SYMBOL", "PAXG/IDR")
        exchange_id = os.getenv("EXCHANGE_ID", "indodax").lower()

        if exchange_id == "indodax":
            base, quote = target_pair.lower().split("/") if "/" in target_pair else (target_pair[:-3].lower(), target_pair[-3:].lower())
            for pair_id in [f"{base}_{quote}", f"{base}{quote}"]:
                try:
                    url = f"https://indodax.com/api/ticker/{pair_id}"
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read().decode())
                        ticker = data.get("ticker", {})
                        last_price = float(ticker.get("last") or 0)
                        buy_bid = float(ticker.get("buy") or 0)
                        sell_ask = float(ticker.get("sell") or 0)
                        if buy_bid > 0 and sell_ask > 0:
                            cls._last_spread_pct = (sell_ask - buy_bid) / buy_bid
                            cls._last_valid_price = round((buy_bid + sell_ask) / 2.0, 0)
                            return cls._last_valid_price
                        if last_price > 0:
                            cls._last_valid_price = last_price
                            return cls._last_valid_price
                except Exception:
                    continue

        try:
            ex = cls._get_public_exchange()
            ticker = ex.fetch_ticker(target_pair)
            buy_bid = float(ticker.get("bid") or 0)
            sell_ask = float(ticker.get("ask") or 0)
            last_price = float(ticker.get("last") or ticker.get("close") or 0)
            if buy_bid > 0 and sell_ask > 0:
                cls._last_spread_pct = (sell_ask - buy_bid) / buy_bid
                cls._last_valid_price = round((buy_bid + sell_ask) / 2.0, 0)
                return cls._last_valid_price
            if last_price > 0:
                cls._last_valid_price = last_price
                return cls._last_valid_price
        except Exception as e:
            print(f"[strategy_engine] Peringatan: Gagal mengambil ticker {target_pair}: {e}")

        if cls._last_valid_price is not None:
            return cls._last_valid_price

        raise RuntimeError(f"Tidak dapat mengambil harga awal untuk {target_pair}")


class EMAMomentumStrategy:
    def __init__(self, fast_period: int = 5, slow_period: int = 13, rsi_period: int = 14, atr_period: int = 14):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.prices = []
        self.highs = []
        self.lows = []
        self._last_sample_ts = 0.0
        self._bucket_high = 0.0
        self._bucket_low = 0.0

    def add_price(self, price: float, force_sample: bool = False) -> bool:
        if price <= 0:
            return False

        now = time.time()
        if self._bucket_high == 0.0 or price > self._bucket_high:
            self._bucket_high = price
        if self._bucket_low == 0.0 or price < self._bucket_low:
            self._bucket_low = price

        if not force_sample and (now - self._last_sample_ts) < 60.0 and len(self.prices) > 0:
            return False

        self.prices.append(price)
        spread = max(0.01, price * 0.0002)
        self.highs.append(round(max(self._bucket_high, price + spread), 2))
        self.lows.append(round(min(self._bucket_low, price - spread), 2))

        self._last_sample_ts = now
        self._bucket_high = price
        self._bucket_low = price

        if len(self.prices) > 100:
            self.prices.pop(0)
            self.highs.pop(0)
            self.lows.pop(0)
        return True

    def calculate_ema(self, period: int, price_series: list = None) -> float:
        series = price_series if price_series is not None else self.prices
        if not series:
            return 0.0
        if len(series) < period:
            return series[-1]
        k = 2 / (period + 1)
        ema = sum(series[:period]) / period
        for p in series[period:]:
            ema = (p * k) + (ema * (1 - k))
        return ema

    def calculate_rsi(self) -> float:
        if len(self.prices) <= self.rsi_period:
            return 50.0

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

        recent_gains = gains[-self.rsi_period:]
        recent_losses = losses[-self.rsi_period:]

        avg_gain = sum(recent_gains) / self.rsi_period
        avg_loss = sum(recent_losses) / self.rsi_period

        if avg_gain == 0 and avg_loss == 0:
            return 50.0
        if avg_loss == 0:
            return 75.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calculate_atr(self) -> float:
        current_p = self.prices[-1] if self.prices else 71000000.0
        min_atr = current_p * 0.001
        if len(self.prices) <= self.atr_period:
            return round(current_p * 0.0015, 2)

        tr_list = []
        for i in range(1, len(self.prices)):
            hl = self.highs[i] - self.lows[i]
            hc = abs(self.highs[i] - self.prices[i-1])
            lc = abs(self.lows[i] - self.prices[i-1])
            tr = max(hl, hc, lc)
            tr_list.append(tr)

        recent_tr = tr_list[-self.atr_period:]
        return max(min_atr, sum(recent_tr) / len(recent_tr))

    def analyze(self, pair: str, current_price: float) -> dict:
        if not current_price or current_price <= 0:
            return None

        is_new_candle = self.add_price(current_price, force_sample=False)
        if not is_new_candle or len(self.prices) < self.rsi_period + 2:
            return None

        ema_fast = self.calculate_ema(self.fast_period)
        ema_slow = self.calculate_ema(self.slow_period)
        prev_ema_fast = self.calculate_ema(self.fast_period, self.prices[:-1])
        prev_ema_slow = self.calculate_ema(self.slow_period, self.prices[:-1])

        rsi = self.calculate_rsi()
        atr = self.calculate_atr()

        ema_cross = (ema_fast > ema_slow) and (prev_ema_fast <= prev_ema_slow)
        price_momentum = current_price > self.prices[-2]
        rsi_valid = 45.0 <= rsi <= 68.0
        spread_pct = MarketDataFeed._last_spread_pct
        spread_valid = spread_pct <= 0.0035

        if ema_cross and not (price_momentum and rsi_valid and spread_valid):
            reasons = []
            if not rsi_valid:
                reasons.append(f"RSI {rsi:.1f} di luar zona aman (45-68)")
            if not price_momentum:
                reasons.append("Harga belum konfirmasi naik dari menit lalu")
            if not spread_valid:
                reasons.append(f"Spread Indodax terlalu lebar ({spread_pct*100:.2f}% > 0.35%)")
            return {
                "action": "SKIPPED",
                "pair": pair,
                "trigger_price": current_price,
                "reason": ", ".join(reasons),
                "meta": {
                    "rsi": round(rsi, 2),
                    "atr": round(atr, 2),
                    "ema_fast": round(ema_fast, 0 if "IDR" in pair.upper() else 2),
                    "ema_slow": round(ema_slow, 0 if "IDR" in pair.upper() else 2)
                }
            }

        if ema_cross and price_momentum and rsi_valid and spread_valid:
            min_tp_dist = current_price * 0.010
            min_sl_dist = current_price * 0.006

            tp_distance = round(max(atr * 2.0, min_tp_dist), 2)
            sl_distance = round(max(atr * 1.3, min_sl_dist), 2)

            tp_price = round(current_price + tp_distance, 0 if "IDR" in pair.upper() else 2)
            sl_price = round(current_price - sl_distance, 0 if "IDR" in pair.upper() else 2)

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
