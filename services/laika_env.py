"""Apply LAIka's stored settings (Settings pages) to this process's
environment, before the program reads its configuration.

Every host program imports this first: the stored values (Redis
laika:settings, see apps/api/settings_schema.py) take the place of the
environment variables the program already reads; settings never stored
keep their environment or built-in defaults. LAIKA_SETTINGS_SOURCE=none
(tests) skips it. Never fails a program: no Redis, no settings applied.
"""

import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "apps" / "api"))


def apply():
    if os.environ.get("LAIKA_SETTINGS_SOURCE") == "none":
        return {}
    try:
        import redis as redis_lib
        import laika_redis
        import settings_schema
        client = redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                          password=laika_redis.password(), decode_responses=True,
                                          socket_connect_timeout=2, socket_timeout=2)
        env = settings_schema.environment(client)
    except Exception as exc:  # no Redis yet (first boot): defaults
        print(f"[laika-settings] not applied: {exc}", file=sys.stderr, flush=True)
        return {}
    os.environ.update(env)
    return env


APPLIED = apply()
