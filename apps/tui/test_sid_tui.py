import importlib.util
from contextlib import contextmanager, redirect_stdout
from io import StringIO
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

    def llen(self, key):
        return 0


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


def test_active_goal_is_rendered_as_active_and_goal_less_orchestrator_stays_idle():
    assert sid_tui.normalize_orchestrator(
        "sid:orchestrators:active",
        {"status": "idle", "goal_id": "goal-1"},
    )["status"] == "active"
    assert sid_tui.normalize_orchestrator(
        "sid:orchestrators:idle",
        {"status": "idle"},
    )["status"] == "idle"


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


def test_timestamp_display_formats_local_time_and_rejects_unusable_values(monkeypatch):
    converted = []
    local_values = {
        0.0: (1970, 1, 1, 0, 0, 0, 3, 1, 0),
        123.5: (2024, 2, 3, 4, 5, 6, 0, 34, 0),
    }

    def localtime(value):
        converted.append(value)
        return local_values[value]

    monkeypatch.setattr(sid_tui.time, "localtime", localtime)

    assert sid_tui.format_timestamp("0") == "1970-01-01 00:00:00"
    assert sid_tui.format_timestamp("123.5") == "2024-02-03 04:05:06"
    assert converted == [0.0, 123.5]
    for value in (None, "", "bad", "nan", "inf"):
        assert sid_tui.format_timestamp(value) == "-"


def test_timestamp_display_returns_fallback_for_unrepresentable_epoch(monkeypatch):
    def localtime(_value):
        raise OverflowError

    monkeypatch.setattr(sid_tui.time, "localtime", localtime)

    assert sid_tui.format_timestamp("1") == "-"


def test_job_normalization_derives_effective_tokens_and_duration_fallbacks():
    job = sid_tui.normalize_job(
        "sid:jobs:j1",
        {
            "input_tokens": "100",
            "cached_input_tokens": "25",
            "output_tokens": "30",
            "started_at": "10",
            "completed_at": "17.5",
        },
    )

    assert job["effective_tokens"] == 105
    assert job["cached_input_tokens"] == 25
    assert job["duration"] == 7.5
    assert job["status"] == "unknown"
    assert job["role"] == "-"


def test_worker_normalization_preserves_unknowns_and_actual_assignment():
    worker = sid_tui.normalize_worker(
        "sid:workers:w1",
        {
            "role": "reviewer",
            "current_job_id": "job-7",
            "model": "model-x",
            "last_seen": "bad",
        },
    )

    assert worker["id"] == "w1"
    assert worker["role"] == "reviewer"
    assert worker["job_id"] == "job-7"
    assert worker["model"] == "model-x"
    assert worker["heartbeat_age"] == -1
    assert worker["effective_tokens"] == "-"


def test_snapshot_classifies_failures_and_human_approvals(monkeypatch):
    monkeypatch.setattr(sid_tui, "git_info", lambda: ("main", "clean"))
    monkeypatch.setattr(
        sid_tui,
        "r",
        FakeRedis(
            {
                "sid:jobs:failed": {"status": "failed", "updated_at": "2"},
                "sid:jobs:ready": {
                    "status": "awaiting_review",
                    "review_status": "complete",
                    "review_verdict": "pass",
                    "updated_at": "1",
                },
            }
        ),
    )

    view = sid_tui.snapshot()

    assert [job["id"] for job in view["jobs"]["failures"]] == ["failed"]
    assert [job["id"] for job in view["pending_human_approvals"]] == ["ready"]
    assert view["repository"] == {"branch": "main", "status": "clean"}


def test_goal_progress_format_includes_completion_and_status_breakdown():
    assert sid_tui.format_goal_progress(
        {
            "completed": 1,
            "total": 3,
            "status_counts": {"merged": 1, "queued": 1, "unknown": 1},
        }
    ) == "1/3 (merged:1,queued:1,unknown:1)"


def test_draw_includes_orchestrator_goals_and_existing_review_notices(monkeypatch):
    monkeypatch.setattr(sid_tui, "clear", lambda: None)
    monkeypatch.setattr(sid_tui, "git_info", lambda: ("main", "clean"))
    monkeypatch.setattr(
        sid_tui.time,
        "localtime",
        lambda value: (
            (2024, 2, 3, 4, 5, 6, 0, 34, 0)
            if value == 1706933106.0
            else (2024, 2, 3, 4, 6, 6, 0, 34, 0)
        ),
    )
    monkeypatch.setattr(sid_tui, "r", FakeRedis({}))
    monkeypatch.setattr(
        sid_tui,
        "orchestrators",
        lambda: [
            {
                "id": "orch-1",
                "status": "planning",
                "model": "planner-model",
                "active_goal": "goal-1",
                "heartbeat_age": 4,
            }
        ],
    )
    monkeypatch.setattr(
        sid_tui,
        "workers",
        lambda: [],
    )
    monkeypatch.setattr(
        sid_tui,
        "recent_goals",
        lambda: [
            {
                "id": "goal-1",
                "status": "running",
                "summary": "Ship the TUI",
                "created_at": "1706933106",
                "updated_at": "1706933166",
                "child_job_ids": ["job-1", "job-2", "job-3"],
                "child_job_progress": {
                    "completed": 1,
                    "total": 3,
                    "status_counts": {"merged": 1, "queued": 1, "running": 1},
                },
            }
        ],
    )
    monkeypatch.setattr(
        sid_tui,
        "recent_jobs",
        lambda: [
            {
                "id": "review-needed",
                "status": "awaiting_review",
                "role": "builder",
                "review_verdict": "",
                "review_status": "",
                "review_job_id": "",
                "worker": "worker-1",
                "model": "builder-model",
                "total_tokens": "10",
                "effective_tokens": "10",
                "cached_input_tokens": "0",
                "command_count": "1",
                "duration": "1",
            },
            {
                "id": "human-ready",
                "status": "awaiting_review",
                "role": "builder",
                "review_verdict": "pass",
                "review_status": "complete",
                "review_job_id": "review-1",
                "worker": "worker-1",
                "model": "builder-model",
                "total_tokens": "20",
                "effective_tokens": "20",
                "cached_input_tokens": "0",
                "command_count": "1",
                "duration": "2",
            },
            {
                "id": "needs-changes",
                "status": "awaiting_review",
                "role": "builder",
                "review_verdict": "changes_required",
                "review_status": "complete",
                "review_job_id": "review-2",
                "worker": "worker-1",
                "model": "builder-model",
                "total_tokens": "30",
                "effective_tokens": "30",
                "cached_input_tokens": "0",
                "command_count": "1",
                "duration": "3",
            },
        ],
    )

    output = StringIO()
    with redirect_stdout(output):
        sid_tui.draw()
    rendered = output.getvalue()

    assert "ORCHESTRATOR" in rendered
    assert "orch-1" in rendered
    assert "planning" in rendered
    assert "planner-model" in rendered
    assert "4s ago" in rendered
    assert "RECENT GOALS" in rendered
    assert "goal-1" in rendered
    assert "Ship the TUI" in rendered
    assert "2024-02-03 04:05:06" in rendered
    assert "2024-02-03 04:06:06" in rendered
    assert "1706933106" not in rendered
    assert "1706933166" not in rendered
    assert "1/3 (merged:1,queued:1,running:1)" in rendered
    assert "WORKERS" in rendered
    assert "RECENT JOBS" in rendered
    assert "AGENT REVIEW REQUIRED" in rendered
    assert "READY FOR HUMAN APPROVAL" in rendered
    assert "CHANGES REQUIRED" in rendered
    assert "Ctrl-C to exit" in rendered


def test_main_refreshes_every_two_seconds_and_r_refreshes_immediately(monkeypatch):
    keys = iter(("r", "q"))
    draws = []
    waits = []

    monkeypatch.setattr(sid_tui, "draw", lambda: draws.append(len(draws)))
    monkeypatch.setattr(
        sid_tui,
        "_wait_for_key",
        lambda timeout: (waits.append(timeout) or next(keys)),
    )

    @contextmanager
    def terminal_mode():
        yield

    monkeypatch.setattr(sid_tui, "_terminal_mode", terminal_mode)
    with redirect_stdout(StringIO()):
        sid_tui.main()

    assert draws == [0, 1]
    assert waits == [2, 2]


def test_main_q_terminates_without_an_extra_refresh(monkeypatch):
    draws = []
    monkeypatch.setattr(sid_tui, "draw", lambda: draws.append(True))
    monkeypatch.setattr(sid_tui, "_wait_for_key", lambda timeout: "q")

    @contextmanager
    def terminal_mode():
        yield

    monkeypatch.setattr(sid_tui, "_terminal_mode", terminal_mode)
    with redirect_stdout(StringIO()):
        sid_tui.main()

    assert draws == [True]


def test_terminal_mode_restores_settings_after_an_exception(monkeypatch):
    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return 7

    restored = []
    monkeypatch.setattr(sid_tui.sys, "stdin", FakeStdin())
    monkeypatch.setattr(sid_tui.termios, "tcgetattr", lambda stream: ["saved"])
    monkeypatch.setattr(
        sid_tui.tty,
        "setcbreak",
        lambda fd: restored.append(("cbreak", fd)),
    )
    monkeypatch.setattr(
        sid_tui.termios,
        "tcsetattr",
        lambda stream, action, settings: restored.append((action, settings)),
    )

    try:
        with sid_tui._terminal_mode():
            raise RuntimeError("stop")
    except RuntimeError:
        pass

    assert restored == [("cbreak", 7), (sid_tui.termios.TCSADRAIN, ["saved"])]
