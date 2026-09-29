import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("sid-tui.py")
SPEC = importlib.util.spec_from_file_location("sid_tui", MODULE_PATH)
sid_tui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sid_tui)


class FakeRedis:
    def __init__(self, hashes):
        self.hashes = hashes

    def scan_iter(self, pattern):
        prefix = pattern[:-1]
        return iter(sorted(key for key in self.hashes if key.startswith(prefix)))

    def hgetall(self, key):
        value = self.hashes.get(key, {})
        if isinstance(value, Exception):
            raise value
        return value


def test_orchestrators_are_sorted_and_malformed_records_are_safe(monkeypatch):
    monkeypatch.setattr(
        sid_tui,
        "r",
        FakeRedis(
            {
                "sid:orchestrators:z": {"id": "z", "status": "idle", "last_seen": "bad"},
                "sid:orchestrators:a": {"id": "a", "model": "m", "last_seen": "0"},
                "sid:orchestrators:broken": None,
            }
        ),
    )

    records = sid_tui.orchestrators()

    assert [record["id"] for record in records] == ["a", "broken", "z"]
    assert records[0]["status"] == "unknown"
    assert records[0]["active_goal"] == "-"
    assert records[1]["heartbeat_age"] == -1


def test_goals_use_text_fallback_and_deterministic_recent_order(monkeypatch):
    monkeypatch.setattr(
        sid_tui,
        "r",
        FakeRedis(
            {
                "sid:goals:older": {
                    "goal": "fallback goal",
                    "status": "queued",
                    "created_at": "10",
                    "jobs": "[\"b\", \"a\"]",
                },
                "sid:goals:newer-b": {
                    "text": "text fallback",
                    "updated_at": "20",
                    "jobs": "not json",
                },
                "sid:goals:newer-a": {
                    "summary": "summary wins",
                    "updated_at": "20",
                    "jobs": "[]",
                },
                "sid:jobs:a": {"status": "merged"},
                "sid:jobs:b": {"status": "running"},
            }
        ),
    )

    goals = sid_tui.recent_goals(limit=2)

    assert [goal["id"] for goal in goals] == ["newer-a", "newer-b"]
    assert goals[0]["summary"] == "summary wins"
    assert goals[1]["summary"] == "text fallback"


def test_goal_progress_counts_child_job_statuses_and_missing_jobs():
    goal = sid_tui.normalize_goal(
        "g1",
        {"goal": "ship", "jobs": '["j2", "j1", "missing"]'},
        {"j1": "merged", "j2": "queued", "missing": None},
    )

    assert goal["child_job_ids"] == ["j1", "j2", "missing"]
    assert goal["child_job_progress"] == {
        "total": 3,
        "completed": 1,
        "status_counts": {"merged": 1, "queued": 1, "unknown": 1},
    }


def test_heartbeat_formatting_is_stable(monkeypatch):
    monkeypatch.setattr(sid_tui.time, "time", lambda: 105.9)

    assert sid_tui.heartbeat_age("100") == 5
    assert sid_tui.format_heartbeat_age(5) == "5s ago"
    assert sid_tui.format_heartbeat_age(-1) == "unknown"
    assert sid_tui.format_heartbeat_age("bad") == "unknown"
