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

from fastapi import APIRouter, Query

from usage_core import TOKEN_FIELDS, _day, _number, summarize  # noqa: F401  (shared with the digest)

router = APIRouter()


@router.get("/api/usage")
def usage(days: int = Query(default=30, ge=1, le=365), project: str = Query(default="", max_length=40)):
    import main
    jobs = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:jobs:*")}
    goals = {key.split(":", 2)[2]: data for key, data in main._hashes("laika:goals:*")}
    result = summarize(jobs, goals, time.time() - days * 86400, only_project=project or None)
    result["days"] = days
    result["project"] = project or None
    return result
