# -*- coding: utf-8 -*-
import os
import time
import base64
import hashlib
import hmac
import urllib.parse
import urllib.request
import json
import uuid

class BrokerExecutionError(Exception):
    pass

class KrakenSpotBroker:
    """Adapter resmi Kraken Spot API dengan standar keamanan HMAC-SHA512 dan kepatuhan syariah (tanpa margin)."""
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.kraken.com"

    def _get_kraken_signature(self, urlpath: str, data: dict) -> str:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data['nonce']) + postdata).encode('utf-8')
        message = urlpath.encode('utf-8') + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        sigdigest = base64.b64encode(mac.digest())
        return sigdigest.decode('utf-8')

    def _query_private(self, endpoint: str, data: dict = None) -> dict:
        if data is None:
            data = {}
        data['nonce'] = int(time.time() * 1000)
        urlpath = f"/0/private/{endpoint}"
        headers = {
            'API-Key': self.api_key,
            'API-Sign': self._get_kraken_signature(urlpath, data),
            'User-Agent': 'Hermes-Syariah-Core/1.0'
        }
        
        req_data = urllib.parse.urlencode(data).encode('utf-8')
        req = urllib.request.Request(f"{self.base_url}{urlpath}", data=req_data, headers=headers)
        
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get("error"):
                    raise BrokerExecutionError(f"Kraken API Error: {result['error']}")
                return result.get("result", {})
        except Exception as e:
            raise BrokerExecutionError(f"Gagal koneksi private Kraken: {e}")

    def get_account_balance(self) -> dict:
        """Mengambil saldo kas tunai USD nyata dari dompet Kraken."""
        try:
            balances = self._query_private("Balance")
            # Kraken memakai ZUSD untuk USD fiat, atau USD
            usd_bal = float(balances.get("ZUSD", balances.get("USD", 0.0)))
            paxg_bal = float(balances.get("PAXG", 0.0))
            return {
                "cash_balance": usd_bal,
                "asset_balance": paxg_bal
            }
        except Exception as e:
            print(f"[broker_gateway] Gagal sinkronisasi saldo Kraken: {e}")
            return {"cash_balance": 0.0, "asset_balance": 0.0}

    def execute_spot_order(self, pair: str, action: str, volume: float, price: float) -> dict:
        """Eksekusi order spot beli fisik tanpa leverage."""
        kraken_pair = "PAXGUSD"
        data = {
            "pair": kraken_pair,
            "type": action.lower(),
            "ordertype": "market",
            "volume": str(volume)
            # Tanpa parameter leverage (Syariah: strictly 1:1 spot ownership)
        }
        
        try:
            res = self._query_private("AddOrder", data)
            txids = res.get("txid", [str(uuid.uuid4())[:8]])
            ticket = txids[0]
            total_cost = round(volume * price, 2)
            acc = self.get_account_balance()

            return {
                "ticket": ticket,
                "pair": pair,
                "action": action,
                "volume": volume,
                "fill_price": price,
                "total_cost": total_cost,
                "remaining_balance": acc.get("cash_balance", 0.0)
            }
        except Exception as e:
            raise BrokerExecutionError(f"Gagal eksekusi live order: {e}")

    def close_spot_position(self, ticket: str, volume: float, entry_price: float, current_price: float) -> dict:
        """Eksekusi penjualan spot fisik (market sell) untuk merealisasikan PnL."""
        kraken_pair = "PAXGUSD"
        data = {
            "pair": kraken_pair,
            "type": "sell",
            "ordertype": "market",
            "volume": str(volume)
        }
        
        try:
            self._query_private("AddOrder", data)
            pnl = round((current_price - entry_price) * volume, 2)
            acc = self.get_account_balance()
            return {
                "ticket": ticket,
                "pnl": pnl,
                "remaining_balance": acc.get("cash_balance", 0.0)
            }
        except Exception as e:
            raise BrokerExecutionError(f"Gagal close live position: {e}")


class PaperSpotBroker:
    """Simulator internal untuk paper trading aman tanpa menyentuh saldo riil."""
    def __init__(self, initial_balance: float = 10000.0):
        self.balance = initial_balance

    def get_account_balance(self) -> dict:
        return {"cash_balance": self.balance, "asset_balance": 0.0}

    def execute_spot_order(self, pair: str, action: str, volume: float, price: float) -> dict:
        total_cost = round(volume * price, 2)
        if self.balance < total_cost:
            raise BrokerExecutionError("Saldo kas tidak mencukupi untuk pembelian spot 1:1.")
        
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

    def close_spot_position(self, ticket: str, volume: float, entry_price: float, current_price: float) -> dict:
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
    api_key = os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_API_SECRET", "")

    if mode == "LIVE" and api_key and api_secret and "ISI_" not in api_key:
        print("[broker_gateway] 🌐 KONEKSI AKTIF: KRAKEN LIVE SPOT BROKER (Strict Syariah 1:1)")
        return KrakenSpotBroker(api_key=api_key, api_secret=api_secret)
    else:
        print("[broker_gateway] 🧪 KONEKSI AKTIF: PAPER SPOT SIMULATOR (Sandbox Safe Mode)")
        return PaperSpotBroker()
