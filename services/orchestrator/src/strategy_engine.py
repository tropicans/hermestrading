# -*- coding: utf-8 -*-
import os
import time
import urllib.request
import json
import ccxt


def parse_timeframe_minutes(tf_str: str = None) -> int:
    raw = (tf_str or os.getenv("TRADING_TIMEFRAME", "1h")).strip().lower()
    if raw.endswith("m"):
        return max(1, int(raw[:-1]))
    if raw.endswith("h"):
        return max(1, int(raw[:-1]) * 60)
    if raw.endswith("d"):
        return max(1, int(raw[:-1]) * 1440)
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


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
    def get_recent_candles_ohlc(cls, pair: str = None, limit: int = 220, timeframe_min: int = None) -> list:
        """Ambil candle OHLC resmi sesuai timeframe yang dikonfigurasi."""
        target_pair = pair or os.getenv("TRADING_SYMBOL", "BTC/IDR")
        tf_min = timeframe_min if timeframe_min is not None else parse_timeframe_minutes()
        base, quote = target_pair.upper().split("/") if "/" in target_pair else (target_pair[:-3].upper(), target_pair[-3:].upper())
        symbol = f"{base}{quote}"
        now_ts = int(time.time())
        from_ts = now_ts - (limit * tf_min * 60 * 2)
        try:
            url = (f"https://indodax.com/tradingview/history_v2?symbol={symbol}"
                   f"&tf={tf_min}&from={from_ts}&to={now_ts}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if not isinstance(data, list) or len(data) < 5:
                    return []
                out = []
                seen = set()
                for c in data:
                    ts = c.get("Time")
                    if ts in seen:
                        continue
                    seen.add(ts)
                    o = float(c.get("Open") or 0)
                    h = float(c.get("High") or 0)
                    l = float(c.get("Low") or 0)
                    cl = float(c.get("Close") or 0)
                    if min(o, h, l, cl) <= 0:
                        continue
                    out.append({"t": int(ts), "o": o, "h": h, "l": l, "c": cl})
                out.sort(key=lambda x: x["t"])
                return out[-limit:]
        except Exception as e:
            print(f"[strategy_engine] Gagal memuat candle OHLC: {e}")
        return []

    @classmethod
    def get_recent_candles(cls, pair: str = None, limit: int = 220) -> list:
        return [c["c"] for c in cls.get_recent_candles_ohlc(pair, limit)]

    @classmethod
    def get_spot_price(cls, pair: str = None) -> float:
        """Mengambil Mid-Price (rata-rata Bid & Ask aktif) dari Indodax."""
        target_pair = pair or os.getenv("TRADING_SYMBOL", "BTC/IDR")
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
    """
    HERMES v3 Wide-Swing 3-Layer Spot Grid (Syariah 1:1):
    Akumulasi bertingkat 3 lapis (-4.5% / -5.18% / -5.85%) saat RSI jenuh jual + konfirmasi candle hijau,
    dengan exit terpadu pada +4.2% dari Weighted Average Cost.
    """
    def __init__(self, fast_period: int = 20, slow_period: int = 50, trend_period: int = 200,
                 rsi_period: int = 14, atr_period: int = 14, fib_lookback: int = 30,
                 timeframe_min: int = None):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_period = trend_period
        self.rsi_period = rsi_period
        self.atr_period = atr_period
        self.fib_lookback = fib_lookback
        self.timeframe_min = timeframe_min if timeframe_min is not None else parse_timeframe_minutes()
        self.candle_interval_sec = float(self.timeframe_min * 60)
        self.opens = []
        self.prices = []
        self.highs = []
        self.lows = []
        self._last_sample_ts = 0.0
        self._bucket_high = 0.0
        self._bucket_low = 0.0
        self.candle_ts = []

        self.step_pct = float(os.getenv("GRID_STEP_PCT", "0.045"))
        self.tp_pct = float(os.getenv("GRID_TP_PCT", "0.042"))
        self.max_layers = int(os.getenv("GRID_MAX_LAYERS", "3"))
        self.layer_weights = [0.25, 0.30, 0.35]

    def _update_candle_aggregates(self, price: float) -> None:
        if self._bucket_high == 0.0 or price > self._bucket_high:
            self._bucket_high = price
        if self._bucket_low == 0.0 or price < self._bucket_low:
            self._bucket_low = price

    def ingest_ohlc(self, candles: list) -> None:
        if not candles:
            return
        self.opens = [c["o"] for c in candles]
        self.prices = [c["c"] for c in candles]
        self.highs = [c["h"] for c in candles]
        self.lows = [c["l"] for c in candles]
        self.candle_ts = [c.get("t", 0) for c in candles]
        self._last_sample_ts = time.time()
        self._bucket_high = self.prices[-1]
        self._bucket_low = self.prices[-1]

    def add_price(self, price: float, force_sample: bool = False) -> bool:
        if price <= 0:
            return False

        now = time.time()
        self._update_candle_aggregates(price)

        if not force_sample and (now - self._last_sample_ts) < self.candle_interval_sec and len(self.prices) > 0:
            return False

        prev_close = self.prices[-1] if self.prices else price
        self.opens.append(prev_close)
        self.prices.append(price)
        spread = max(0.01, price * 0.0002)
        self.highs.append(round(max(self._bucket_high, price + spread), 2))
        self.lows.append(round(min(self._bucket_low, price - spread), 2))

        self._last_sample_ts = now
        self._bucket_high = price
        self._bucket_low = price

        if len(self.prices) > 300:
            self.opens.pop(0)
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
            return sum(series) / len(series)
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
            change = series[i] - series[i - 1]
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

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        if avg_gain == 0:
            return 0.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def calculate_atr(self) -> float:
        current_p = self.prices[-1] if self.prices else 1500000000.0
        if len(self.prices) <= self.atr_period:
            return round(current_p * 0.005, 2)

        tr_list = []
        for i in range(1, len(self.prices)):
            hl = self.highs[i] - self.lows[i]
            hc = abs(self.highs[i] - self.prices[i - 1])
            lc = abs(self.lows[i] - self.prices[i - 1])
            tr_list.append(max(hl, hc, lc))

        recent_tr = tr_list[-self.atr_period:]
        atr = sum(recent_tr) / len(recent_tr)
        return max(current_p * 0.0005, round(atr, 2))

    def calculate_fib_pullback(self, live_price: float = None) -> dict:
        curr = live_price if live_price else (self.prices[-1] if self.prices else 0.0)
        if not self.prices or curr <= 0:
            return {"swing_high": curr, "swing_low": curr, "retrace_pct": 50.0, "zone_pos_pct": 50.0, "is_valid": True}

        window_highs = self.highs[-self.fib_lookback:] if self.highs else self.prices[-self.fib_lookback:]
        window_lows = self.lows[-self.fib_lookback:] if self.lows else self.prices[-self.fib_lookback:]
        swing_high = max(max(window_highs), curr)
        swing_low = min(min(window_lows), curr)
        swing_range = swing_high - swing_low
        if swing_range <= (curr * 0.0030):
            return {"swing_high": round(swing_high, 0), "swing_low": round(swing_low, 0), "retrace_pct": 50.0, "zone_pos_pct": 50.0, "is_valid": True}

        retrace_pct = ((swing_high - curr) / swing_range) * 100.0
        zone_pos_pct = max(0.0, min(100.0, ((curr - swing_low) / swing_range) * 100.0))
        return {
            "swing_high": round(swing_high, 0),
            "swing_low": round(swing_low, 0),
            "retrace_pct": round(retrace_pct, 1),
            "zone_pos_pct": round(zone_pos_pct, 1),
            "is_valid": True
        }

    def evaluate_signal(self, pair: str, current_price: float, open_positions: list = None,
                        spread_pct: float = None) -> dict:
        if not current_price or current_price <= 0 or len(self.prices) < self.slow_period:
            return None

        positions = [p for p in (open_positions or []) if p.get("pair") == pair]
        layer_idx = len(positions)
        if layer_idx >= self.max_layers:
            return None

        ema_fast = self.calculate_ema(self.fast_period)
        ema_slow = self.calculate_ema(self.slow_period)
        rsi = self.calculate_rsi()
        atr = self.calculate_atr()
        round_nd = 0 if "IDR" in pair.upper() else 2

        last_open = self.opens[-1] if self.opens else current_price
        last_close = self.prices[-1]
        is_green_candle = last_close > last_open

        if spread_pct is None:
            spread_pct = MarketDataFeed._last_spread_pct
        max_spread = float(os.getenv("MAX_SPREAD_PCT", "0.0040"))
        spread_valid = 0 <= spread_pct <= max_spread

        if layer_idx == 0:
            target_entry = ema_slow * (1.0 - self.step_pct)
            rsi_limit = 35.0
            in_zone = last_close <= target_entry
        else:
            sorted_pos = sorted(positions, key=lambda x: float(x.get("fill_price", 0)))
            lowest_entry = float(sorted_pos[0].get("fill_price", current_price))
            required_drop = self.step_pct * (1.0 + 0.15 * layer_idx)
            target_entry = lowest_entry * (1.0 - required_drop)
            rsi_limit = 33.0
            in_zone = last_close <= target_entry

        rsi_valid = rsi < rsi_limit

        if in_zone and not (is_green_candle and rsi_valid and spread_valid):
            reasons = []
            if not is_green_candle:
                reasons.append("Menunggu penutupan candle hijau konfirmasi pantulan")
            if not rsi_valid:
                reasons.append(f"RSI {rsi:.1f} >= {rsi_limit:.0f}")
            if not spread_valid:
                reasons.append(f"Spread lebar ({spread_pct*100:.2f}%)")
            return {
                "action": "SKIPPED",
                "pair": pair,
                "trigger_price": current_price,
                "reason": f"[Grid L{layer_idx+1}] " + ", ".join(reasons),
                "meta": {
                    "rsi": round(rsi, 2),
                    "atr": round(atr, 2),
                    "ema_fast": round(ema_fast, round_nd),
                    "ema_slow": round(ema_slow, round_nd),
                    "layer": layer_idx + 1,
                    "target_entry": round(target_entry, round_nd)
                }
            }

        if not (in_zone and is_green_candle and rsi_valid and spread_valid):
            return None

        weight = self.layer_weights[min(layer_idx, len(self.layer_weights) - 1)]
        tp_price = round(current_price * (1.0 + self.tp_pct), round_nd)
        # Spot Syariah 1:1 Wide-Swing Grid tidak memakai SL ketat; proteksi bencana ekstrem -35%
        sl_price = round(current_price * 0.65, round_nd)
        roundtrip_cost = float(os.getenv("ROUNDTRIP_COST_PCT", "0.0074"))
        net_tp_pct = self.tp_pct - roundtrip_cost

        return {
            "pair": pair,
            "action": "BUY",
            "contract_type": "SPOT",
            "trigger_price": current_price,
            "take_profit": tp_price,
            "stop_loss": sl_price,
            "layer_weight": weight,
            "layer_num": layer_idx + 1,
            "meta": {
                "rsi": round(rsi, 2),
                "atr": round(atr, 2),
                "ema_fast": round(ema_fast, round_nd),
                "ema_slow": round(ema_slow, round_nd),
                "layer": layer_idx + 1,
                "layer_weight": weight,
                "net_rr": round(net_tp_pct / 0.015, 2)
            }
        }

    def analyze(self, pair: str, current_price: float, open_positions: list = None) -> dict:
        if not current_price or current_price <= 0:
            return None

        is_new_candle = self.add_price(current_price, force_sample=False)
        if not is_new_candle or len(self.prices) < self.slow_period:
            return None

        return self.evaluate_signal(pair, current_price, open_positions=open_positions)
