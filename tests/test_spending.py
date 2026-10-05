"""apps/api/spending.py: what counts this month, limits and the refusal message."""

import datetime
import sys

from laika_testing import ROOT, MemoryRedis

sys.path.insert(0, str(ROOT / "apps/api"))
import spending  # noqa: E402


class Ledger(MemoryRedis):
    def incrbyfloat(self, key, amount):
        self.values[key] = str(float(self.values.get(key) or 0) + amount)

    def expire(self, key, seconds):
        return True


def test_jobs_planning_and_reviews_count_for_the_project_and_whoever_gave_the_goal():
    now = datetime.datetime(2026, 10, 20, 12, tzinfo=datetime.timezone.utc).timestamp()
    start = spending.month_start(now)
    last_month = start - 3600
    jobs = {"b1": {"project_id": "shop", "goal_id": "g1", "cost_usd": "1.50", "created_at": str(start + 10)},
            "r1": {"builder_job_id": "b1", "role": "reviewer", "cost_usd": "0.25", "created_at": str(start + 20)},
            "old": {"project_id": "shop", "goal_id": "g1", "cost_usd": "9", "created_at": str(last_month)}}
    goals = {"g1": {"project_id": "shop", "submitted_by": "Sam", "planner_cost_usd": "0.75", "created_at": str(start + 5)}}
    by_project, by_person = spending.totals(jobs, goals, start)
    assert by_project == {"shop": 2.5} and by_person == {"sam": 2.5}


def test_states_and_blocking():
    assert spending.state(1, 0)["state"] == "none"
    assert spending.state(7.9, 10)["state"] == "ok" and spending.state(8, 10)["state"] == "warn"
    over = spending.state(10, 10)
    assert over["state"] == "over" and over["percent"] == 100
    assert "This project's monthly spending limit is used up ($10.00 of $10.00" in spending.blocked(over, None)
    assert spending.blocked(spending.state(12, 10, "warn"), None) == ""
    assert spending.blocked(None, spending.state(5, 5)).startswith("Your monthly spending limit")


def test_assistant_turns_go_to_the_monthly_ledger():
    r = Ledger()
    spending.record_assistant(r, "shop", "Sam", 0.4, now=datetime.datetime(2026, 10, 2).timestamp())
    spending.record_assistant(r, "shop", "", 0.1, now=datetime.datetime(2026, 10, 3).timestamp())
    assert float(r.values["laika:spend:2026-10:project:shop"]) == 0.5
    assert float(r.values["laika:spend:2026-10:person:sam"]) == 0.4
    record = {"budget_usd": "0.5"}
    assert spending.project_state(r, "shop", record, {}, now=datetime.datetime(2026, 10, 5).timestamp())["state"] == "over"


def test_the_month_is_the_same_everywhere_utc():
    late = datetime.datetime(2026, 11, 1, 1, tzinfo=datetime.timezone.utc).timestamp()   # 8 pm Oct 31 in Texas
    assert spending.month(late) == "2026-11"
    assert spending.month_start(late) == datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc).timestamp()
