import os
import sys
import time
import json
import threading
import redis
from syariah_guard import SyariahGuard, SyariahComplianceError
from news_filter import NewsBlackoutFilter

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

try:
    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        decode_responses=True
    )
    r.ping()
    print("[workers] Terhubung ke Redis event broker.")
except Exception as e:
    print(f"[workers] Gagal terkoneksi ke Redis: {e}")
    sys.exit(1)

guard = SyariahGuard()
news_filter = NewsBlackoutFilter()

def listen_emergency_events():
    pubsub = r.pubsub()
    pubsub.subscribe("hermes:events")
    for message in pubsub.listen():
        if message["type"] == "message":
            data = message["data"]
            if data == "KILL_SWITCH_TRIGGERED":
                print("[workers] 🚨 EMERGENCY RECEIVED: Menghentikan pemrosesan!")
            elif data == "TRADING_RESUMED":
                print("[workers] ✅ RESUME RECEIVED: Siaga memproses order.")

def evaluate_and_respond():
    """Worker loop untuk membaca antrean proposal order dari Orchestrator"""
    print("[workers] Worker Queue Processor aktif mendengarkan 'hermes:trade_queue'...")
    while True:
        # Blocking pop dengan timeout 2 detik
        item = r.blpop("hermes:trade_queue", timeout=2)
        if not item:
            continue
            
        _, raw_payload = item
        trade_payload = json.loads(raw_payload)
        trade_id = trade_payload.get("trade_id", "unknown")
        resp_channel = f"hermes:trade_result:{trade_id}"

        # 1. Cek Kill-Switch
        if r.get("hermes:kill_switch") == "active":
            res = {"status": "REJECTED", "reason": "Emergency Kill-Switch aktif."}
            r.publish(resp_channel, json.dumps(res))
            print(f"[workers] Trade #{trade_id} DITOLAK: Kill-Switch aktif.")
            continue

        # 2. Cek News Filter
        in_blackout, reason = news_filter.is_in_blackout()
        if in_blackout:
            res = {"status": "REJECTED", "reason": reason}
            r.publish(resp_channel, json.dumps(res))
            print(f"[workers] Trade #{trade_id} DITOLAK: {reason}")
            continue

        # 3. Validasi Syariah
        try:
            guard.validate_trade(trade_payload)
            res = {"status": "APPROVED", "trade_id": trade_id}
            r.publish(resp_channel, json.dumps(res))
            print(f"[workers] Trade #{trade_id} [{trade_payload['pair']}] VERIFIKASI LOLOS (Syariah Compliant).")
        except SyariahComplianceError as err:
            res = {"status": "REJECTED", "reason": str(err)}
            r.publish(resp_channel, json.dumps(res))
            print(f"[workers] Trade #{trade_id} [{trade_payload['pair']}] DITOLAK SYARIAH: {err}")

if __name__ == "__main__":
    t_event = threading.Thread(target=listen_emergency_events, daemon=True)
    t_event.start()

    evaluate_and_respond()
