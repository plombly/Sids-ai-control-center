"""Every job status change, recorded: who changed it, from what, to what and
why, in a capped Redis stream per job (laika:job-events:<job id>). Job
records only keep their latest error; this is the history that shows why a
job went failed -> queued -> needs_human. Standard library only.

JobEventRedis(client, actor) wraps a redis-py style client: an HSET of
"status" on laika:jobs:<id> also appends {from, to, actor, reason, at,
unexpected} to the job's stream. Transitions are checked against
TRANSITIONS in warn-only mode: an unexpected one is recorded and logged,
never refused (LAIka has many legitimate writers; this finds the
surprising ones first).
"""

import re
import time

STREAM_PREFIX = "laika:job-events:"
MAXLEN = 200
_JOB_KEY = re.compile(r"^laika:jobs:([A-Za-z0-9_-]{1,64})$")
REASON_FIELDS = ("error", "needs_human_reason", "defer_reason", "retry_reason", "integration_error",
                 "blocked_reason", "review_error", "repair_error")

_ANY_FAILURE = {"failed", "test_failed", "integration_failed", "needs_human", "rejected", "deferred",
                "blocked_failed_dependency"}
TRANSITIONS = {
    None: None,  # a new record: anything
    # Reviews, repairs and integrations go straight to their working state.
    "queued": {"claimed", "running", "reviewing", "repairing", "integrating", "deferred", "failed", "blocked",
               "queued", "rejected", "blocked_failed_dependency"},
    "reviewing": {"review_complete", "failed"},
    "repairing": {"repair_complete", "testing", "failed"},
    "integrating": {"integrate_complete", "integration_failed", "failed"},
    "blocked": {"queued", "blocked", "blocked_failed_dependency", "rejected", "failed"},
    "claimed": {"running", "reviewing", "repairing", "integrating", "testing", "deferred", "failed",
                "completed_no_changes", "awaiting_review", "review_complete", "repair_complete",
                "integrate_complete", "integration_failed", "queued"},
    "running": {"testing", "awaiting_review", "completed_no_changes"} | _ANY_FAILURE,
    "testing": {"awaiting_review", "completed_no_changes", "running"} | _ANY_FAILURE,
    "deferred": {"queued", "failed"},
    "awaiting_review": {"awaiting_review", "merged", "integration_failed", "needs_human", "rejected", "failed",
                        "queued", "repairing", "test_failed"},
    "failed": {"queued", "needs_human", "failed", "rejected", "blocked_failed_dependency"},
    "test_failed": {"queued", "needs_human", "rejected", "failed", "blocked_failed_dependency"},
    "integration_failed": {"queued", "needs_human", "awaiting_review", "rejected", "failed"},
    "needs_human": {"queued", "awaiting_review", "rejected", "needs_human", "failed"},
    "blocked_failed_dependency": {"blocked", "queued", "rejected"},
    "merged": set(),
    "rejected": set(),
    "completed_no_changes": set(),
}


def allowed(before, after):
    if before not in TRANSITIONS or TRANSITIONS[before] is None:
        return True
    return after == before or after in TRANSITIONS[before]


def job_id_of(key):
    found = _JOB_KEY.match(str(key))
    return found.group(1) if found else None


def record(client, key, before, after, fields, actor, now=None, log=print):
    """Append one status change to the job's stream; never raises."""
    job_id = job_id_of(key)
    if job_id is None or after is None or before == after:
        return
    reason = next((str(fields[f]) for f in REASON_FIELDS if fields.get(f)), "")
    unexpected = not allowed(before, after)
    entry = {"from": before or "", "to": after, "actor": actor, "reason": reason[:300],
             "at": f"{time.time() if now is None else now:.3f}", "unexpected": "1" if unexpected else ""}
    try:
        client.xadd(STREAM_PREFIX + job_id, entry, maxlen=MAXLEN, approximate=True)
    except Exception as exc:
        log(f"[job-events] {job_id}: {exc}")
        return
    if unexpected:
        log(f"[job-events] unexpected transition for job {job_id}: {before} -> {after} by {actor}")


def timeline(client, job_id, limit=MAXLEN):
    """[{from, to, actor, reason, at, unexpected}] oldest first."""
    try:
        entries = client.xrange(STREAM_PREFIX + job_id, count=limit)
    except Exception:
        return []
    out = []
    for _, fields in entries or []:
        item = {k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
                for k, v in (fields or {}).items()}
        try:
            item["at"] = float(item.get("at") or 0)
        except ValueError:
            item["at"] = 0.0
        item["unexpected"] = item.get("unexpected") == "1"
        out.append(item)
    return out


class JobEventRedis:
    """A client wrapper that records job status changes made through it."""

    def __init__(self, client, actor, log=print):
        self._client, self._actor, self._log = client, actor, log

    def __getattr__(self, name):
        return getattr(self._client, name)

    def hset(self, key, field=None, value=None, mapping=None):
        fields = dict(mapping or {})
        if field is not None:
            fields[field] = value
        watched = "status" in fields and job_id_of(key) is not None
        before = None
        if watched:
            try:
                before = self._client.hget(key, "status")
            except Exception:
                before = None
        if field is not None:
            result = self._client.hset(key, field, value, mapping=mapping) if mapping else self._client.hset(key, field, value)
        else:
            result = self._client.hset(key, mapping=mapping)
        if watched:
            record(self._client, key, before, fields["status"], fields, self._actor, log=self._log)
        return result
