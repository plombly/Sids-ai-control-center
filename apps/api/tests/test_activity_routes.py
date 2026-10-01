"""Project activity: goals, jobs and the event log in one timeline."""

import json

import activity_routes


def test_timeline_merges_and_orders_everything():
    goals = {"g1": {"project_id": "shop", "goal": "Add pricing\nmore", "created_at": "100", "status": "completed", "updated_at": "400"},
             "g2": {"project_id": "other", "goal": "x", "created_at": "999"}}
    jobs = {"j1": {"project_id": "shop", "role": "builder", "status": "merged", "title": "Pricing page", "merged_at": "350"},
            "j2": {"project_id": "shop", "role": "builder", "status": "needs_human", "title": "Fix login", "updated_at": "300"},
            "r1": {"project_id": "shop", "role": "reviewer", "status": "review_complete", "updated_at": "500"}}
    logged = [json.dumps({"at": 450, "kind": "undo", "title": "Undo Upload a.txt", "ref": "abc"}), "not json"]
    events = activity_routes.timeline("shop", goals, jobs, logged)
    assert [e["kind"] for e in events] == ["undo", "goal_done", "merged", "stuck", "goal_started"]
    assert events[1]["title"] == "Goal finished: Add pricing"
    assert events[2]["title"] == "Approved and merged: Pricing page"
    assert all(e["ref"] != "g2" for e in events)
    assert len(activity_routes.timeline("shop", goals, jobs, logged, limit=2)) == 2
