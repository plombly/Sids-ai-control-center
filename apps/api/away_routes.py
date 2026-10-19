"""What LAIka handled by itself while you were away (home page strip).

GET /api/away?since=<epoch> (administrators): automatic recoveries from the
job histories (services/job_events.py) since then, newest first.
"""

import time

from fastapi import APIRouter, Query

router = APIRouter()

# (from, to) -> what it means, for changes LAIka made without anyone asking.
def describe(event):
    before, after, actor = event.get("from") or "", event.get("to") or "", event.get("actor") or ""
    if after == "deferred" and before in ("claimed", "running", "testing", "queued"):
        if "lost" in (event.get("reason") or ""):
            return "survived a restart: the build runs again without using an attempt"
        return "waited for a paused AI provider without using an attempt"
    if before == "deferred" and after == "queued":
        return "picked up again without using an attempt"
    if before in ("failed", "test_failed", "integration_failed") and after == "queued" and actor == "orchestrator":
        return "rebuilt automatically after a failure"
    if after == "needs_human":
        return "handed to you after LAIka's own tries"
    if after == "merged" and actor == "operator":
        return "merged (approved change)"
    return ""


def collect(events_by_job, titles, since, limit=30):
    items = []
    for job_id, events in events_by_job.items():
        for event in events:
            if float(event.get("at") or 0) < since:
                continue
            text = describe(event)
            if text:
                items.append({"job": job_id, "title": titles.get(job_id, job_id), "what": text,
                              "at": float(event.get("at") or 0), "reason": event.get("reason") or ""})
    items.sort(key=lambda item: -item["at"])
    return items[:limit]


@router.get("/api/away")
def away(since: float = Query(0, ge=0)):
    import main
    since = since or time.time() - 86400
    rows = dict(main._hashes("laika:jobs:*"))
    titles = {key.split(":", 2)[2]: main._job_title(data) for key, data in rows.items()}
    events_by_job = {}
    for job_id in titles:
        try:
            entries = main.redis.xrange(f"laika:job-events:{job_id}", min=f"{int(since * 1000)}-0", count=50)
        except Exception:
            continue
        if entries:
            events_by_job[job_id] = [fields for _, fields in entries]
    return {"since": since, "items": collect(events_by_job, titles, since)}
