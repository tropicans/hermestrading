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
    def get_recent_candles(cls, pair: str = None, limit: int = 35) -> list:
        """Mengambil riwayat candle 1-menit (TF 1m) langsung dari chart Indodax."""
        target_pair = pair or os.getenv("TRADING_SYMBOL", "PAXG/IDR")
        base, quote = target_pair.upper().split("/") if "/" in target_pair else (target_pair[:-3].upper(), target_pair[-3:].upper())
        symbol = f"{base}{quote}"
        now_ts = int(time.time())
        from_ts = now_ts - (limit * 60 * 3)
        try:
            url = f"https://indodax.com/tradingview/history_v2?symbol={symbol}&tf=1&from={from_ts}&to={now_ts}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode())
                if isinstance(data, list) and len(data) >= 5:
                    return [float(c.get("Close", 0)) for c in data[-limit:] if float(c.get("Close", 0)) > 0]
        except Exception as e:
            print(f"[strategy_engine] Info history_v2 fallback: {e}")
        return []

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

        if cls._last_valid_price is not None:
            return cls._last_valid_price

        raise RuntimeError(f"Tidak dapat mengambil harga awal untuk {target_pair}")


class EMAMomentumStrategy:
    def __init__(self, fast_period: int = 5, slow_period: int = 13, rsi_period: int = 14, atr_period: int = 14, fib_lookback: int = 30):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.fib_lookback = fib_lookback
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

    def calculate_ema(self, period: int, price_series: list = None, live_price: float = None) -> float:
        base_series = price_series if price_series is not None else self.prices
        series = (base_series[:-1] + [live_price]) if (live_price and len(base_series) > 0) else base_series
        if not series:
            return 0.0
        if len(series) < period:
            return series[-1]
        k = 2 / (period + 1)
        ema = sum(series[:period]) / period
        for p in series[period:]:
            ema = (p * k) + (ema * (1 - k))
        return ema

    def calculate_rsi(self, live_price: float = None) -> float:
        series = (self.prices[:-1] + [live_price]) if (live_price and len(self.prices) > 0) else self.prices
        if len(series) <= self.rsi_period:
            return 50.0

        gains = []
        losses = []
        for i in range(1, len(series)):
            change = series[i] - series[i-1]
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

        noise_floor = (series[-1] * 0.0001) / self.rsi_period
        avg_gain += noise_floor
        avg_loss += noise_floor

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

    def calculate_fib_pullback(self, live_price: float = None) -> dict:
        """
        Menghitung posisi harga terhadap Swing High & Swing Low 30 menit terakhir:
        - retracement_pct: seberapa dalam diskon dari puncak Swing High (0% = tepat di pucuk, 50% = tengah, 100% = dasar Swing Low).
        - zone_pos_pct: posisi dari bawah (0% = dasar Swing Low, 100% = pucuk Swing High).
        - is_valid: True jika harga tidak berada di pucuk ekstrem (zone_pos_pct <= 76.4% atau retracement >= 23.6%),
                    atau jika rentang harga masih sempit/sideways (< 0.20%) sehingga aman untuk breakout awal.
        """
        curr = live_price if live_price else (self.prices[-1] if self.prices else 0.0)
        if not self.prices or curr <= 0:
            return {"swing_high": curr, "swing_low": curr, "retrace_pct": 50.0, "zone_pos_pct": 50.0, "is_valid": True}

        window_highs = self.highs[-self.fib_lookback:] if self.highs else self.prices[-self.fib_lookback:]
        window_lows = self.lows[-self.fib_lookback:] if self.lows else self.prices[-self.fib_lookback:]

        swing_high = max(max(window_highs), curr)
        swing_low = min(min(window_lows), curr)
        swing_range = swing_high - swing_low

        # Jika rentang ayunan 30 menit masih sempit (< 0.20% dari harga), anggap fase akumulasi/breakout awal (aman)
        if swing_range <= (curr * 0.0020):
            return {
                "swing_high": round(swing_high, 0),
                "swing_low": round(swing_low, 0),
                "retrace_pct": 50.0,
                "zone_pos_pct": 50.0,
                "is_valid": True
            }

        retrace_pct = ((swing_high - curr) / swing_range) * 100.0
        zone_pos_pct = ((curr - swing_low) / swing_range) * 100.0

        # Valid jika sudah terdiskon minimal 23.6% dari Swing High (posisi <= 76.4% dari lembah ke pucuk)
        is_valid = zone_pos_pct <= 76.4

        return {
            "swing_high": round(swing_high, 0),
            "swing_low": round(swing_low, 0),
            "retrace_pct": round(retrace_pct, 1),
            "zone_pos_pct": round(zone_pos_pct, 1),
            "is_valid": is_valid
        }

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
        fib = self.calculate_fib_pullback(current_price)

        ema_cross = (ema_fast > ema_slow) and (prev_ema_fast <= prev_ema_slow)
        price_momentum = current_price > self.prices[-2]
        rsi_valid = 45.0 <= rsi <= 68.0
        spread_pct = MarketDataFeed._last_spread_pct
        spread_valid = spread_pct <= 0.0085
        fib_valid = fib["is_valid"]

        if ema_cross and not (price_momentum and rsi_valid and spread_valid and fib_valid):
            reasons = []
            if not rsi_valid:
                reasons.append(f"RSI {rsi:.1f} di luar zona (45-68)")
            if not fib_valid:
                reasons.append(f"Fib Guard: Harga di pucuk swing ({fib['zone_pos_pct']:.1f}% > 76.4%, diskon baru {fib['retrace_pct']:.1f}%)")
            if not price_momentum:
                reasons.append("Harga belum konfirmasi naik dari menit lalu")
            if not spread_valid:
                reasons.append(f"Spread terlalu lebar ({spread_pct*100:.2f}% > 0.85%)")
            return {
                "action": "SKIPPED",
                "pair": pair,
                "trigger_price": current_price,
                "reason": ", ".join(reasons),
                "meta": {
                    "rsi": round(rsi, 2),
                    "atr": round(atr, 2),
                    "ema_fast": round(ema_fast, 0 if "IDR" in pair.upper() else 2),
                    "ema_slow": round(ema_slow, 0 if "IDR" in pair.upper() else 2),
                    "fib_retrace": fib["retrace_pct"],
                    "fib_zone": fib["zone_pos_pct"]
                }
            }

        if ema_cross and price_momentum and rsi_valid and spread_valid and fib_valid:
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
                    "ema_slow": round(ema_slow, 2),
                    "fib_retrace": fib["retrace_pct"],
                    "fib_zone": fib["zone_pos_pct"]
                }
            }
        return None
