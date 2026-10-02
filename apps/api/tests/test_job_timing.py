import main

from test_dashboard_api import FakeRedis, client


def _job(status="running", **fields):
    return {"id": "j1", "status": status, **fields}


def test_running_job_has_live_elapsed_time(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({
        "sid:jobs:j1": _job(started_at="100.5"),
    }))
    monkeypatch.setattr(main.time, "time", lambda: 145.75)

    job = client.get("/api/jobs").json()[0]
    assert job["started_at"] == 100.5
    assert job["finished_at"] is None
    assert job["elapsed_seconds"] == 45.25


def test_finished_job_has_exact_elapsed_and_duration_fallback(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({
        "sid:jobs:j1": _job("completed", started_at="100.5", finished_at="145.75"),
    }))

    job = client.get("/api/jobs/j1").json()
    assert job["started_at"] == 100.5
    assert job["finished_at"] == 145.75
    assert job["elapsed_seconds"] == 45.25
    assert job["duration"] == 45.25
    assert main._duration({
        "started_at": "100.5", "finished_at": "145.75", "ended_at": "garbage",
    }) == 45.25


def test_old_job_has_null_timing_in_list_and_detail(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({
        "sid:jobs:j1": _job(),
    }))

    listed = client.get("/api/jobs")
    detail = client.get("/api/jobs/j1")
    assert listed.status_code == 200
    assert detail.status_code == 200
    for payload in (listed.json()[0], detail.json()):
        assert payload["started_at"] is None
        assert payload["finished_at"] is None
        assert payload["elapsed_seconds"] is None


def test_garbage_timing_values_are_null(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({
        "sid:jobs:j1": _job(started_at="garbage", finished_at="NaN"),
    }))

    job = client.get("/api/jobs/j1").json()
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["elapsed_seconds"] is None


def test_worker_job_started_at_is_float_or_null(client, monkeypatch):
    monkeypatch.setattr(main, "redis", FakeRedis({
        "sid:workers:w1": {"id": "w1", "job_started_at": "100.5"},
        "sid:workers:w2": {"id": "w2", "job_started_at": ""},
        "sid:workers:w3": {"id": "w3"},
    }))

    workers = {worker["id"]: worker for worker in client.get("/api/workers").json()}
    assert workers["w1"]["job_started_at"] == 100.5
    assert workers["w2"]["job_started_at"] is None
    assert workers["w3"]["job_started_at"] is None
