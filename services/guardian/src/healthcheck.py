import sys
import os
import redis

# Cek apakah Guardian masih terhubung ke Redis event broker
redis_host = os.getenv("REDIS_HOST", "redis")
redis_port = int(os.getenv("REDIS_PORT", 6379))
redis_pass = os.getenv("REDIS_PASSWORD", "")

try:
    r = redis.Redis(host=redis_host, port=redis_port, password=redis_pass, socket_timeout=3)
    r.ping()
    sys.exit(0)
except Exception:
    sys.exit(1)
