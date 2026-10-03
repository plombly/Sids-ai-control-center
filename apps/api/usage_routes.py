"""Usage and cost: tokens and dollars per project, day, provider and role.

Sums what jobs already record (effective/input/output/cached tokens,
Claude's cost_usd, run time) plus the planner's cost on each goal. Reviews,
repairs and integrations count toward their builder's project. Claude cost
is what the CLI reports against the claude.ai plan; Codex reports tokens
only (it runs on the ChatGPT plan), so its cost shows as tokens.
"""

import datetime
import time
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, Request

from usage_core import TOKEN_FIELDS, _day, _number, summarize  # noqa: F401  (shared with the digest)

router = APIRouter()


@router.get("/api/usage")
def usage(request: Request, days: int = Query(default=30, ge=1, le=365), project: str = Query(default="", max_length=40)):
    import access
    import main
    jobs = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:jobs:*")}
    goals = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:goals:*")}
    visible = access.levels(main.redis, getattr(request.state, "ctx", None))
    if visible is not None:  # a member: only their projects
        if project and project not in visible:
            raise HTTPException(status_code=404, detail="Not found")
        jobs = {k: v for k, v in jobs.items() if (v.get("project_id") or "laika") in visible}
        goals = {k: v for k, v in goals.items() if (v.get("project_id") or "laika") in visible}
    result = summarize(jobs, goals, time.time() - days * 86400, only_project=project or None)
    result["days"] = days
    result["project"] = project or None
    return result
