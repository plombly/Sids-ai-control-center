#!/usr/bin/env python3
"""Administrator account tools for the host (root only).

    laika-admin.py setup-code       print a one-time code for the web setup
                                    (valid 24 hours; only while no
                                    administrator exists)
    laika-admin.py reset-password   set a new random administrator password,
                                    sign out every session, print it once
"""

import hashlib
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.append(str(ROOT / "apps/api"))
import laika_env  # noqa: E402,F401

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CODE_TTL = 24 * 3600


def redis_client():
    import os
    import redis as redis_lib
    import laika_redis
    return redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                                    password=laika_redis.password(), decode_responses=True)


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}", hashlib.sha256(raw.encode()).hexdigest()


def setup_code(r):
    if r.hgetall("laika:auth:admin"):
        print("The administrator account already exists. Sign in, or use reset-password.", file=sys.stderr)
        return 1
    code, digest = new_code()
    r.set("laika:setup:code", digest, ex=CODE_TTL)
    print(code)
    return 0


def reset_password(r):
    import auth
    account = r.hgetall("laika:auth:admin")
    if not account:
        print("No administrator yet: use setup-code and the web setup.", file=sys.stderr)
        return 1
    password = "-".join("".join(secrets.choice(ALPHABET.lower()) for _ in range(5)) for _ in range(4))
    r.hset("laika:auth:admin", mapping={"password": auth.hash_password(password), "password_reset_at": str(time.time())})
    for sid in list(r.smembers("laika:session-ids") or []):
        r.delete(f"laika:sessions:{sid}")
        r.srem("laika:session-ids", sid)
    print(f"user: {account.get('username')}\npassword: {password}\n(change it in Settings → Access after signing in)")
    return 0


def main(argv, r=None):
    action = argv[1] if len(argv) > 1 else ""
    if action not in ("setup-code", "reset-password"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    r = r or redis_client()
    return setup_code(r) if action == "setup-code" else reset_password(r)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
