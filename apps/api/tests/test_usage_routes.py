"""Usage: tokens and cost per project/day/provider/role."""

import usage_routes

NOW = 1_800_000_000


def test_summary_attributes_reviews_to_their_builders_project():
    jobs = {
        "b1": {"project_id": "shop", "provider": "codex", "role": "builder", "created_at": NOW, "effective_tokens": "1000",
               "input_tokens": "900", "output_tokens": "100", "duration_seconds": "60"},
        "r1": {"builder_job_id": "b1", "provider": "claude", "role": "reviewer", "created_at": NOW,
               "effective_tokens": "500", "cost_usd": "0.25"},
        "s1": {"provider": "claude", "role": "repair", "created_at": NOW - 86400, "cost_usd": "0.10"},
        "old": {"provider": "codex", "created_at": NOW - 90 * 86400, "effective_tokens": "999999"},
        "bad": {"provider": "codex", "created_at": NOW, "effective_tokens": "nan", "cost_usd": "x"},
    }
    goals = {"g1": {"project_id": "shop", "planner_provider": "claude", "planner_cost_usd": "0.5", "created_at": NOW}}
    result = usage_routes.summarize(jobs, goals, since=NOW - 30 * 86400)
    by_project = {row["project"]: row for row in result["by_project"]}
    assert by_project["shop"]["cost_usd"] == 0.75 and by_project["shop"]["effective_tokens"] == 1500
    assert by_project["shop"]["jobs"] == 3 and by_project["laika"]["cost_usd"] == 0.1
    assert result["total"]["effective_tokens"] == 1500  # 90-day-old job left out, nan counted as 0
    roles = {row["role"]: row["jobs"] for row in result["by_role"]}
    assert roles == {"builder": 2, "reviewer": 1, "repair": 1, "planner": 1}
    assert [row["day"] for row in result["by_day"]] == sorted(row["day"] for row in result["by_day"])
    shop_only = usage_routes.summarize(jobs, goals, since=NOW - 30 * 86400, only_project="shop")
    assert shop_only["total"]["cost_usd"] == 0.75 and [row["project"] for row in shop_only["by_project"]] == ["shop"]
