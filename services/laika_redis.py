"""The Redis password for LAIka's host-side programs.

Redis requires a password once /etc/laika/redis.env exists (root-only,
created by scripts/enable-redis-password.sh). Every LAIka program passes
password() when it connects; the API container gets the same file through
docker-compose. Project sandboxes hide /etc/laika, so project code cannot
read it. Without the file (or REDIS_PASSWORD), connections carry no password.
"""

import os

ENV_FILE = os.environ.get("LAIKA_REDIS_ENV", "/etc/laika/redis.env")


def password():
    """REDIS_PASSWORD from the environment, else from ENV_FILE, else None."""
    value = os.environ.get("REDIS_PASSWORD")
    if value:
        return value
    try:
        with open(ENV_FILE) as handle:
            for line in handle:
                key, sep, rest = line.strip().partition("=")
                if sep and key.strip() == "REDIS_PASSWORD":
                    return rest.strip().strip("'\"") or None
    except OSError:
        pass
    return None
