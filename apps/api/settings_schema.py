"""LAIka settings: one validated list of everything an operator may change.

Shared by the API (Settings pages, GET/PUT /api/settings) and every host
program (services/laika_env.py applies stored values to the environment at
start). Standard library only.

Stored overrides live in Redis laika:settings (JSON {key: value}); anything
not stored uses the default below. Keys of service settings are the
environment variable names the programs already read, so a stored value
simply takes the place of the variable.

apply:
  "live"     takes effect at once (read on every use, or by the browser)
  "restart"  services pick it up when they restart ("Apply now" restarts
             them safely, waiting for running jobs)
  "host"     also changes something on the host (the backup timer);
             applied by the operator service
"""

import json
import re

KEY = "laika:settings"

SECTIONS = [
    ("general", "General", "What this server is called and how the dashboard behaves."),
    ("appearance", "Appearance", "How LAIka looks for everyone using this server."),
    ("ai", "AI & pipeline", "Which AI does what, how much it may spend and how hard LAIka tries."),
    ("workers", "Workers", "How many jobs run at the same time."),
    ("projects", "Project defaults", "Starting values for new projects, apps, previews and builds."),
    ("backups", "Backups & upkeep", "Backups, restore checks and cleaning up old data."),
    ("health", "Health checks", "When the watchdog warns you."),
]

ROLES = ("planner", "builder", "reviewer", "repair")


def _f(key, section, label, kind, default, help="", apply="restart", **extra):
    return {"key": key, "section": section, "label": label, "type": kind, "default": str(default),
            "help": help, "apply": apply, **extra}


FIELDS = [
    # --- general -------------------------------------------------------------------------
    _f("SERVER_NAME", "general", "Server name", "str", "LAIka", "Shown in the dashboard, notifications and the phone app.",
       apply="live", max_length=60),
    _f("DASHBOARD_REFRESH_SECONDS", "general", "Dashboard refresh (seconds)", "int", 5,
       "How often open pages fetch fresh data.", apply="live", min=2, max=60),
    _f("CLOCK", "general", "Clock", "choice", "24h", apply="live", choices=["24h", "12h"]),
    _f("DATE_FORMAT", "general", "Dates", "choice", "short", apply="live",
       choices=["short", "iso"], choice_labels={"short": "Oct 1, 14:03", "iso": "2026-10-01 14:03"}),
    # --- appearance ----------------------------------------------------------------------
    _f("THEME", "appearance", "Theme", "choice", "system", "System follows the device's light or dark mode.",
       apply="live", choices=["system", "dark", "light"]),
    _f("ACCENT", "appearance", "Accent colour", "color", "#5b9dff", apply="live",
       presets=["#5b9dff", "#3fcf8e", "#b48cff", "#ff9f5a", "#ef5d6c", "#3cc8c8", "#efb44a"]),
    _f("DENSITY", "appearance", "Layout", "choice", "comfortable", apply="live", choices=["comfortable", "compact"]),
    _f("FONT_SCALE", "appearance", "Text size (%)", "int", 100, apply="live", min=85, max=130),
    _f("REDUCE_MOTION", "appearance", "Reduce motion", "bool", "false", "No animations or spinners.", apply="live"),
    _f("HOME_SECTIONS", "appearance", "Home page sections", "list", "needs,progress,projects,recent",
       "Which sections the home page shows, in order.", apply="live",
       choices=["needs", "progress", "projects", "recent"],
       choice_labels={"needs": "Needs you", "progress": "In progress", "projects": "Projects", "recent": "Recently finished"}),
    # --- AI & pipeline -------------------------------------------------------------------
    *[_f(f"PROVIDER_{role.upper()}", "ai", f"{role.capitalize()} runs on", "choice",
         "codex" if role == "builder" else "claude", apply="restart", choices=["claude", "codex"],
         help={"planner": "Splits goals into jobs.", "builder": "Writes the code.",
               "reviewer": "Reviews every change independently.", "repair": "Fixes what review found."}[role])
      for role in ROLES],
    _f("DEFAULT_MODEL", "ai", "Codex model", "str", "gpt-5.6-luna", "Model used whenever a role runs on Codex.",
       max_length=60, pattern=r"^[A-Za-z0-9._:-]+$"),
    _f("CLAUDE_PLANNER_MODEL", "ai", "Claude planner model", "str", "opus", max_length=60, pattern=r"^[A-Za-z0-9._:-]+$"),
    _f("CLAUDE_PLANNER_ATOMIC_MODEL", "ai", "Claude model for one-step goals", "str", "sonnet", max_length=60,
       pattern=r"^[A-Za-z0-9._:-]+$"),
    _f("CLAUDE_BUILDER_MODEL", "ai", "Claude builder model", "str", "sonnet", max_length=60, pattern=r"^[A-Za-z0-9._:-]+$"),
    _f("CLAUDE_REVIEWER_MODEL", "ai", "Claude reviewer model", "str", "sonnet", max_length=60, pattern=r"^[A-Za-z0-9._:-]+$"),
    _f("CLAUDE_REPAIR_MODEL", "ai", "Claude repair model", "str", "sonnet", max_length=60, pattern=r"^[A-Za-z0-9._:-]+$"),
    *[_f(f"CLAUDE_{role.upper()}_BUDGET_USD", "ai", f"Claude {role} spending cap per run ($)", "float", budget,
         min=0.05, max=50) for role, budget in (("planner", 1.0), ("builder", 3.0), ("reviewer", 1.5), ("repair", 2.0))],
    _f("CLAUDE_MAX_CONCURRENT", "ai", "Claude runs at the same time", "int", 2,
       "Claude subscriptions have usage limits; fewer parallel runs last longer.", min=1, max=16),
    _f("CLAUDE_COOLDOWN_SECONDS", "ai", "Pause after a Claude usage limit (seconds)", "int", 1800,
       "Work falls back to Codex meanwhile.", min=60, max=86400),
    _f("REVIEW_ASPECTS", "ai", "Review aspects", "list", "spec,safety",
       "Independent reviews every change must pass.", choices=["spec", "safety"],
       choice_labels={"spec": "Does exactly what was asked", "safety": "Security and safety"}),
    _f("MAX_REPAIR_ATTEMPTS", "ai", "Repairs before asking you", "int", 2, min=0, max=10),
    _f("MAX_BUILD_ATTEMPTS", "ai", "Rebuilds before asking you", "int", 2, min=1, max=10),
    _f("MAX_REVIEW_RECOVERIES", "ai", "Review retries before asking you", "int", 2, min=0, max=10),
    _f("BEST_OF_FROM_ATTEMPT", "ai", "Build twice from attempt", "int", 2,
       "A retried build is made twice in parallel and the smaller passing change wins. 0 = never.", min=0, max=10),
    _f("BUILDER_TIMEOUT_SECONDS", "ai", "Builder time limit (seconds)", "int", 900, min=120, max=14400),
    _f("REPAIR_TIMEOUT_SECONDS", "ai", "Repair time limit (seconds)", "int", 600, min=120, max=14400),
    _f("REVIEWER_TIMEOUT_SECONDS", "ai", "Review time limit (seconds)", "int", 240, min=60, max=7200),
    _f("PLAN_TIMEOUT", "ai", "Planning time limit (seconds)", "int", 180, min=60, max=3600),
    _f("MAX_CONCURRENT_GOALS", "ai", "Goals planned at the same time", "int", 4, min=1, max=20),
    _f("ASSIST_MODEL", "ai", "Goal assistant model", "str", "claude-haiku-4-5-20251001",
       "The quick model behind 'Plan it with me'.", max_length=80, pattern=r"^[A-Za-z0-9._:-]+$"),
    _f("ASSIST_BUDGET_USD", "ai", "Goal assistant cap per turn ($)", "float", 0.30, min=0.05, max=5),
    # --- workers -------------------------------------------------------------------------
    # The scaler (services/scaler/laika_scaler.py) reads these every 15 s.
    _f("AUTOSCALE", "workers", "Automatic scaling", "bool", "true",
       "Add workers when jobs could run in parallel and are waiting, remove them when idle. "
       "Off: always run the fixed number below.", apply="live"),
    _f("WORKER_COUNT", "workers", "Fixed number of workers", "int", 8,
       "Used when automatic scaling is off. Each worker runs one job at a time.", apply="live", min=1, max=32),
    _f("MIN_WORKERS", "workers", "Fewest workers", "int", 1, "Automatic scaling never goes below this.",
       apply="live", min=1, max=32),
    _f("MAX_WORKERS", "workers", "Most workers", "int", 0,
       "0 = suggested from this server: 2 per CPU, about 0.9 GB of memory each, at most 16.",
       apply="live", min=0, max=32),
    _f("SCALE_UP_WAIT_MINUTES", "workers", "Add a worker after jobs wait (minutes)", "float", 3,
       "Only jobs that could start right now count: not ones waiting for other jobs or for the same files.",
       apply="live", min=0.5, max=120),
    _f("SCALE_DOWN_IDLE_MINUTES", "workers", "Remove a worker after idling (minutes)", "float", 15,
       apply="live", min=1, max=1440),
    _f("PRESSURE_MEMORY_PERCENT", "workers", "Drain a worker below free memory (%)", "int", 15,
       "Works in both modes: the newest worker finishes its job and stops; never mid-job.",
       apply="live", min=3, max=60),
    _f("CRITICAL_MEMORY_PERCENT", "workers", "Pause new work below free memory (%)", "int", 7,
       "No worker starts a new job until memory is back above the drain level.", apply="live", min=1, max=50),
    _f("PRESSURE_LOAD", "workers", "Drain a worker above load per CPU", "float", 1.5,
       "The 5-minute load average divided by the CPU count, held for 2 minutes.", apply="live", min=0.5, max=10),
    _f("SUPPORT_WORKERS", "workers", "Support workers", "int", 2,
       "Up to this many of the last running workers (one per four) prefer medium and low importance work, "
       "so smaller projects keep moving.", apply="restart", min=0, max=31),
    # --- project defaults ----------------------------------------------------------------
    _f("DEFAULT_IMPORTANCE", "projects", "Importance of new projects", "choice", "medium", apply="live",
       choices=["high", "medium", "low"]),
    _f("DEFAULT_APP_MEMORY_MB", "projects", "App memory limit (MB)", "int", 1024, min=64, max=65536),
    _f("DEFAULT_APP_CPUS", "projects", "App CPU limit (cores)", "float", 1.0, min=0.1, max=64),
    _f("DEFAULT_APP_TASKS", "projects", "App process limit", "int", 512, min=16, max=32768),
    _f("APPS_PORT_MIN", "projects", "First app port", "int", 8100, min=1024, max=65000),
    _f("APPS_PORT_MAX", "projects", "Last app port", "int", 8199, min=1024, max=65535),
    _f("PREVIEW_HOURS", "projects", "Previews stop after (hours)", "int", 4, min=1, max=72),
    _f("BUILD_KEEP", "projects", "Builds kept per project", "int", 5, min=1, max=50),
    _f("BUILD_MEMORY", "projects", "Build memory limit", "choice", "4g", choices=["2g", "4g", "6g", "8g", "12g", "16g"]),
    _f("BUILD_CPUS", "projects", "Build CPU limit (cores)", "int", 2, min=1, max=64),
    _f("BUILD_MAX_SECONDS", "projects", "Build time limit (seconds)", "int", 2700, min=300, max=21600),
    _f("LAIKA_TRASH_HOURS", "projects", "Deleted projects can be restored for (hours)", "int", 24, min=1, max=720),
    # --- backups & upkeep ----------------------------------------------------------------
    _f("BACKUP_TIME", "backups", "Daily backup at", "time", "03:30", apply="host"),
    _f("BACKUP_KEEP", "backups", "Backups kept", "int", 14, min=1, max=365),
    _f("BACKUP_REMOTE", "backups", "Off-site copy (rsync target)", "str", "",
       "e.g. backup@nas:/backups/laika. Empty = keep backups on this server only.", max_length=200,
       pattern=r"^$|^[A-Za-z0-9._@:/~-]+$"),
    _f("PRUNE_DAYS", "backups", "Delete finished job records after (days)", "int", 30, min=7, max=3650),
    # --- health --------------------------------------------------------------------------
    _f("WATCHDOG_DISK_MIN_FREE_PERCENT", "health", "Warn when free disk is below (%)", "int", 10, min=1, max=50),
    _f("WATCHDOG_BACKUP_MAX_AGE_HOURS", "health", "Warn when the last backup is older than (hours)", "int", 36,
       min=2, max=720),
]
BY_KEY = {field["key"]: field for field in FIELDS}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean(field, raw):
    """The stored string for a submitted value, or ValueError with a message."""
    kind = field["type"]
    value = raw if not isinstance(raw, str) else raw.strip()
    if kind == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        if str(value).lower() in ("true", "false"):
            return str(value).lower()
        raise ValueError("must be on or off")
    if kind in ("int", "float"):
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        if "min" in field and number < field["min"] or "max" in field and number > field["max"]:
            raise ValueError(f"must be between {field['min']} and {field['max']}")
        return str(number)
    value = "" if value is None else str(value)
    if kind == "choice":
        if value not in field["choices"]:
            raise ValueError(f"must be one of {', '.join(field['choices'])}")
        return value
    if kind == "list":
        items = [item.strip() for item in (value if isinstance(raw, list) else value.split(",")) if str(item).strip()]
        if not items or any(item not in field["choices"] for item in items) or len(set(items)) != len(items):
            raise ValueError(f"choose one or more of {', '.join(field['choices'])}")
        return ",".join(items)
    if kind == "time":
        if not _TIME.match(value):
            raise ValueError("must be a time like 03:30")
        return value
    if kind == "color":
        if not _COLOR.match(value):
            raise ValueError("must be a colour like #5b9dff")
        return value.lower()
    if len(value) > field.get("max_length", 200) or any(ord(c) < 32 for c in value):
        raise ValueError("is too long or has invalid characters")
    if field.get("pattern") and not re.match(field["pattern"], value):
        raise ValueError("has invalid characters")
    if not value and field["key"] not in ("BACKUP_REMOTE",):
        raise ValueError("cannot be empty")
    return value


def stored(redis):
    try:
        data = json.loads(redis.get(KEY) or "{}")
    except (TypeError, ValueError):
        return {}
    return {key: str(value) for key, value in data.items() if key in BY_KEY} if isinstance(data, dict) else {}


def values(redis):
    """Every setting: stored value or default."""
    current = stored(redis)
    return {field["key"]: current.get(field["key"], field["default"]) for field in FIELDS}


def suggested_workers(cpus, memory_gb):
    """Most workers for a server: agents mostly wait on the AI providers, so
    2 per CPU; tests and builds need memory, so about 0.9 GB each; 2..16."""
    try:
        by_cpu = int(cpus) * 2
        by_memory = int(float(memory_gb) / 0.9)
    except (TypeError, ValueError):
        return 4
    return max(2, min(by_cpu, by_memory, 16))


def validate(changes):
    """(clean changes, errors {key: message}); unknown keys are errors."""
    cleaned, errors = {}, {}
    for key, raw in (changes or {}).items():
        field = BY_KEY.get(key)
        if field is None:
            errors[key] = "unknown setting"
            continue
        try:
            cleaned[key] = clean(field, raw)
        except ValueError as exc:
            errors[key] = f"{field['label']} {exc}"
    merged = {**{f["key"]: f["default"] for f in FIELDS}, **cleaned}
    if int(merged["SUPPORT_WORKERS"]) >= int(merged["WORKER_COUNT"]):
        errors.setdefault("SUPPORT_WORKERS", "Support workers must be fewer than workers")
    if int(merged["MAX_WORKERS"]) and int(merged["MIN_WORKERS"]) > int(merged["MAX_WORKERS"]):
        errors.setdefault("MIN_WORKERS", "Fewest workers must not be above most workers")
    if int(merged["CRITICAL_MEMORY_PERCENT"]) >= int(merged["PRESSURE_MEMORY_PERCENT"]):
        errors.setdefault("CRITICAL_MEMORY_PERCENT", "The pause level must be below the drain level")
    if int(merged["APPS_PORT_MIN"]) > int(merged["APPS_PORT_MAX"]):
        errors.setdefault("APPS_PORT_MAX", "Last app port must not be below the first")
    return cleaned, errors


def save(redis, cleaned):
    """Store changes (a value equal to the default is removed). Returns the
    keys whose apply mode needs a restart or a host change."""
    current = stored(redis)
    for key, value in cleaned.items():
        if value == BY_KEY[key]["default"]:
            current.pop(key, None)
        else:
            current[key] = value
    redis.set(KEY, json.dumps(current, sort_keys=True))
    return sorted(key for key in cleaned if BY_KEY[key]["apply"] != "live")


def environment(redis):
    """Stored values of service settings as environment variables
    (services/laika_env.py). Role providers become ROLE_PROVIDERS."""
    current = stored(redis)
    env = {key: value for key, value in current.items() if BY_KEY[key]["apply"] != "live"}
    roles = [f"{role}={current[f'PROVIDER_{role.upper()}']}" for role in ROLES if f"PROVIDER_{role.upper()}" in current]
    for role in ROLES:
        env.pop(f"PROVIDER_{role.upper()}", None)
    if roles:
        env["ROLE_PROVIDERS"] = ",".join(roles)
    return env


def public():
    """The schema for the Settings pages (no values)."""
    return {"sections": [{"id": s, "label": label, "help": text} for s, label, text in SECTIONS], "fields": FIELDS}
