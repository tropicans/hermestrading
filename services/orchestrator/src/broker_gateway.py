# -*- coding: utf-8 -*-
import os
import time
import uuid
import ccxt

class BrokerExecutionError(Exception):
    pass

class OrderNotFilledError(BrokerExecutionError):
    """Limit order tidak terisi dalam batas waktu (bukan kegagalan teknis)."""
    pass

# Biaya taker Indodax, dipakai untuk menghitung PnL estimasi.
TAKER_FEE_RATE = float(os.getenv("INDODAX_TAKER_FEE", "0.0022"))
# Netralkan spread: beli sedikit di atas best ask, jual sedikit di bawah best bid.
SLIPPAGE_PAD_PCT = float(os.getenv("SLIPPAGE_PAD_PCT", "0.0005"))
# Berapa lama menunggu limit order terisi sebelum dianggap gagal.
FILL_TIMEOUT_SEC = float(os.getenv("ORDER_FILL_TIMEOUT_SEC", "20"))


def _is_indodax_currency(code: str, entry: dict = None) -> bool:
    """Tebak apakah sebuah kode mata uang adalah aset Indodax (bukan 'free'/'used'/'total')."""
    if not code or len(code) > 10:
        return False
    if not code.replace("_", "").replace("-", "").isalnum():
        return False
    if entry is not None:
        return isinstance(entry, dict) and "free" in entry
    return True


class CCXTSpotBroker:
    """Adapter multi-exchange berbasis CCXT (Indodax, dll) dengan kepatuhan Syariah (Spot 1:1 tanpa margin)."""

    def __init__(self, exchange_id: str, api_key: str, api_secret: str):
        self.exchange_id = exchange_id.lower()
        exchange_class = getattr(ccxt, self.exchange_id)
        self.client = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'timeout': 10000,
            'options': {
                'defaultType': 'spot',
                'createMarketBuyOrderRequiresPrice': True
            }
        })
        self._markets_patched = False
        self._market_loaded_at = 0.0
        self._markets_cache = {}

    # ---------- market helpers ----------

    def _ensure_indodax_markets(self):
        if self.exchange_id == "indodax":
            try:
                markets = self.client.load_markets()
                for sym, m in markets.items():
                    info = m.get("info", {})
                    ticker_id = info.get("ticker_id")
                    if ticker_id:
                        m["id"] = ticker_id
                self._markets_patched = True
                self._market_loaded_at = time.time()
            except Exception as e:
                print(f"[broker_gateway] Peringatan saat memuat market {self.exchange_id}: {e}")

    def _market(self, symbol: str) -> dict:
        if self.exchange_id == "indodax" and (not self._markets_patched or time.time() - self._market_loaded_at > 3600):
            self._ensure_indodax_markets()
        try:
            if not self._markets_cache:
                self._markets_cache = self.client.markets or {}
            return self._markets_cache.get(symbol) or {}
        except Exception:
            return {}

    def _amount_precision(self, symbol: str, volume: float) -> float:
        """Bulatkan volume ke presisi yang diterima exchange.

        Indodax menolak nominal yang bukan kelipatan lot (PAXG lot 0.0001).
        Tanpa pembulatan, volume 0.000438 selalu ditolak dan posisi tidak terbuka.
        """
        market = self._market(symbol)
        min_amount = market.get("limits", {}).get("amount", {}).get("min") or 0.0
        # Lot minimum harus dinaikkan SEBELUM pembulatan presisi. Kalau dibalik,
        # nilai di bawah lot akan dibulatkan ke 0 dan order ditolak exchange.
        target = max(volume, min_amount) if min_amount else volume
        try:
            amount = float(self.client.amount_to_precision(symbol, target))
        except Exception:
            amount = round(target, 8)
        if amount <= 0 and min_amount:
            amount = min_amount
        return round(amount, 8)

    def _split_pair(self, pair: str):
        if "/" in pair:
            base, quote = pair.upper().split("/")
            return base, quote
        if pair.upper().endswith("IDR"):
            return pair[:-3].upper(), "IDR"
        if pair.upper().endswith("USDT"):
            return pair[:-4].upper(), "USDT"
        return pair[:-3].upper(), pair[-3:].upper()

    # ---------- account ----------

    def get_account_balance(self, pair: str = None) -> dict:
        target_pair = pair or os.getenv("TRADING_SYMBOL", "PAXG/IDR")
        base_curr, quote_curr = self._split_pair(target_pair)
        try:
            balances = self.client.fetch_balance()
            cash_bal = float(balances.get(quote_curr, 0.0) or 0.0)
            asset_bal = float(balances.get(base_curr, 0.0) or 0.0)

            if cash_bal == 0.0 and asset_bal == 0.0:
                # Beberapa versi ccxt tidak mengisi kunci mata uang di top-level.
                # Bentuk yang benar: balances[currency] == {"free": ..., "used": ..., "total": ...}
                for code, entry in balances.items():
                    if not _is_indodax_currency(code, entry):
                        continue
                    free_amt = float(entry.get("free") or 0.0)
                    if code.upper() == quote_curr:
                        cash_bal = free_amt
                    elif code.upper() == base_curr:
                        asset_bal = free_amt

            return {
                "cash_balance": cash_bal,
                "asset_balance": asset_bal
            }
        except Exception as e:
            print(f"[broker_gateway] Gagal sinkronisasi saldo {self.exchange_id}: {e}")
            return {"cash_balance": 0.0, "asset_balance": 0.0}

    # ---------- order lifecycle ----------

    def _wait_for_fill(self, order: dict, symbol: str) -> dict:
        """Tunggu limit order terisi tuntas; batalkan bila melewati batas waktu."""
        try:
            if order.get('id'):
                self.client.fetch_order(order['id'], symbol)
        except Exception:
            pass

        deadline = time.time() + FILL_TIMEOUT_SEC
        last = order
        while time.time() < deadline:
            last = self.client.fetch_order(order['id'], symbol) if order.get('id') else order
            status = (last.get('status') or '').lower()
            if status == 'closed':
                return last
            if status == 'canceled':
                break
            time.sleep(1.5)

        try:
            if order.get('id'):
                self.client.cancel_order(order['id'], symbol)
                last = self.client.fetch_order(order['id'], symbol)
        except Exception as e:
            print(f"[broker_gateway] Peringatan saat membatalkan order {order.get('id')}: {e}")

        filled = float(last.get('filled') or 0.0)
        if filled > 0:
            return last
        raise OrderNotFilledError(
            f"Limit order {symbol} tidak terisi dalam {FILL_TIMEOUT_SEC:.0f}s "
            f"(status: {last.get('status')}). Harga pasar lewat dari limit."
        )

    def _real_fill_price(self, order: dict, fallback: float) -> float:
        avg = order.get('average')
        if avg:
            return float(avg)
        cost = order.get('cost')
        filled = float(order.get('filled') or 0.0)
        if cost and filled > 0:
            return float(cost) / filled
        return float(order.get('price') or fallback)

    def _order_fee(self, order: dict) -> float:
        fee = order.get('fee') or {}
        try:
            return float(fee.get('cost') or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _apply_slippage_pad(self, order: dict, side: str, best_price: float, current_price: float) -> dict:
        """Pasang limit order di sisi spread agar cepat terisi."""
        if not best_price or best_price <= 0:
            return order
        market = self._market(order.get('symbol', ''))
        real_symbol = market.get('symbol') or order.get('symbol')
        try:
            self.client.cancel_order(order['id'], real_symbol)
        except Exception:
            order['_cancel_failed'] = True
            return order

        if side == 'buy':
            limit_price = best_price * (1.0 + SLIPPAGE_PAD_PCT)
        else:
            limit_price = best_price * (1.0 - SLIPPAGE_PAD_PCT)

        if "IDR" in real_symbol.upper():
            limit_price = int(round(limit_price))
        else:
            try:
                limit_price = float(self.client.price_to_precision(real_symbol, limit_price))
            except Exception:
                pass

        amount = float(order.get('amount') or order.get('filled') or 0.0)
        try:
            amount = float(self.client.amount_to_precision(real_symbol, amount))
        except Exception:
            pass

        new_order = self.client.create_order(real_symbol, 'limit', side, amount, limit_price)
        print(f"[broker_gateway] Repricing {side} {real_symbol} {amount} @ {limit_price}")
        return new_order

    def _fetch_ticker_prices(self, symbol: str) -> tuple:
        try:
            ticker = self.client.fetch_ticker(symbol)
            return ticker.get('bid'), ticker.get('ask')
        except Exception:
            return None, None

    def _place_and_confirm(self, symbol: str, side: str, amount: float, ref_price: float) -> dict:
        """Buat limit order, pastikan terisi, kembalikan order final dengan fill price nyata."""
        bid, ask = self._fetch_ticker_prices(symbol)
        if side == 'buy':
            limit_price = (ask or ref_price) * (1.0 + SLIPPAGE_PAD_PCT)
        else:
            limit_price = (bid or ref_price) * (1.0 - SLIPPAGE_PAD_PCT)

        if "IDR" in symbol.upper():
            limit_price = int(round(limit_price))
        else:
            try:
                limit_price = float(self.client.price_to_precision(symbol, limit_price))
            except Exception:
                pass

        order = self.client.create_order(symbol, 'limit', side, amount, limit_price)
        try:
            final = self._wait_for_fill(order, symbol)
        except OrderNotFilledError:
            # Coba sekali lagi dengan pad lebih agresif dari harga pasar terkini.
            bid2, ask2 = self._fetch_ticker_prices(symbol)
            fresh = ask2 if side == 'buy' else bid2
            if not fresh:
                raise
            retry = self._apply_slippage_pad(order, side, fresh, ref_price)
            if retry is order and order.get('_cancel_failed'):
                raise
            final = self._wait_for_fill(retry, symbol)
            if not final.get('average') and not final.get('cost'):
                final['average'] = retry.get('average') or ref_price
        return final

    def execute_spot_order(self, pair: str, action: str, volume: float, price: float) -> dict:
        base_curr, quote_curr = self._split_pair(pair)
        symbol = f"{base_curr}/{quote_curr}"
        side = action.lower()

        try:
            if self.exchange_id == "indodax":
                self._ensure_indodax_markets()

            amount = self._amount_precision(symbol, volume)
            order = self._place_and_confirm(symbol, side, amount, price)

            ticket = str(order.get('id') or uuid.uuid4().hex[:8].upper())
            filled = float(order.get('filled') or amount)
            fill_price = self._real_fill_price(order, price)
            fee = self._order_fee(order)
            total_cost = round(fill_price * filled + fee, 2)
            acc = self.get_account_balance(symbol)

            return {
                "ticket": ticket,
                "pair": symbol,
                "action": action.upper(),
                "volume": round(filled, 8),
                "fill_price": fill_price,
                "fee": fee,
                "total_cost": total_cost,
                "remaining_balance": acc.get("cash_balance", 0.0)
            }
        except OrderNotFilledError as e:
            raise BrokerExecutionError(str(e))
        except Exception as e:
            raise BrokerExecutionError(f"Gagal eksekusi live order di {self.exchange_id}: {e}")

    def close_spot_position(self, ticket: str, volume: float, entry_price: float, current_price: float, pair: str = None) -> dict:
        target_pair = pair or os.getenv("TRADING_SYMBOL", "PAXG/IDR")
        base_curr, quote_curr = self._split_pair(target_pair)
        symbol = f"{base_curr}/{quote_curr}"
        try:
            acc_before = self.get_account_balance(symbol)
            avail_asset = acc_before.get("asset_balance", 0.0)
            sell_vol = self._amount_precision(symbol, min(volume, avail_asset) if avail_asset > 0 else volume)
            if sell_vol <= 0:
                raise BrokerExecutionError(f"Volume jual {symbol} nol setelah pembulatan lot.")

            if self.exchange_id == "indodax":
                self._ensure_indodax_markets()

            order = self._place_and_confirm(symbol, 'sell', sell_vol, current_price)

            fill_price = self._real_fill_price(order, current_price)
            filled = float(order.get('filled') or sell_vol)
            sell_fee = self._order_fee(order)
            if sell_fee <= 0.0:
                sell_fee = fill_price * filled * TAKER_FEE_RATE
            buy_fee = entry_price * filled * TAKER_FEE_RATE

            gross_pnl = (fill_price - entry_price) * filled
            pnl = round(gross_pnl - sell_fee - buy_fee, 2)
            acc = self.get_account_balance(symbol)
            return {
                "ticket": ticket,
                "pnl": pnl,
                "fill_price": fill_price,
                "volume": round(filled, 8),
                "fee": round(sell_fee + buy_fee, 2),
                "remaining_balance": acc.get("cash_balance", 0.0)
            }
        except BrokerExecutionError:
            raise
        except Exception as e:
            raise BrokerExecutionError(f"Gagal close live position di {self.exchange_id}: {e}")


class PaperSpotBroker:
    def __init__(self, initial_balance: float = None):
        pair = os.getenv("TRADING_SYMBOL", "PAXG/IDR").upper()
        if initial_balance is None:
            initial_balance = float(os.getenv("PAPER_INITIAL_BALANCE", "150000000.0" if "IDR" in pair else "10000.0"))
        self.balance = initial_balance

    def get_account_balance(self, pair: str = None) -> dict:
        return {"cash_balance": self.balance, "asset_balance": 0.0}

    def execute_spot_order(self, pair: str, action: str, volume: float, price: float) -> dict:
        total_cost = round(volume * price, 2)
        if self.balance < total_cost:
            raise BrokerExecutionError(f"Saldo kas ({self.balance:,.2f}) tidak mencukupi untuk pembelian spot ({total_cost:,.2f}).")

        self.balance = round(self.balance - total_cost, 2)
        ticket = f"TICK-{uuid.uuid4().hex[:8].upper()}"
        return {
            "ticket": ticket,
            "pair": pair,
            "action": action,
            "volume": volume,
            "fill_price": price,
            "fee": round(total_cost * TAKER_FEE_RATE, 2),
            "total_cost": total_cost,
            "remaining_balance": self.balance
        }

    def close_spot_position(self, ticket: str, volume: float, entry_price: float, current_price: float, pair: str = None) -> dict:
        # Fee ikut dihitung supaya angka PAPER tidak lebih optimistis dari LIVE.
        fees = ((entry_price * volume) + (current_price * volume)) * TAKER_FEE_RATE
        pnl = round((current_price - entry_price) * volume - fees, 2)
        total_proceeds = round(volume * current_price, 2)
        self.balance = round(self.balance + total_proceeds, 2)
        return {
            "ticket": ticket,
            "pnl": pnl,
            "fill_price": current_price,
            "volume": volume,
            "fee": round(fees, 2),
            "remaining_balance": self.balance
        }


def get_broker_client():
    mode = os.getenv("TRADING_MODE", "PAPER").upper()
    exchange_id = os.getenv("EXCHANGE_ID", "indodax").lower()
    api_key = os.getenv("EXCHANGE_API_KEY", os.getenv("KRAKEN_API_KEY", ""))
    api_secret = os.getenv("EXCHANGE_SECRET_KEY", os.getenv("KRAKEN_API_SECRET", ""))

    if mode == "LIVE" and api_key and api_secret and "your_" not in api_key and "ISI_" not in api_key:
        print(f"[broker_gateway] 🌐 KONEKSI AKTIF: {exchange_id.upper()} LIVE SPOT BROKER via CCXT (Strict Syariah 1:1)")
        return CCXTSpotBroker(exchange_id=exchange_id, api_key=api_key, api_secret=api_secret)

    # Dulu jatuh ke simulasi secara diam-diam. Sekarang eksplisit dan berisik:
    # mode LIVE tanpa kredensial valid berarti tidak ada trading nyata sama sekali.
    if mode == "LIVE":
        print("[broker_gateway] ⚠️  PERINGATAN KRITIS: TRADING_MODE=LIVE tetapi EXCHANGE_API_KEY / "
              "EXCHANGE_SECRET_KEY kosong atau placeholder. TIDAK ADA order live yang akan dikirim; "
              "semua eksekusi hanya simulasi PAPER.")
        try:
            r = None
            import redis as _redis
            r = _redis.Redis(host=os.getenv("REDIS_HOST", "redis"),
                             port=int(os.getenv("REDIS_PORT", 6379)),
                             password=os.getenv("REDIS_PASSWORD", ""),
                             decode_responses=True)
            import json as _json
            r.publish("hermes:alerts", _json.dumps({
                "type": "SYSTEM_ERROR",
                "level": "ERROR",
                "message": "TRADING_MODE=LIVE tetapi API key exchange tidak valid. Sistem berjalan sebagai PAPER.",
                "timestamp": "startup"
            }))
        except Exception:
            pass
    else:
        print(f"[broker_gateway] 🧪 KONEKSI AKTIF: PAPER SPOT SIMULATOR (Mode: {mode} | Exchange Target: {exchange_id.upper()})")
    return PaperSpotBroker()
