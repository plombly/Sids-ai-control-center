"""While you were away (apps/api/away_routes.py)."""

import away_routes


def test_only_what_laika_did_by_itself_since_then():
    events = {"b1": [{"from": "running", "to": "deferred", "actor": "orchestrator", "reason": "worker lost while running", "at": "200"},
                     {"from": "deferred", "to": "queued", "actor": "orchestrator", "at": "210"},
                     {"from": "queued", "to": "claimed", "actor": "worker w1", "at": "220"}],
              "b2": [{"from": "test_failed", "to": "queued", "actor": "orchestrator", "at": "50"}]}
    items = away_routes.collect(events, {"b1": "Cart"}, since=100)
    assert [(i["job"], i["what"]) for i in items] == [
        ("b1", "picked up again without using an attempt"),
        ("b1", "survived a restart: the build runs again without using an attempt")]
    assert items[0]["title"] == "Cart"
