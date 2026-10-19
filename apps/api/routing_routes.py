"""Which AI did which work, when LAIka switched provider and why, and the
worker scaler's decisions (Settings -> AI, "Routing").

GET /api/routing?days=7 (administrators)
"""

import json
import time
from collections import Counter, defaultdict

from fastapi import APIRouter, Query

router = APIRouter()


def summarize(jobs, since):
    """{roles: [{role, runs: [{provider, model, count}]}], fallbacks: [...], waits: {...}}."""
    by_role = defaultdict(Counter)
    fallbacks, waits, accounts = [], Counter(), Counter()
    for job in jobs:
        try:
            when = float(job.get("created_at") or job.get("updated_at") or 0)
        except ValueError:
            when = 0
        if when < since:
            continue
        role = job.get("role") or job.get("job_role") or "builder"
        provider = job.get("provider") or ""
        if provider:
            by_role[role][(provider, job.get("model") or "")] += 1
            if job.get("provider_account"):
                accounts[(provider, job["provider_account"])] += 1
        switches = int(job.get("account_switches") or 0)
        if switches:
            waits["account_switches"] += switches
        if job.get("provider_fallback"):
            fallbacks.append({"job": job.get("id", ""), "role": role, "at": when,
                              "reason": str(job["provider_fallback"])[:200]})
        deferrals = int(job.get("deferrals") or 0)
        if deferrals:
            waits["provider_waits"] += deferrals
        interruptions = int(job.get("interruptions") or 0)
        if interruptions:
            waits["interruptions"] += interruptions
    roles = [{"role": role, "runs": [{"provider": p, "model": m, "count": n}
                                      for (p, m), n in counts.most_common()]}
             for role, counts in sorted(by_role.items())]
    fallbacks.sort(key=lambda item: -item["at"])
    return {"roles": roles, "fallbacks": fallbacks[:50], "waits": dict(waits),
            "accounts": [{"provider": p, "account": a, "count": n} for (p, a), n in accounts.most_common()]}


@router.get("/api/routing")
def routing(days: int = Query(7, ge=1, le=90)):
    import main
    since = time.time() - days * 86400
    jobs = [data for _, data in main._hashes("laika:jobs:*")]
    result = summarize(jobs, since)
    try:
        scaler = [json.loads(raw) for raw in main.redis.lrange("laika:scaler:log", 0, 99) or []]
    except Exception:
        scaler = []
    return {"days": days, **result, "scaler": scaler}
