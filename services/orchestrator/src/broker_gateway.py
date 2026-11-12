# -*- coding: utf-8 -*-
import os
import uuid
import ccxt

class BrokerExecutionError(Exception):
    pass

class CCXTSpotBroker:
    """Adapter multi-exchange berbasis CCXT (Indodax, Tokocrypto, dll) dengan kepatuhan Syariah (Spot 1:1 tanpa margin)."""
    def __init__(self, exchange_id: str, api_key: str, api_secret: str):
        self.exchange_id = exchange_id.lower()
        exchange_class = getattr(ccxt, self.exchange_id)
        self.client = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'createMarketBuyOrderRequiresPrice': True
            }
        })

    def _split_pair(self, pair: str):
        if "/" in pair:
            base, quote = pair.upper().split("/")
            return base, quote
        if pair.upper().endswith("IDR"):
            return pair[:-3].upper(), "IDR"
        if pair.upper().endswith("USDT"):
            return pair[:-4].upper(), "USDT"
        return pair[:-3].upper(), pair[-3:].upper()

    def get_account_balance(self, pair: str = None) -> dict:
        """Mengambil saldo kas tunai (IDR/USDT) dan saldo aset (PAXG) dari dompet exchange."""
        target_pair = pair or os.getenv("TRADING_SYMBOL", "PAXG/IDR")
        base_curr, quote_curr = self._split_pair(target_pair)
        try:
            balances = self.client.fetch_balance()
            free_bal = balances.get("free", {})
            cash_bal = float(free_bal.get(quote_curr, 0.0))
            asset_bal = float(free_bal.get(base_curr, 0.0))
            return {
                "cash_balance": cash_bal,
                "asset_balance": asset_bal
            }
        except Exception as e:
            print(f"[broker_gateway] Gagal sinkronisasi saldo {self.exchange_id}: {e}")
            return {"cash_balance": 0.0, "asset_balance": 0.0}

    def execute_spot_order(self, pair: str, action: str, volume: float, price: float) -> dict:
        """Eksekusi order spot fisik 1:1 tanpa leverage."""
        symbol = pair if "/" in pair else f"{self._split_pair(pair)[0]}/{self._split_pair(pair)[1]}"
        side = action.lower()
        try:
            # Untuk market buy di Indodax, CCXT menghitung total IDR (volume * price)
            order = self.client.create_order(symbol, "market", side, volume, price)
            ticket = str(order.get("id") or uuid.uuid4().hex[:8].upper())
            total_cost = round(volume * price, 2)
            acc = self.get_account_balance(symbol)

            return {
                "ticket": ticket,
                "pair": symbol,
                "action": action.upper(),
                "volume": volume,
                "fill_price": price,
                "total_cost": total_cost,
                "remaining_balance": acc.get("cash_balance", 0.0)
            }
        except Exception as e:
            raise BrokerExecutionError(f"Gagal eksekusi live order di {self.exchange_id}: {e}")

    def close_spot_position(self, ticket: str, volume: float, entry_price: float, current_price: float, pair: str = None) -> dict:
        """Eksekusi penjualan spot fisik (market sell) untuk merealisasikan PnL."""
        target_pair = pair or os.getenv("TRADING_SYMBOL", "PAXG/IDR")
        symbol = target_pair if "/" in target_pair else f"{self._split_pair(target_pair)[0]}/{self._split_pair(target_pair)[1]}"
        try:
            self.client.create_order(symbol, "market", "sell", volume)
            pnl = round((current_price - entry_price) * volume, 2)
            acc = self.get_account_balance(symbol)
            return {
                "ticket": ticket,
                "pnl": pnl,
                "remaining_balance": acc.get("cash_balance", 0.0)
            }
        except Exception as e:
            raise BrokerExecutionError(f"Gagal close live position di {self.exchange_id}: {e}")


class PaperSpotBroker:
    """Simulator internal untuk paper trading aman tanpa menyentuh saldo riil."""
    def __init__(self, initial_balance: float = None):
        pair = os.getenv("TRADING_SYMBOL", "PAXG/IDR").upper()
        if initial_balance is None:
            # Jika pair IDR, gunakan simulasi Rp 150.000.000 (~$10,000), jika USD gunakan 10000.0
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
            "total_cost": total_cost,
            "remaining_balance": self.balance
        }

    def close_spot_position(self, ticket: str, volume: float, entry_price: float, current_price: float, pair: str = None) -> dict:
        pnl = round((current_price - entry_price) * volume, 2)
        total_proceeds = round(volume * current_price, 2)
        self.balance = round(self.balance + total_proceeds, 2)
        return {
            "ticket": ticket,
            "pnl": pnl,
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
    else:
        print(f"[broker_gateway] 🧪 KONEKSI AKTIF: PAPER SPOT SIMULATOR (Mode: {mode} | Exchange Target: {exchange_id.upper()})")
        return PaperSpotBroker()
