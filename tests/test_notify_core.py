"""apps/api/notify_core.py: targets file, settings, quiet hours."""

import datetime
import stat
import sys

from laika_testing import ROOT, MemoryRedis

sys.path.insert(0, str(ROOT / "apps/api"))
import notify_core  # noqa: E402


def test_targets_file_is_root_only_and_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_core, "LEGACY_FILE", tmp_path / "none.env")
    notify_core.save_targets({"DISCORD_WEBHOOK": "https://discord.example/hook/abcd", "DISCORD_MENTION": "123"}, tmp_path)
    notify_core.save_targets({"DISCORD_MENTION": ""}, tmp_path)
    targets = notify_core.load_targets(tmp_path)
    assert targets == {"DISCORD_WEBHOOK": "https://discord.example/hook/abcd"}
    assert stat.S_IMODE((tmp_path / "notify.env").stat().st_mode) == 0o600
    assert notify_core.masked(targets) == {"discord": True, "discord_hint": "…abcd", "mention": "", "ntfy": False,
                                           "ntfy_hint": "", "dashboard_url": ""}
    try:
        notify_core.save_targets({"EVIL": "x"}, tmp_path)
        raise AssertionError("unknown key accepted")
    except ValueError:
        pass


def test_settings_are_cleaned_and_quiet_hours_wrap_midnight():
    r = MemoryRedis()
    saved = notify_core.save_settings(r, {"events": {"approval": "ping", "bogus": "ping", "goal_done": "loud"},
                                          "quiet": {"enabled": True, "start": "22:00", "end": "7:00"},
                                          "digest": {"day": "funday", "time": "25:00"}})
    assert saved["events"]["approval"] == "ping" and saved["events"]["goal_done"] == "post" and "bogus" not in saved["events"]
    assert saved["quiet"] == {"enabled": True, "start": "22:00", "end": "07:00"}
    assert saved["digest"] == {"day": "sun", "time": "18:00"}
    assert notify_core.load_settings(r) == saved
    at = lambda h, m: datetime.datetime(2026, 10, 1, h, m)
    assert notify_core.in_quiet_hours(saved, at(23, 0)) and notify_core.in_quiet_hours(saved, at(6, 59))
    assert not notify_core.in_quiet_hours(saved, at(7, 0)) and not notify_core.in_quiet_hours(saved, at(12, 0))
    assert notify_core.mode_for(saved, "approval", at(23, 0)) == "hold"
    assert notify_core.mode_for(saved, "health_red", at(23, 0)) == "ping"  # urgent


def test_personal_targets_are_separate_files_without_the_dashboard_address(tmp_path):
    notify_core.save_targets({"NTFY_URL": "https://ntfy.sh/sam-topic"}, tmp_path, person="sam")
    assert notify_core.load_targets(tmp_path, person="sam") == {"NTFY_URL": "https://ntfy.sh/sam-topic"}
    assert stat.S_IMODE((tmp_path / "people" / "sam.env").stat().st_mode) == 0o600
    for bad in ({"DASHBOARD_URL": "http://x"}, {"NTFY_URL": "https://ntfy.sh/a\nDISCORD_WEBHOOK=evil"}):
        try:
            notify_core.save_targets(bad, tmp_path, person="sam")
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    for name in ("..", "../x", "a/b", ""):
        try:
            notify_core.env_file(tmp_path, name)
            raise AssertionError(f"accepted {name!r}")
        except ValueError:
            pass
    notify_core.remove_person("sam", tmp_path)
    assert notify_core.load_targets(tmp_path, person="sam") == {}


def test_who_may_receive_what():
    levels = {"view": 1, "build": 2, "approve": 3}
    may = lambda kind, have: notify_core.may_receive(kind, "shop", False, {"shop": have}, levels)
    assert may("approval", 3) and not may("approval", 2)
    assert may("needs_human", 2) and not may("needs_human", 1)
    assert may("goal_done", 1) and not may("health_red", 3) and not may("backup_failed", 3)
    assert notify_core.may_receive("health_red", "", True, None, levels)
    assert "health_red" not in notify_core.personal_events(False) and "health_red" in notify_core.personal_events(True)
    assert notify_core.clean_personal({"goals": "everything"})["goals"] == "mine"
