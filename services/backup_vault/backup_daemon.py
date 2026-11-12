import os
import time
import shutil
import sqlite3
import datetime
import redis

DATA_DIR = "/app/data"
BACKUP_DIR = "/app/backups"
DB_FILE = os.path.join(DATA_DIR, "trading.db")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

def run_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    target_db = os.path.join(BACKUP_DIR, f"trading_backup_{timestamp}.db")

    # 1. Safe SQLite Hot-Backup
    if os.path.exists(DB_FILE):
        try:
            src = sqlite3.connect(DB_FILE)
            dst = sqlite3.connect(target_db)
            with dst:
                src.backup(dst, pages=1, progress=None)
            dst.close()
            src.close()
            print(f"[backup] SQLite snapshot berhasil disimpan: {target_db}")
        except Exception as e:
            print(f"[backup] Gagal snapshot SQLite: {e}")
    else:
        print("[backup] File trading.db tidak ditemukan, melewati SQLite backup.")

    # 2. Trigger Redis BGSAVE
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD)
        r.bgsave()
        print("[backup] Redis BGSAVE berhasil dipicu.")
    except Exception as e:
        print(f"[backup] Gagal memicu Redis BGSAVE: {e}")

    # 3. Rotasi Backup (Hapus file lebih dari 7 hari)
    now = time.time()
    retention_period = 7 * 86400
    for fname in os.listdir(BACKUP_DIR):
        fpath = os.path.join(BACKUP_DIR, fname)
        if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > retention_period):
            os.remove(fpath)
            print(f"[backup] Menghapus backup usang: {fname}")

if __name__ == "__main__":
    print("[backup] Hermes Backup Vault Daemon aktif. Interval backup: 6 jam.")
    time.sleep(10)
    run_backup()
    while True:
        time.sleep(21600)  # 6 jam
        run_backup()
