import os

class SyariahComplianceError(Exception):
    pass

class SyariahGuard:
    def __init__(self):
        self.strict_mode = os.getenv("SHARIAH_STRICT_MODE", "true").lower() == "true"
        self.max_leverage = float(os.getenv("MAX_LEVERAGE_RATIO", "1.0"))

    def validate_trade(self, trade_proposal: dict) -> dict:
        # 1. Cek Jenis Kontrak (Wajib Spot)
        order_type = trade_proposal.get("contract_type", "").upper()
        if order_type != "SPOT":
            raise SyariahComplianceError(f"Pelanggaran Syariah: Kontrak '{order_type}' ditolak. Hanya transaksi SPOT yang diizinkan.")

        # 2. Cek Biaya Swap / Rollover Fee (Riba)
        swap_rate = trade_proposal.get("swap_fee", 0.0)
        if swap_rate > 0.0 or trade_proposal.get("is_swap_free") is False:
            raise SyariahComplianceError("Pelanggaran Syariah: Terdeteksi bunga menginap/swap (Riba).")

        # 3. Cek Batasan Leverage (Rasio 1:1)
        leverage = trade_proposal.get("leverage", 1.0)
        if leverage > self.max_leverage:
            raise SyariahComplianceError(f"Pelanggaran Syariah: Leverage {leverage}x melebihi batas maksimal {self.max_leverage}x.")

        # 4. Validasi Underlying Spot Asset
        asset_pair = trade_proposal.get("pair", "").upper()
        allowed_pairs = [
            "EURUSD", "GBPUSD", "USDJPY", "XAUUSD",
            "PAXGUSDT", "PAXG/USDT", "PAXGUSD", "PAXG/USD",
            "PAXGIDR", "PAXG/IDR", "BTCUSDT", "BTC/USDT", "BTCIDR", "BTC/IDR", "USDT/IDR", "USDTIDR"
        ]
        if asset_pair not in allowed_pairs:
            raise SyariahComplianceError(f"Pelanggaran Syariah: Pair '{asset_pair}' tidak masuk daftar underlying spot terverifikasi.")

        return {
            "status": "APPROVED",
            "compliance_checked": True,
            "pair": asset_pair,
            "leverage": leverage
        }
