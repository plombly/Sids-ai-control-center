"""Notifications shared by the host notifier (scripts/laika-notify.py,
scripts/laika-digest.py) and the API (settings page, "Send test"). Standard
library only.

Targets (secrets) live in a root-only env file, NOTIFY_DIR/notify.env:
DISCORD_WEBHOOK, DISCORD_MENTION (user id to ping), NTFY_URL, DASHBOARD_URL.
The API sees that directory mounted at /notify; values are write-only there.
Everything else (what each event does, quiet hours, digest schedule) is
plain settings in Redis laika:notify:settings.
"""

import datetime
import json
import os
import tempfile
import urllib.request
from pathlib import Path

NOTIFY_DIR = Path(os.environ.get("LAIKA_NOTIFY_DIR", "/etc/laika/notify"))
LEGACY_FILE = Path("/etc/laika/notify.env")
SETTINGS_KEY = "laika:notify:settings"
TARGET_KEYS = ("DISCORD_WEBHOOK", "DISCORD_MENTION", "NTFY_URL", "DASHBOARD_URL")
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
    "digest": ("Weekly digest", "post", False),
}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SETTINGS = {
    "events": {name: default for name, (_, default, _) in EVENTS.items()},
    "quiet": {"enabled": False, "start": "22:00", "end": "07:00"},
    "digest": {"day": "sun", "time": "18:00"},
}


# --- targets ---------------------------------------------------------------------------

def env_file(base=None):
    return Path(base or NOTIFY_DIR) / "notify.env"


def load_targets(base=None):
    values = {}
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


def save_targets(changes, base=None):
    """Merge changes into the env file (root-only, atomic). An empty string
    removes a value."""
    folder = Path(base or NOTIFY_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    current = {k: v for k, v in load_targets(base).items() if k in TARGET_KEYS}
    for key, value in changes.items():
        if key not in TARGET_KEYS:
            raise ValueError(f"unknown setting {key}")
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    body = "# LAIka notifications. Root-only; edited from the dashboard Settings page.\n"
    body += "".join(f"{key}={current[key]}\n" for key in TARGET_KEYS if key in current)
    handle = tempfile.NamedTemporaryFile("w", dir=folder, prefix=".notify-", delete=False)
    try:
        os.chmod(handle.name, 0o600)
        handle.write(body)
        handle.close()
        os.replace(handle.name, env_file(base))
    except BaseException:
        handle.close()
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise
    return current


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


def load_settings(redis):
    try:
        return clean_settings(json.loads(redis.get(SETTINGS_KEY) or "{}"))
    except (TypeError, ValueError):
        return clean_settings({})


def save_settings(redis, raw):
    settings = clean_settings(raw)
    redis.set(SETTINGS_KEY, json.dumps(settings))
    return settings


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
