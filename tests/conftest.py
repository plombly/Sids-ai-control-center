"""Shared fixtures for host-side tests (job-review CLI, operator service).

Nothing here touches the live Redis, the live checkout or real Git: Redis is
an in-memory fake and Git is a scripted fake keyed on (args, cwd).
"""

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

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

    def __init__(self, records=None):
        self.records = records if records is not None else {}
        self.values = {}

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
