import importlib.util
import os
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "laika-prune.py"
SPEC = importlib.util.spec_from_file_location("laika_prune", SCRIPT)
laika_prune = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(laika_prune)


class FakeRedis:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.calls = []

    def hgetall(self, key):
        self.calls.append(key)
        return self.mapping.get(key, {})


def age(path, timestamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.name.encode())
    os.utime(path, (timestamp, timestamp))
    return path


def run(root, fake, apply=False, now=1000):
    from io import StringIO

    out = StringIO()
    result = laika_prune.prune(root, 30, fake, apply, now=now, out=out)
    return result, out.getvalue()


def test_dry_run_reports_without_deleting(tmp_path):
    old = 1000 - 31 * 86400
    merged = age(tmp_path / "jobs" / "abc.jsonl", old)
    planner = age(tmp_path / "planner" / "old.json", old)
    fake = FakeRedis({"laika:jobs:abc": {"status": "merged"}})

    result, output = run(tmp_path, fake)

    assert result == (2, merged.stat().st_size + planner.stat().st_size)
    assert merged.exists() and planner.exists()
    assert f"would remove {merged}" in output
    assert f"would remove {planner}" in output
    assert "DRY RUN: 2 files," in output


def test_apply_prunes_eligible_jobs_and_planner(tmp_path):
    old = 1000 - 31 * 86400
    recent = 1000 - 2 * 86400
    merged = age(tmp_path / "jobs" / "merged.json", old)
    missing = age(tmp_path / "jobs" / "missing-tests.log", old)
    new = age(tmp_path / "jobs" / "merged-new.json", recent)
    nonmatching = age(tmp_path / "jobs" / "notes.txt", old)
    old_planner = age(tmp_path / "planner" / "old.json", old)
    new_planner = age(tmp_path / "planner" / "new.json", recent)
    fake = FakeRedis({"laika:jobs:merged": {"status": "merged"}})

    run(tmp_path, fake, apply=True)

    assert not merged.exists() and not missing.exists() and not old_planner.exists()
    assert new.exists() and nonmatching.exists() and new_planner.exists()


def test_active_job_logs_are_kept(tmp_path):
    old = 1000 - 31 * 86400
    fake = FakeRedis()
    for job_id, status in (("running", "running"), ("review", "awaiting_review"), ("human", "needs_human")):
        for suffix in (".jsonl", "-tests.log", ".review.json"):
            age(tmp_path / "jobs" / f"{job_id}{suffix}", old)
        fake.mapping[f"laika:jobs:{job_id}"] = {"status": status}

    run(tmp_path, fake, apply=True)

    assert list((tmp_path / "jobs").iterdir())


def test_integration_keeps_newest_twenty(tmp_path):
    old = 1000 - 31 * 86400
    for index in range(25):
        age(tmp_path / "integration" / f"report-{index}.json", old - index)
    fake = FakeRedis()

    run(tmp_path, fake, apply=True)

    assert {path.name for path in (tmp_path / "integration").iterdir()} == {
        f"report-{index}.json" for index in range(20)
    }


def test_fewer_than_twenty_old_integration_reports_are_kept(tmp_path):
    old = 1000 - 31 * 86400
    for index in range(3):
        age(tmp_path / "integration" / f"report-{index}.json", old - index)

    run(tmp_path, FakeRedis(), apply=True)

    assert len(list((tmp_path / "integration").iterdir())) == 3


def test_symlinks_are_not_followed(tmp_path):
    old = 1000 - 31 * 86400
    outside = age(tmp_path / "outside" / "outside.jsonl", old)
    (tmp_path / "jobs").mkdir()
    (tmp_path / "integration").mkdir()
    jobs_link = tmp_path / "jobs" / "outside-tests.log"
    integration_link = tmp_path / "integration" / "outside.json"
    jobs_link.symlink_to(outside)
    integration_link.symlink_to(outside)
    (tmp_path / "planner-link").mkdir()
    (tmp_path / "planner-link" / "x.json").symlink_to(outside)

    _, output = run(tmp_path, FakeRedis({"laika:jobs:outside": {"status": "merged"}}), apply=True)

    assert outside.exists() and jobs_link.exists() and integration_link.exists()
    assert str(outside) not in output


def test_job_id_regex_handles_hyphenated_ids():
    assert laika_prune._job_id(Path("abc123-tests.log")) == "abc123"
    assert laika_prune._job_id(Path("abc123.review.json")) == "abc123"
