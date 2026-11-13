import redis
import time
import logging

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')

import os
REDIS_HOST = os.getenv("REDIS_HOST", "aidocumines_redis")
REDIS_PORT = 6379
CHECK_INTERVAL = 30  # seconds

def promote_if_replica():
    try:
        r = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT)
        info = r.info("replication")
        role = info.get("role")

        if role == "slave" or role == "replica":
            logging.warning("⚠️ Redis is in replica mode! Promoting to master...")
            r.slaveof(no_one=True)
            logging.info("✅ Redis promoted to master.")
        else:
            logging.info("✔️ Redis is in master mode.")
    except Exception as e:
        logging.error(f"❌ Error checking Redis: {e}")

if __name__ == "__main__":
    while True:
        promote_if_replica()
        time.sleep(CHECK_INTERVAL)

