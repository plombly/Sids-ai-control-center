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
