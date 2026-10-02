"""Shared helpers for host-side tests (job-review CLI, operator service).

Nothing here touches the live Redis, the live checkout or real Git: Redis is
an in-memory fake and Git is a scripted fake keyed on (args, cwd).
"""

import importlib.util
import itertools
import sys
import time
from pathlib import Path

from redis.exceptions import ResponseError

ROOT = Path(__file__).resolve().parents[1]
_module_ids = itertools.count()


def load_module(path, name=None):
    """Load a script by path (job-review.py has a hyphen, so no import)."""
    name = name or f"laika_test_{Path(path).stem.replace('-', '_')}_{next(_module_ids)}"
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
        self.zsets = {}
        self.groups = {}
        self.clock = clock

    def exists(self, key):
        return int(key in self.records or key in self.values)

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

    def smembers(self, key):
        return set(self.values.get(key) or set())

    def sadd(self, key, *members):
        self.values.setdefault(key, set()).update(members)

    def lpush(self, key, value):
        self.values.setdefault(key, []).insert(0, value)
        return len(self.values[key])

    def ltrim(self, key, start, end):
        self.values[key] = self.lrange(key, start, end)

    def lrem(self, key, count, value):
        items = self.values.get(key, [])
        kept = [item for item in items if item != value]
        self.values[key] = kept
        return len(items) - len(kept)

    def lrange(self, key, start, end):
        items = self.values.get(key, [])
        return list(items[start:] if end == -1 else items[start:end + 1])

    def delete(self, *keys):
        return sum(1 for key in keys
                   if self.values.pop(key, None) is not None
                   or self.records.pop(key, None) is not None)

    def scan_iter(self, pattern):
        prefix = pattern.removesuffix("*")
        return iter(sorted(k for k in [*self.records, *self.values] if k.startswith(prefix)))

    def eval(self, script, count, key, *args):
        if "ZREMRANGEBYSCORE" in script:  # agent_cli claude slot acquire
            now, expiry, holder, limit = float(args[0]), float(args[1]), args[2], int(args[3])
            zset = self.zsets.setdefault(key, {})
            for member in [m for m, score in zset.items() if score <= now]:
                del zset[member]
            if holder in zset or len(zset) < limit:
                zset[holder] = expiry
                return 1
            return 0
        # compare-and-delete lock release
        if self.values.get(key) == args[0]:
            del self.values[key]
            return 1
        return 0

    def zrem(self, key, *members):
        zset = self.zsets.setdefault(key, {})
        return sum(1 for m in members if zset.pop(m, None) is not None)

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
        self.branch_heads = {"laika/job-b1": "a" * 40}  # the builder branch
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


JOB = "b1"
BASE = "b" * 40
INTEGRATED = "c" * 40


def make_builder(job_review, status="awaiting_review", **fields):
    worktree = job_review.WORKTREE_ROOT / f"job-{JOB}"
    worktree.mkdir(exist_ok=True)
    record = {
        "id": JOB, "role": "builder", "status": status,
        "worktree": str(worktree), "branch": f"laika/job-{JOB}",
        **fields,
    }
    job_review.r.records[f"laika:jobs:{JOB}"] = record
    return record
