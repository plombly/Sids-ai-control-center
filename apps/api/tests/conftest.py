import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Tests never connect to production databases or load .env.
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['REDIS_URL'] = 'redis://localhost:6379/15'
# Tests mutate fake Redis between requests: no snapshot cache.
os.environ['API_SNAPSHOT_TTL'] = '0'
