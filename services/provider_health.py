"""Why an AI provider could not serve a call, and when to try it again.

Standard library only: used by the worker, the orchestrator, the goal
assistant and the dashboard API.

classify(text, status) sorts a failed Claude or Codex call into
  quota        the plan's usage allowance is used up (5-hour, weekly, credits)
  rate         too many requests right now
  auth         the CLI is not signed in, or its sign-in expired
  unavailable  overloaded, or the CLI is missing / mid-update
  failure      anything else: the task itself failed (never a hold)
and reads the reset time the provider gave ("try again in 2h 5m",
"resets 3:48 AM (America/Chicago)", "Oct 21st 3:48 AM", Retry-After, an
epoch after "limit reached|"). Only the provider's explicit wording counts:
a successful run is never classified.

A hold stops sending work to that account of the provider until the reset
time (+2 min margin), capped (rate limits 24 h, quotas 8 days); see "holds
in Redis" below for the keys.
"""

import datetime
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redaction  # noqa: E402

KINDS = ("quota", "rate", "auth", "unavailable", "failure")
HOLD_KINDS = ("quota", "rate", "auth", "unavailable")
MARGIN = 120
DEFAULT_SECONDS = {"quota": 3600, "rate": 300, "auth": 1800, "unavailable": 300}
CAP_SECONDS = {"quota": 8 * 86400, "rate": 86400, "auth": 86400, "unavailable": 3600}
INSTALL_SECONDS = 120   # the CLI updating itself: a short pause only

_QUOTA = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:hit|reached|exceeded) (?:your|the) (?:session|usage|weekly|daily|monthly|5-hour) limit\b",
    r"\busage limit (?:reached|exceeded|hit)\b",
    r"\b(?:5-hour|five-hour|daily|weekly|monthly|session) (?:usage )?limit (?:reached|exceeded|hit)\b",
    r"\bexceeded (?:your|the) (?:\w+ )?quota\b",
    r"\bquota (?:has been |was )?(?:exceeded|exhausted|reached)\b",
    r"\binsufficient[_ ]quota\b",
    r"\bcredit balance is too low\b",
    r"\b(?:out of|no remaining|insufficient) credits\b",
    r"\blimit reached\|\d{10}\b",
)]
_RATE = [re.compile(p, re.IGNORECASE) for p in (
    r"\brate[ _-]?limit(?:ed|_error| exceeded| reached| hit)\b",
    r"\b(?:hit|reached|exceeded) (?:a|the|your) rate[ _-]?limit\b",
    r"\btoo many requests\b",
    r"\b(?:status|http|code|error)\W{0,3}429\b",
)]
_AUTH = [re.compile(p, re.IGNORECASE) for p in (
    r"\bnot (?:logged|signed) in\b",
    r"\bplease (?:run )?/login\b",
    r"\bplease (?:log ?in|sign in|re-?authenticate) again\b",
    r"\b(?:authentication|login) (?:is )?required\b",
    r"\bauthentication failed\b",
    # Codex with a sign-in it cannot use (seen with a broken auth.json, 0.159).
    r"\bplan type is required for chatgpt authentication\b",
    r"\bunauthori[sz]ed\b",
    r"\b(?:status|http|code|error)\W{0,3}401\b",
    r"\binvalid (?:api[ _-]?key|x-api-key|access token|bearer token|credentials)\b",
    r"\b(?:access token|refresh token|session|credentials?|login|oauth token) (?:has )?(?:expired|been revoked)\b",
)]
_UNAVAILABLE = [re.compile(p, re.IGNORECASE) for p in (
    r"\boverloaded\b",
    r"\b(?:status|http|code|error)\W{0,3}(?:529|503)\b",
    r"\bservice (?:is )?(?:temporarily )?unavailable\b",
)]
INSTALL_BROKEN = re.compile(
    r"postinstall|optional dependency|No such file or directory|command not found|ENOENT|cannot execute",
    re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Failure:
    def __init__(self, kind, reason="", until=None, hint="absent"):
        self.kind = kind          # one of KINDS
        self.reason = reason      # short, readable, redacted
        self.until = until        # epoch seconds the provider may be tried again, or None
        self.hint = hint          # parsed | absent | stale (a reset time in the past)

    @property
    def holds(self):
        return self.kind in HOLD_KINDS

    def __repr__(self):
        return f"Failure({self.kind!r}, until={self.until!r}, hint={self.hint!r})"


def clean(text):
    return _ANSI.sub("", str(text or "")).replace("\r", "")


def excerpt(text, limit=240):
    """The most telling line of an error, shortened and redacted."""
    lines = [line.strip() for line in clean(text).splitlines() if line.strip()]
    pick = next((line for line in reversed(lines)
                 if any(p.search(line) for p in _QUOTA + _RATE + _AUTH + _UNAVAILABLE)), lines[-1] if lines else "")
    pick = re.sub(r"\s+", " ", redaction.redact(pick))
    return pick if len(pick) <= limit else pick[:limit - 1] + "…"


# --- reset times ------------------------------------------------------------------------

_UNITS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
_RELATIVE = re.compile(
    r"\b(?:try again|retry|resets?|available again|wait)\s+(?:in|after)\s+"
    r"((?:\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE)
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATED = re.compile(
    r"\b(?:try again|retry|resets?|available again)(?:\s+(?:at|after|on))?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?"
    r",?(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?)?",
    re.IGNORECASE)
_CLOCK = re.compile(
    r"\b(?:try again|retry|resets?|available again)(?:\s+(?:at|after|on))?\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?(?![a-z])",
    re.IGNORECASE)
_ZONE = re.compile(r"^\s*\(([A-Za-z_]+(?:/[A-Za-z_]+)+|UTC)\)")


def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def reset_time(text, now=None):
    """(epoch seconds, "parsed"|"stale"|"absent") for the reset time in text."""
    now = time.time() if now is None else now
    value = clean(text).replace(" ", " ").replace(" ", " ")
    found = re.search(r"limit reached\|(\d{10})(?!\d)", value, re.IGNORECASE)
    if found:
        return _judge(float(found.group(1)), now)
    found = re.search(r"\bretry[-_]after[\"']?\s*[:=]\s*(\d{1,6})(?![\d:.])", value, re.IGNORECASE)
    if found:
        return _judge(now + int(found.group(1)), now)
    found = _RELATIVE.search(value)
    if found:
        total = sum(float(n) * _UNITS[u.lower()] for n, u in _PART.findall(found.group(1)))
        if total > 0:
            return _judge(now + total, now)
    found = _DATED.search(value)
    if found:
        month, day, year, hour, minute, ampm = found.groups()
        zone = _zone((_ZONE.match(value[found.end():]) or [None, None])[1] or "") or datetime.datetime.now().astimezone().tzinfo
        current = datetime.datetime.fromtimestamp(now, zone)
        h = int(hour or 0) % 12 + (12 if (ampm or "").lower() == "p" else 0) if hour else 0
        try:
            moment = datetime.datetime(int(year or current.year), _MONTHS.index(month.lower()[:3]) + 1, int(day),
                                       h, int(minute or 0), tzinfo=zone)
        except ValueError:
            return None, "absent"
        if not year and moment.timestamp() < now - 86400:
            moment = moment.replace(year=moment.year + 1)  # "Jan 2nd" said in late December
        return _judge(moment.timestamp(), now)
    found = _CLOCK.search(value)
    if found:
        hour, minute, ampm = found.groups()
        zone = _zone((_ZONE.match(value[found.end():]) or [None, None])[1] or "") or datetime.datetime.now().astimezone().tzinfo
        current = datetime.datetime.fromtimestamp(now, zone)
        h = int(hour) % 12 + (12 if ampm.lower() == "p" else 0)
        moment = current.replace(hour=h, minute=int(minute or 0), second=0, microsecond=0)
        if moment.timestamp() <= now:
            moment += datetime.timedelta(days=1)   # "resets 3am" said at 10pm
        return _judge(moment.timestamp(), now)
    return None, "absent"


def _judge(when, now):
    return (when, "parsed") if when > now else (None, "stale")


# --- classification ---------------------------------------------------------------------

def classify(text, status=None, now=None):
    """Failure for a provider's error text (and HTTP status, if known)."""
    value = clean(text)
    status = int(status) if str(status or "").isdigit() else None
    if any(p.search(value) for p in _QUOTA):
        kind = "quota"
    elif status == 429 or any(p.search(value) for p in _RATE):
        kind = "rate"
    elif status in (401, 403) or any(p.search(value) for p in _AUTH):
        kind = "auth"
    elif status in (503, 529) or any(p.search(value) for p in _UNAVAILABLE):
        kind = "unavailable"
    else:
        return Failure("failure", excerpt(value))
    until, hint = reset_time(value, now) if kind in ("quota", "rate") else (None, "absent")
    return Failure(kind, excerpt(value), until, hint)


def install_broken(text):
    return bool(INSTALL_BROKEN.search(clean(text)))


def codex_failure(log_text, now=None):
    """Failure from a Codex --json log: only its final error / turn.failed
    event counts (the agent's own commands may print anything)."""
    message = ""
    for line in str(log_text or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            message = str(event.get("message") or "")
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            message = str(error.get("message") if isinstance(error, dict) else error or "")
        elif event.get("type") == "turn.completed":
            message = ""  # a later success wins
    return classify(message, now=now) if message else None


def hold_seconds(failure, now=None):
    """How long to hold a provider for this failure (0: no hold)."""
    if not failure.holds:
        return 0
    now = time.time() if now is None else now
    if failure.kind == "unavailable" and install_broken(failure.reason):
        return INSTALL_SECONDS
    if failure.until:
        seconds = failure.until - now + MARGIN
    else:
        seconds = DEFAULT_SECONDS[failure.kind]
    return int(max(60, min(seconds, CAP_SECONDS[failure.kind])))


# --- holds in Redis ---------------------------------------------------------------------
#
# Holds are per account (services/provider_accounts.py):
#   laika:provider-cooldown:<provider>:<account>   reason, TTL = the hold
#   laika:provider-hold:<provider>:<account>       JSON {kind, reason, until, at, hint}
# The provider-wide keys (no account) are a summary kept for everything that
# already reads them (dashboard, watchdog): set only while EVERY enabled
# account of the provider is held, for as long as the first of them stays
# held, with that account's details.

def cooldown_key(provider, account=None):
    return f"laika:provider-cooldown:{provider}" + (f":{account}" if account else "")


def hold_key(provider, account=None):
    return f"laika:provider-hold:{provider}" + (f":{account}" if account else "")


def usage_key(provider, account=None):
    """Plan-usage readings; the default account keeps the key it always had."""
    import provider_accounts
    account = account or provider_accounts.DEFAULT
    return f"laika:provider-usage:{provider}" + ("" if account == provider_accounts.DEFAULT else f":{account}")


def _account(account):
    import provider_accounts
    return account or provider_accounts.DEFAULT


def hold(r, provider, failure, now=None, account=None):
    """Hold this account of provider for this failure; returns the hold
    seconds (0 = none). A longer existing hold is never shortened."""
    seconds = hold_seconds(failure, now)
    if not seconds:
        return 0
    account = _account(account)
    now = time.time() if now is None else now
    try:
        left = r.ttl(cooldown_key(provider, account))
        if isinstance(left, int) and left > seconds:
            return left
        reason = f"{provider} {failure.kind}: {failure.reason}"[:300]
        r.set(cooldown_key(provider, account), reason, ex=seconds)
        r.set(hold_key(provider, account), json.dumps({"kind": failure.kind, "reason": failure.reason[:300],
                                                       "until": now + seconds, "at": now, "hint": failure.hint}),
              ex=seconds)
    except Exception:
        pass
    summarize(r, provider, now)
    return seconds


def summarize(r, provider, now=None):
    """Keep the provider-wide summary keys in step with the accounts' holds."""
    import provider_accounts
    now = time.time() if now is None else now
    try:
        waits = []
        for account in provider_accounts.accounts(r, provider):
            if not account["enabled"] or not provider_accounts.has_credentials(provider, account["id"]):
                continue
            left = r.ttl(cooldown_key(provider, account["id"]))
            if not (isinstance(left, int) and left > 0):
                return  # an account can serve: no provider-wide hold (an older one stays until it ends)
            waits.append((left, account["id"]))
        if not waits:
            return
        left, account = min(waits)
        r.set(cooldown_key(provider), r.get(cooldown_key(provider, account)) or f"{provider} on hold", ex=left)
        r.set(hold_key(provider), r.get(hold_key(provider, account)) or "{}", ex=left)
    except Exception:
        pass


def held(r, provider, account=None):
    """account given: that account cannot be used now; None: no account of
    the provider can (the provider-wide summary)."""
    try:
        if r.get(cooldown_key(provider)):
            return True
        if account:
            return bool(r.get(cooldown_key(provider, account)))
        import provider_accounts
        enabled = [a["id"] for a in provider_accounts.accounts(r, provider)
                   if a["enabled"] and provider_accounts.has_credentials(provider, a["id"])]
        return all(r.get(cooldown_key(provider, a)) for a in enabled)  # none usable at all: held too
    except Exception:
        return False


def info(r, provider, now=None, account=None):
    """{held, kind, reason, until, seconds_left, hint} for the dashboard
    (account None: the provider-wide summary)."""
    now = time.time() if now is None else now
    try:
        reason = r.get(cooldown_key(provider, account))
        if not reason:
            return {"held": False}
        left = r.ttl(cooldown_key(provider, account))
        detail = json.loads(r.get(hold_key(provider, account)) or "{}")
    except Exception:
        return {"held": False}
    seconds = left if isinstance(left, int) and left > 0 else 0
    return {"held": True, "kind": detail.get("kind") or "unavailable", "reason": detail.get("reason") or reason,
            "until": detail.get("until") or (now + seconds), "seconds_left": seconds, "hint": detail.get("hint", "absent")}


def release(r, provider, account=None):
    """End an account's hold (None: the default account) and the provider-wide
    summary: at least that account can serve again."""
    try:
        r.delete(cooldown_key(provider, _account(account)), hold_key(provider, _account(account)),
                 cooldown_key(provider), hold_key(provider))
    except Exception:
        pass
