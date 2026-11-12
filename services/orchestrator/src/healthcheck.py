import sys
import os
import time
import sqlite3

# Cek koneksi ke database SQLite
db_path = os.getenv("DB_PATH", "/app/data/trading.db")
if not os.path.exists(db_path):
    sys.exit(1)

try:
    conn = sqlite3.connect(db_path, timeout=3)
    conn.execute("SELECT 1 FROM orders LIMIT 1")
    conn.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
