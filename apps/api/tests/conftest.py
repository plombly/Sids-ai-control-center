import os

# Tests never read the live server's stored settings (services/laika_env.py).
os.environ["LAIKA_SETTINGS_SOURCE"] = "none"
# Most tests run without an administrator account: keep the API unlocked
# (tests/test_auth.py turns the lock on).
os.environ["LAIKA_SETUP_LOCK"] = "0"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Tests never connect to production databases or load .env.
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['REDIS_URL'] = 'redis://localhost:6379/15'
# Tests mutate fake Redis between requests: no snapshot cache.
os.environ['API_SNAPSHOT_TTL'] = '0'
