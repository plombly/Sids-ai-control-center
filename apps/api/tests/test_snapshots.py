"""Hash reads: one pipelined round trip, non-hash keys skipped, short cache."""

import main


class Pipe:
    def __init__(self, store, calls):
        self.store, self.calls, self.ops = store, calls, []

    def hgetall(self, key):
        self.ops.append(key)

    def execute(self, raise_on_error=True):
        self.calls.append(list(self.ops))
        out = []
        for key in self.ops:
            value = self.store.get(key)
            out.append(Exception("WRONGTYPE") if isinstance(value, str) else (value or {}))
        return out


class PipeRedis:
    def __init__(self, store):
        self.store, self.calls = store, []

    def scan_iter(self, pattern):
        return iter(sorted(k for k in self.store if k.startswith(pattern[:-1])))

    def pipeline(self, transaction=False):
        return Pipe(self.store, self.calls)


def test_one_round_trip_and_non_hash_keys_skipped(monkeypatch):
    fake = PipeRedis({"laika:goals:g1": {"id": "g1"}, "laika:goals:g1:planning": "locked", "laika:goals:g2": {}})
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "_SNAPSHOT_TTL", 0)
    assert main._hashes("laika:goals:*") == [("laika:goals:g1", {"id": "g1"})]
    assert len(fake.calls) == 1, "all keys in one pipelined round trip"


def test_snapshot_cache_reuses_a_recent_read(monkeypatch):
    fake = PipeRedis({"laika:jobs:j1": {"id": "j1", "status": "queued"}})
    monkeypatch.setattr(main, "redis", fake)
    monkeypatch.setattr(main, "_SNAPSHOT_TTL", 30)
    monkeypatch.setattr(main, "_snapshots", {})
    main._hashes("laika:jobs:*")
    fake.store["laika:jobs:j1"]["status"] = "running"
    main._hashes("laika:jobs:*")
    assert len(fake.calls) == 1, "second read within the TTL is served from the snapshot"
    monkeypatch.setattr(main, "_SNAPSHOT_TTL", 0)
    assert main._hashes("laika:jobs:*")[0][1]["status"] == "running"
    assert len(fake.calls) == 2
