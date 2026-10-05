"""Notifications shared by the host notifier (scripts/laika-notify.py,
scripts/laika-digest.py) and the API (settings page, "Send test"). Standard
library only.

Targets (secrets) live in a root-only env file, NOTIFY_DIR/notify.env:
DISCORD_WEBHOOK, DISCORD_MENTION (user id to ping), NTFY_URL, DASHBOARD_URL.
The API sees that directory mounted at /notify; values are write-only there.
Everything else (what each event does, quiet hours, digest schedule) is
plain settings in Redis laika:notify:settings.

That is the server's channel (the administrators'). Since 1.2 every person
may also have their own: targets in NOTIFY_DIR/people/<name>.env (same
rules: root-only, write-only from the page; no dashboard address) and
settings in laika:notify:settings:<name>. A person only gets events for
projects they may see, at the level the event needs (EVENT_LEVEL), and goal
events only for goals they gave unless they choose "all".
"""

import datetime
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

NOTIFY_DIR = Path(os.environ.get("LAIKA_NOTIFY_DIR", "/etc/laika/notify"))
LEGACY_FILE = Path("/etc/laika/notify.env")
SETTINGS_KEY = "laika:notify:settings"
TARGET_KEYS = ("DISCORD_WEBHOOK", "DISCORD_MENTION", "NTFY_URL", "DASHBOARD_URL")
PERSONAL_KEYS = ("DISCORD_WEBHOOK", "DISCORD_MENTION", "NTFY_URL")
PERSON = re.compile(r"^[a-z0-9._-]{2,40}$")
MODES = ("ping", "post", "off")

# type: (label, default mode, urgent: goes out even in quiet hours)
EVENTS = {
    "approval": ("A change is ready for approval", "post", False),
    "needs_human": ("A job is stuck and needs you", "ping", False),
    "goal_done": ("A goal finished", "post", False),
    "goal_failed": ("A goal failed", "ping", False),
    "app_problem": ("An app crashed or could not install", "ping", False),
    "backup_failed": ("A backup or restore check failed", "ping", True),
    "health_red": ("System health turned red", "ping", True),
    "sftp_conflict": ("SFTP changes could not be applied", "ping", False),
    "budget": ("A monthly spending limit is nearly or fully used", "ping", False),
    "digest": ("Weekly digest", "post", False),
}
# What a person needs to receive an event (access.LEVELS names, or admin).
EVENT_LEVEL = {"approval": "approve", "needs_human": "build", "app_problem": "build", "goal_done": "view",
               "goal_failed": "view", "backup_failed": "admin", "health_red": "admin", "digest": "view",
               "sftp_conflict": "build", "budget": "approve"}
GOAL_SCOPES = ("mine", "all")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SETTINGS = {
    "events": {name: default for name, (_, default, _) in EVENTS.items()},
    "quiet": {"enabled": False, "start": "22:00", "end": "07:00"},
    "digest": {"day": "sun", "time": "18:00"},
}


# --- targets ---------------------------------------------------------------------------

def env_file(base=None, person=None):
    if person is None:
        return Path(base or NOTIFY_DIR) / "notify.env"
    if not PERSON.fullmatch(person or "") or set(person) == {"."}:
        raise ValueError("not a user name")
    return Path(base or NOTIFY_DIR) / "people" / f"{person}.env"


def load_targets(base=None, person=None):
    values = {}
    if person is not None:
        try:
            lines = env_file(base, person).read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            key, sep, value = line.strip().partition("=")
            if sep and key in PERSONAL_KEYS:
                values[key] = value.strip()
        return values
    for path in (env_file(base), LEGACY_FILE):
        try:
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    values.setdefault(key.strip(), value.strip().strip("'\""))
        except OSError:
            continue
        break
    for key in TARGET_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def save_targets(changes, base=None, person=None):
    """Merge changes into the env file (root-only, atomic). An empty string
    removes a value. person: that person's own file."""
    allowed = TARGET_KEYS if person is None else PERSONAL_KEYS
    target = env_file(base, person)
    folder = target.parent
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    current = {k: v for k, v in load_targets(base, person).items() if k in allowed}
    for key, value in changes.items():
        if key not in allowed or "\n" in str(value):
            raise ValueError(f"unknown setting {key}")
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    body = "# LAIka notifications. Root-only; edited from the dashboard Settings page.\n"
    body += "".join(f"{key}={current[key]}\n" for key in allowed if key in current)
    handle = tempfile.NamedTemporaryFile("w", dir=folder, prefix=".notify-", delete=False)
    try:
        os.chmod(handle.name, 0o600)
        handle.write(body)
        handle.close()
        os.replace(handle.name, target)
    except BaseException:
        handle.close()
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise
    return current


def remove_person(name, base=None):
    try:
        env_file(base, name).unlink()
    except (OSError, ValueError):
        pass


def has_target(targets):
    return bool(targets.get("DISCORD_WEBHOOK") or targets.get("NTFY_URL"))


def masked(targets):
    """What the dashboard may show about the targets."""
    hook = targets.get("DISCORD_WEBHOOK", "")
    ntfy = targets.get("NTFY_URL", "")
    return {"discord": bool(hook), "discord_hint": f"…{hook[-4:]}" if hook else "",
            "mention": targets.get("DISCORD_MENTION", ""), "ntfy": bool(ntfy),
            "ntfy_hint": f"…{ntfy[-4:]}" if ntfy else "", "dashboard_url": targets.get("DASHBOARD_URL", "")}


# --- settings --------------------------------------------------------------------------

def _hhmm(value, default):
    try:
        hours, minutes = str(value).split(":")
        if 0 <= int(hours) < 24 and 0 <= int(minutes) < 60:
            return f"{int(hours):02d}:{int(minutes):02d}"
    except ValueError:
        pass
    return default


def clean_settings(raw):
    """Defaults filled in, unknown or invalid values dropped."""
    raw = raw if isinstance(raw, dict) else {}
    events = dict(DEFAULT_SETTINGS["events"])
    for name, mode in (raw.get("events") or {}).items():
        if name in EVENTS and mode in MODES:
            events[name] = mode
    quiet_raw = raw.get("quiet") or {}
    quiet = {"enabled": bool(quiet_raw.get("enabled", False)),
             "start": _hhmm(quiet_raw.get("start"), "22:00"), "end": _hhmm(quiet_raw.get("end"), "07:00")}
    digest_raw = raw.get("digest") or {}
    digest = {"day": digest_raw.get("day") if digest_raw.get("day") in DAYS else "sun",
              "time": _hhmm(digest_raw.get("time"), "18:00")}
    return {"events": events, "quiet": quiet, "digest": digest}


def clean_personal(raw):
    """A person's settings: the same shape, plus which goals they hear about."""
    raw = raw if isinstance(raw, dict) else {}
    settings = clean_settings(raw)
    settings["goals"] = raw.get("goals") if raw.get("goals") in GOAL_SCOPES else "mine"
    return settings


def settings_key(person=None):
    return SETTINGS_KEY if person is None else f"{SETTINGS_KEY}:{person}"


def load_settings(redis, person=None):
    clean = clean_settings if person is None else clean_personal
    try:
        return clean(json.loads(redis.get(settings_key(person)) or "{}"))
    except (TypeError, ValueError):
        return clean({})


def save_settings(redis, raw, person=None):
    settings = (clean_settings if person is None else clean_personal)(raw)
    redis.set(settings_key(person), json.dumps(settings))
    return settings


def may_receive(kind, project, is_admin, levels, level_names):
    """May someone with these project levels ({project: number}; admins:
    everything) receive this event? level_names: access.LEVELS."""
    need = EVENT_LEVEL.get(kind, "admin")
    if is_admin:
        return True
    if need == "admin" or not project:
        return False
    return (levels or {}).get(project, 0) >= level_names[need]


def personal_events(is_admin):
    """The event types a person can choose for themselves."""
    return [name for name in EVENTS if is_admin or EVENT_LEVEL.get(name) != "admin"]


def in_quiet_hours(settings, now=None):
    quiet = settings["quiet"]
    if not quiet["enabled"]:
        return False
    now = now or datetime.datetime.now()
    current = now.strftime("%H:%M")
    start, end = quiet["start"], quiet["end"]
    return start <= current < end if start <= end else current >= start or current < end


def mode_for(settings, event_type, now=None):
    """'ping', 'post', 'off' or 'hold' (quiet hours: send later)."""
    mode = settings["events"].get(event_type, "post")
    if mode != "off" and in_quiet_hours(settings, now) and not EVENTS.get(event_type, ("", "", False))[2]:
        return "hold"
    return mode


# --- sending ---------------------------------------------------------------------------

def send(targets, title, message, link, mode="post", opener=urllib.request.urlopen, log=print):
    """Deliver to every configured target; returns how many accepted it.
    mode 'ping' mentions DISCORD_MENTION (only that user can ever be pinged)."""
    delivered = 0
    if targets.get("NTFY_URL"):
        server, _, topic = targets["NTFY_URL"].rstrip("/").rpartition("/")
        body = json.dumps({"topic": topic, "title": title, "message": message, "click": link,
                           "priority": 4 if mode == "ping" else 3, "tags": ["robot"]}).encode()
        request = urllib.request.Request(server + "/", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener(request, timeout=10):
                delivered += 1
        except Exception as exc:
            log(f"[notify] ntfy failed: {exc}")
    if targets.get("DISCORD_WEBHOOK"):
        mention = targets.get("DISCORD_MENTION", "")
        ping = mode == "ping" and mention.isdigit()
        content = (f"<@{mention}> " if ping else "") + f"**{title}**\n{message}" + (f"\n{link}" if link else "")
        body = json.dumps({"content": content[:1990],
                           "allowed_mentions": {"parse": [], "users": [mention] if ping else []}}).encode()
        request = urllib.request.Request(targets["DISCORD_WEBHOOK"], data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "laika-notify"})
        try:
            with opener(request, timeout=10):
                delivered += 1
        except Exception as exc:
            log(f"[notify] discord failed: {exc}")
    return delivered
