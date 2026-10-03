#!/usr/bin/env python3
"""Administrator account tools for the host (root only).

    laika-admin.py setup-code       print a one-time code for the web setup
                                    (valid 24 hours; only while no
                                    administrator exists)
    laika-admin.py reset-password [USER]  set a new random password (default:
                                          the first administrator),
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
    import access
    if access.any_user(r):
        print("The administrator account already exists. Sign in, or use reset-password.", file=sys.stderr)
        return 1
    code, digest = new_code()
    r.set("laika:setup:code", digest, ex=CODE_TTL)
    print(code)
    return 0


def reset_password(r, username=""):
    """A new password for one account (default: the first administrator);
    that person is signed out everywhere."""
    import access
    import auth
    access.migrate(r)
    if username:
        account = access.get_user(r, username)
    else:
        account = next(iter(u for u in access.all_users(r) if u.get("role") == "admin"), None)
    if not account:
        print("No such account. With no account yet: use setup-code and the web setup.", file=sys.stderr)
        return 1
    password = "-".join("".join(secrets.choice(ALPHABET.lower()) for _ in range(5)) for _ in range(4))
    access.save_user(r, account["name"], password=auth.hash_password(password), password_reset_at=time.time(), disabled="")
    auth.end_sessions(r, user=account["name"])
    print(f"user: {account.get('username')}\npassword: {password}\n(change it in Settings → Access after signing in)")
    return 0


def main(argv, r=None):
    action = argv[1] if len(argv) > 1 else ""
    if action not in ("setup-code", "reset-password"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    r = r or redis_client()
    return setup_code(r) if action == "setup-code" else reset_password(r, argv[2] if len(argv) > 2 else "")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
