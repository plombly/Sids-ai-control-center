"""Several accounts per AI provider, and which one a run uses.

An account is a Claude or Codex subscription sign-in or a pay-per-use API
key. LAIka spreads runs over a provider's accounts: each run gets the
account with the most room left (fewest runs going on relative to its
limit, then lowest plan usage). A provider is paused only when all its
accounts are. Standard library only (worker, orchestrator, goal assistant,
host scripts, and the dashboard's API through its own copy of the reads).

Registry (Redis, written by the dashboard's API, administrators only):
  laika:provider-accounts:<provider>            set of account ids
  laika:provider-account:<provider>:<id>        {label, kind login|key,
                                                 max_concurrent (0 = no limit),
                                                 enabled "1"|"", created_at}
"default" always exists: the sign-in and key LAIka had before accounts
(~/.codex, ~/.config/laika/claude-token, providers.env). Its pause and
usage keep their old Redis names.

Credentials live in the laika user's home, written by
scripts/laika-providers.py (never in Redis):
  claude login   ~/.config/laika/claude-tokens/<id>   (default: claude-token)
  claude key     ~/.config/laika/claude-keys/<id>     (default: ANTHROPIC_API_KEY)
  codex          CODEX_HOME ~/.codex-accounts/<id>    (default: ~/.codex)

Running runs per account: laika:provider-slots:<provider>:<id> (sorted set
of holders, scored by lease expiry; the default account: without ":<id>"),
so a crashed holder frees itself.
"""

import json
import os
import pwd
import re
import time
from pathlib import Path

PROVIDERS = ("claude", "codex")
DEFAULT = "default"
KINDS = ("login", "key")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def default_limit(provider):
    """A new account's concurrent runs: Claude CLAUDE_MAX_CONCURRENT (2),
    Codex no limit (0)."""
    if provider == "claude":
        try:
            return max(1, int(os.getenv("CLAUDE_MAX_CONCURRENT", "2")))
        except ValueError:
            return 2
    return 0

_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local limit = tonumber(ARGV[4])
if redis.call('ZSCORE', KEYS[1], ARGV[3]) or limit <= 0 or redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
  return 1
end
return 0
"""


def home():
    return Path(os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir)


def account_key(provider, account):
    return f"laika:provider-account:{provider}:{account}"


def running_key(provider, account):
    """The default account keeps the key Claude's slots always had."""
    return f"laika:provider-slots:{provider}" + ("" if account == DEFAULT else f":{account}")


def accounts(r, provider):
    """[{id, label, kind, max_concurrent, enabled}] with "default" first."""
    found = {DEFAULT: {"id": DEFAULT, "label": "Default", "kind": "login",
                       "max_concurrent": default_limit(provider), "enabled": True}}
    try:
        ids = sorted(r.smembers(f"laika:provider-accounts:{provider}") or [])
    except Exception:
        ids = []
    for account in [DEFAULT, *ids]:
        try:
            record = r.hgetall(account_key(provider, account)) or {}
        except Exception:
            record = {}
        if not record and account != DEFAULT:
            continue
        base = found.get(account, {"id": account, "label": account, "kind": "login", "enabled": True,
                                   "max_concurrent": default_limit(provider)})
        if record:
            base = {**base,
                    "label": record.get("label") or base["label"],
                    "kind": record.get("kind") if record.get("kind") in KINDS else base["kind"],
                    "max_concurrent": _int(record.get("max_concurrent"), base["max_concurrent"]),
                    "enabled": record.get("enabled", "1") == "1"}
        found[account] = base
    return list(found.values())


def stop_percent(r, provider, account):
    """Plan usage at which LAIka stops using this account until its window
    resets: the account's own "stop at" (e.g. lower on the plan the owner
    also uses by hand), else CLAUDE_EARLY_HOLD_PERCENT (95)."""
    try:
        value = (r.hgetall(account_key(provider, account or DEFAULT)) or {}).get("stop_percent")
    except Exception:
        value = None
    try:
        if value not in (None, ""):
            return min(100.0, max(10.0, float(value)))
    except ValueError:
        pass
    try:
        return float(os.getenv("CLAUDE_EARLY_HOLD_PERCENT", "95"))
    except ValueError:
        return 95.0


def _int(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def running(r, provider, account, now=None):
    now = time.time() if now is None else now
    try:
        return int(r.zcount(running_key(provider, account), now, "+inf"))
    except Exception:
        return 0


def usage_percent(r, provider, account):
    """The highest plan-usage window of the account's last reading, or 0."""
    import provider_health
    try:
        reading = json.loads(r.get(provider_health.usage_key(provider, account)) or "{}")
    except (ValueError, TypeError):
        return 0.0
    except Exception:
        return 0.0
    return max([float(w.get("percent") or 0) for w in (reading.get("windows") or {}).values()] or [0.0])


def credential_home():
    """Where the accounts' credentials are: the laika user's home (also when
    a root service such as the scaler asks), else this user's."""
    try:
        import laika_user
        entry = laika_user.account()
        if entry is not None:
            return Path(entry.pw_dir)
    except Exception:
        pass
    return home()


def has_credentials(provider, account, base=None):
    """An extra account is used only once it is signed in or has its key
    (never silently as the default sign-in)."""
    if account == DEFAULT:
        return True
    base = base or credential_home()
    if provider == "claude":
        return bool(_read(claude_key_path(account, base)) or _read(claude_token_path(account, base)))
    return (codex_home(account, base) / "auth.json").is_file()


def candidates(r, provider, now=None, base=None):
    """Enabled, signed-in accounts not paused, best first (most room left)."""
    import provider_health
    now = time.time() if now is None else now
    usable = []
    for account in accounts(r, provider):
        if not account["enabled"] or provider_health.held(r, provider, account["id"]):
            continue
        if not has_credentials(provider, account["id"], base):
            continue
        busy = running(r, provider, account["id"], now)
        limit = account["max_concurrent"]
        load = busy / limit if limit else busy / 100.0
        usable.append((load, usage_percent(r, provider, account["id"]), account["id"] != DEFAULT, account["id"]))
    return [account for *_, account in sorted(usable)]


def acquire(r, provider, holder, lease_seconds, now=None, base=None):
    """An account id with a running slot now held by holder, or None when
    every usable account is full (or all are paused)."""
    now = time.time() if now is None else now
    limits = {a["id"]: a["max_concurrent"] for a in accounts(r, provider)}
    for account in candidates(r, provider, now, base):
        try:
            got = r.eval(_ACQUIRE, 1, running_key(provider, account), str(now), str(now + lease_seconds),
                         holder, str(limits.get(account, 0)))
        except Exception:
            return account  # bookkeeping must never stop the pipeline
        if got:
            return account
    return None


def wait(r, provider, holder, lease_seconds, max_wait, tick=None, sleep=time.sleep, poll=2.0):
    """acquire(), retried until max_wait seconds have passed: the account id,
    or None."""
    deadline = time.time() + max_wait
    while True:
        account = acquire(r, provider, holder, lease_seconds)
        if account:
            return account
        if time.time() >= deadline:
            return None
        if tick:
            try:
                tick()
            except Exception:
                pass
        sleep(poll)


def release(r, provider, account, holder):
    if not account:
        return
    try:
        r.zrem(running_key(provider, account), holder)
    except Exception:
        pass


def capacity(r, provider, now=None):
    """(free slots, total slots or 0 = unlimited) over usable accounts."""
    now = time.time() if now is None else now
    free = total = 0
    unlimited = False
    import provider_health
    for account in accounts(r, provider):
        if not account["enabled"] or provider_health.held(r, provider, account["id"]):
            continue
        if not has_credentials(provider, account["id"]):
            continue
        limit = account["max_concurrent"]
        if not limit:
            unlimited = True
            continue
        total += limit
        free += max(0, limit - running(r, provider, account["id"], now))
    return (10 ** 6 if unlimited else free), (0 if unlimited else total)


# --- credentials (files in the laika user's home) -------------------------------------

def claude_token_path(account, base=None):
    base = Path(base or home())
    if account == DEFAULT:
        return base / ".config" / "laika" / "claude-token"
    return base / ".config" / "laika" / "claude-tokens" / account


def claude_key_path(account, base=None):
    return Path(base or home()) / ".config" / "laika" / "claude-keys" / account


def codex_home(account, base=None):
    base = Path(base or home())
    return base / ".codex" if account == DEFAULT else base / ".codex-accounts" / account


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def env_for(provider, account, base=None):
    """Environment additions for running provider's CLI as account."""
    account = account or DEFAULT
    env = {}
    if provider == "claude":
        key = _read(claude_key_path(account, base))
        token = _read(claude_token_path(account, base))
        if key:
            env["ANTHROPIC_API_KEY"] = key
            env["CLAUDE_CODE_OAUTH_TOKEN"] = ""
        elif token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            if account != DEFAULT:
                env["ANTHROPIC_API_KEY"] = ""  # this account's login, not the default key
    elif provider == "codex":
        if account != DEFAULT:
            env["CODEX_HOME"] = str(codex_home(account, base))
    return env


def apply_env(env, additions):
    """env with additions applied; an empty value removes the variable."""
    merged = dict(env)
    for name, value in additions.items():
        if value:
            merged[name] = value
        else:
            merged.pop(name, None)
    return merged
