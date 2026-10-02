"""scripts/laika-digest.py: posts once in the scheduled hour of the scheduled day."""

import datetime

from laika_testing import ROOT, load_module


def test_due_once_per_week_in_the_scheduled_hour():
    module = load_module(ROOT / "scripts/laika-digest.py")
    settings = module.notify_core.clean_settings({"digest": {"day": "sun", "time": "18:00"}})
    sunday_six = datetime.datetime(2026, 10, 4, 18, 20)  # a Sunday
    due, week = module.due(settings, sunday_six, None)
    assert due and week == "2026-W40"
    assert not module.due(settings, sunday_six, week)[0]  # already sent this week
    assert not module.due(settings, datetime.datetime(2026, 10, 4, 17, 59), None)[0]
    assert not module.due(settings, datetime.datetime(2026, 10, 5, 18, 0), None)[0]  # Monday


def test_a_group_is_one_line_with_its_members_indented():
    import sys
    sys.path.insert(0, str(ROOT / "apps/api"))
    import digest
    now = 1_790_000_000
    projects = [("shop", "Shop"), ("shop-api", "Shop API"), ("blog", "Blog"), ("shop-app", "Shop app")]
    goals = {"g1": {"project_id": "shop", "status": "completed", "updated_at": now - 100},
             "g2": {"project_id": "shop-api", "status": "completed", "updated_at": now - 100},
             "g3": {"project_id": "blog", "status": "completed", "updated_at": now - 100}}
    jobs = {"j1": {"project_id": "shop-api", "role": "builder", "status": "merged", "merged_at": now - 50},
            "j2": {"project_id": "shop-api", "role": "builder", "status": "awaiting_review", "review_verdict": "pass"}}
    _, text = digest.build(now, projects, goals, jobs, {}, parents={"shop-api": "shop", "shop-app": "shop"})
    lines = text.splitlines()
    group = lines.index("• **Shop** (group of 2): 2 goals done · 1 merged · **1 waiting for approval**")
    assert lines[group + 1] == "    ◦ Shop: 1 goal done"
    assert lines[group + 2].startswith("    ◦ Shop API: 1 goal done · 1 merged")
    assert "• **Blog**: 1 goal done" in lines
    assert not any("Shop app" in line for line in lines)  # nothing happened there
