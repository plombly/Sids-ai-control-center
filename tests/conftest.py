"""Shared fixtures for host-side tests (job-review CLI, operator service).

Nothing here touches the live Redis, the live checkout or real Git: Redis is
an in-memory fake and Git is a scripted fake keyed on (args, cwd).
"""

import importlib.util
import itertools
import sys
import time
from pathlib import Path

import pytest
from redis.exceptions import ResponseError

ROOT = Path(__file__).resolve().parents[1]
_module_ids = itertools.count()


def load_module(path, name=None):
    """Load a script by path (job-review.py has a hyphen, so no import)."""
    name = name or f"sid_test_{Path(path).stem.replace('-', '_')}_{next(_module_ids)}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MemoryRedis:
    """The subset of redis-py used by job-review.py and the operator."""

    def __init__(self, records=None, clock=time.time):
        self.records = records if records is not None else {}
        self.values = {}
        self.ttls = {}
        self.streams = {}
        self.groups = {}
        self.clock = clock

    # hashes
    def hgetall(self, key):
        return dict(self.records.get(key, {}))

    def hget(self, key, field):
        return self.records.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        record = self.records.setdefault(key, {})
        if field is not None:
            record[str(field)] = str(value)
        if mapping:
            record.update({str(k): str(v) for k, v in mapping.items()})
        return 1

    def hsetnx(self, key, field, value):
        record = self.records.setdefault(key, {})
        if field in record:
            return 0
        record[field] = str(value)
        return 1

    def hdel(self, key, *fields):
        record = self.records.setdefault(key, {})
        for field in fields:
            record.pop(field, None)
        return len(fields)

    # strings / lists
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def rpush(self, key, value):
        self.values.setdefault(key, []).append(value)
        return len(self.values[key])

    def eval(self, script, count, key, owner):
        # Only the compare-and-delete lock release is used.
        if self.values.get(key) == owner:
            del self.values[key]
            return 1
        return 0

    def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    # streams: one consumer group per stream is enough for the operator
    def xadd(self, name, fields, maxlen=None, approximate=True):
        ms = int(self.clock() * 1000)
        entries = self.streams.setdefault(name, [])
        seq = sum(1 for entry_id, _ in entries if entry_id.startswith(f"{ms}-"))
        entry_id = f"{ms}-{seq}"
        entries.append((entry_id, {str(k): str(v) for k, v in fields.items()}))
        return entry_id

    def xgroup_create(self, name, groupname, id="$", mkstream=False):
        if name not in self.streams:
            if not mkstream:
                raise ResponseError("ERR The XGROUP subcommand requires the key to exist")
            self.streams[name] = []
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups[(name, groupname)] = {"delivered": 0, "pending": {}}
        return True

    def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        replies = []
        for name, position in streams.items():
            group = self.groups[(name, groupname)]
            entries = dict(self.streams.get(name, []))
            if position == ">":
                fresh = self.streams[name][group["delivered"]:][:count]
                group["delivered"] += len(fresh)
                for entry_id, _ in fresh:
                    group["pending"][entry_id] = consumername
                messages = [(entry_id, dict(fields)) for entry_id, fields in fresh]
            else:
                ids = [i for i, c in group["pending"].items() if c == consumername][:count]
                messages = [(i, dict(entries.get(i, {}))) for i in ids]
            if messages:
                replies.append([name, messages])
        return replies

    def xack(self, name, groupname, *ids):
        pending = self.groups[(name, groupname)]["pending"]
        return sum(1 for i in ids if pending.pop(i, None) is not None)

    def pending(self, name, groupname):
        return dict(self.groups[(name, groupname)]["pending"])


class GitResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def memory_redis():
    return MemoryRedis()


@pytest.fixture
def job_review(memory_redis, tmp_path):
    """A fresh job-review module bound to fake Redis and a temp worktree root."""
    module = load_module(ROOT / "scripts/job-review.py")
    module.r = memory_redis
    module.REPO_ROOT = tmp_path / "main"
    module.REPO_ROOT.mkdir()
    module.WORKTREE_ROOT = tmp_path / "worktrees"
    module.WORKTREE_ROOT.mkdir()
    return module


class FakeGit:
    """Scripted git for job-review. State fields describe the repository;
    every call is recorded so tests can assert main was never advanced."""

    def __init__(self, module):
        self.module = module
        self.calls = []
        self.main_head = "b" * 40
        self.main_dirty = False
        self.integrated_head = "c" * 40
        self.integrated_dirty = False
        self.branch_heads = {}
        self.failing = set()

    def __call__(self, *args, cwd=None, check=True):
        cwd = Path(cwd if cwd is not None else self.module.REPO_ROOT)
        self.calls.append((args, cwd))
        main = cwd == Path(self.module.REPO_ROOT)
        if args[:2] in self.failing:
            result = GitResult("", f"git {' '.join(args)} failed", 1)
        elif args == ("status", "--porcelain"):
            dirty = self.main_dirty if main else self.integrated_dirty
            result = GitResult(" M file\n" if dirty else "")
        elif args == ("rev-parse", "HEAD"):
            result = GitResult((self.main_head if main else self.integrated_head) + "\n")
        elif args[0] == "rev-parse":
            result = GitResult(self.branch_heads.get(args[1], "0" * 40) + "\n")
        elif args[0] == "show-ref":
            ref = args[-1].removeprefix("refs/heads/")
            result = GitResult(returncode=0 if ref in self.branch_heads else 1)
        elif args[:2] in {("merge", "--ff-only"), ("worktree", "remove"),
                          ("branch", "-d"), ("branch", "-D")}:
            result = GitResult()
        else:
            raise AssertionError(f"unexpected git call: {args} cwd={cwd}")
        if check and result.returncode != 0:
            self.module.fail(result.stderr.strip() or result.stdout.strip())
        return result

    def ran(self, *prefix):
        return [args for args, _ in self.calls if args[:len(prefix)] == prefix]


@pytest.fixture
def fake_git(job_review):
    git = FakeGit(job_review)
    job_review.git = git
    return git


JOB = "b1"
BASE = "b" * 40
INTEGRATED = "c" * 40


def make_builder(job_review, status="awaiting_review", **fields):
    worktree = job_review.WORKTREE_ROOT / f"job-{JOB}"
    worktree.mkdir(exist_ok=True)
    record = {
        "id": JOB, "role": "builder", "status": status,
        "worktree": str(worktree), "branch": f"sid/job-{JOB}",
        **fields,
    }
    job_review.r.records[f"sid:jobs:{JOB}"] = record
    return record


@pytest.fixture
def approvable(job_review, fake_git):
    """A builder/reviewer pair that satisfies every approval check."""
    integration = job_review.WORKTREE_ROOT / f"job-{JOB}-integration"
    integration.mkdir()
    record = make_builder(
        job_review,
        review_status="complete", review_verdict="pass", review_job_id="rv1",
        integration_status="passed", integration_base_commit=BASE,
        integrated_candidate_commit=INTEGRATED, reviewed_commit=INTEGRATED,
        integration_worktree=str(integration),
        integration_branch=f"sid/integration-{JOB}",
    )
    reviewer = {
        "id": "rv1", "role": "reviewer", "builder_job_id": JOB,
        "status": "review_complete", "review_verdict": "pass",
        "candidate_commit": INTEGRATED, "reviewed_commit": INTEGRATED,
    }
    job_review.r.records["sid:jobs:rv1"] = reviewer
    fake_git.main_head = BASE
    fake_git.integrated_head = INTEGRATED
    fake_git.branch_heads[f"sid/integration-{JOB}"] = INTEGRATED
    return record, reviewer, integration
