"""Why a job is waiting, in one sentence, and pausing all new work.

GET  /api/jobs/{id}/why     {"waiting": bool, "reason": "..."} from the live state
GET  /api/system/pause      {"paused": bool, "by", "at", "reason"}
POST /api/system/pause      {"paused": bool, "reason"} (administrators)

Pausing sets laika:pause: workers claim no new job and the orchestrator
plans no new goal; running jobs finish and approved changes still merge.
"""

import json
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
PAUSE_KEY = "laika:pause"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ROLE_PROVIDER_DEFAULT = {"planner": "claude", "builder": "codex", "reviewer": "claude", "repair": "claude"}


class Pause(BaseModel):
    paused: bool
    reason: Optional[str] = Field(default="", max_length=200)


def _main():
    import main
    return main


def _clock(epoch):
    try:
        return time.strftime("%H:%M", time.localtime(float(epoch)))
    except (TypeError, ValueError):
        return "soon"


def explain(job, state):
    """(waiting, reason) for a job record given the live state:
    {paused, held: {provider: until}, workers: [{status}], queue: [ids], claude_full: bool,
     statuses: {job id: status}}."""
    status = job.get("status") or ""
    role = job.get("role") or job.get("job_role") or "builder"
    if status in ("merged", "rejected", "completed_no_changes", "review_complete", "repair_complete",
                  "integrate_complete"):
        return False, "finished"
    if status in ("claimed", "running", "testing", "reviewing", "repairing", "integrating"):
        return False, "being worked on now"
    if state.get("paused") and status in ("queued", "blocked", "deferred"):
        return True, "All new work is paused (Settings → Workers); it starts when work is resumed."
    if status == "needs_human":
        return True, f"Waiting for you: {(job.get('needs_human_reason') or 'LAIka gave up after several tries')[:240]}"
    if status == "deferred":
        if job.get("defer_kind") == "interrupted":
            return True, "Its worker was lost (restart or crash); it runs again in a moment without using an attempt."
        provider = job.get("defer_provider") or "the provider"
        until = (state.get("held") or {}).get(provider)
        when = f" until about {_clock(until)}" if until else ""
        return True, f"Waiting for {provider}, which is paused{when}: {(job.get('defer_reason') or '')[:160]}"
    if status == "blocked":
        if job.get("blocked_reason"):
            return True, f"Waiting for files another job is changing: {job['blocked_reason'][:200]}"
        try:
            deps = json.loads(job.get("dependencies") or "[]")
        except ValueError:
            deps = []
        pending = [d for d in deps if (state.get("statuses") or {}).get(d) not in ("merged", "completed_no_changes")]
        if pending:
            return True, f"Waiting for {len(pending)} earlier job(s) of the plan to be merged: {', '.join(pending[:4])}"
        return True, "Waiting to be released (dependencies just finished)."
    if status == "queued":
        provider = ROLE_PROVIDER_DEFAULT.get(role, "codex")
        until = (state.get("held") or {}).get(provider)
        if until and role == "builder":
            return True, f"Codex is paused until about {_clock(until)}; the build waits without using an attempt."
        if role in ("reviewer", "repair") and state.get("claude_full"):
            return True, ("Waiting for a Claude slot (every account's are in use); it falls back to Codex "
                          "after 15 minutes.")
        workers = state.get("workers") or []
        idle = [w for w in workers if (w.get("status") or "") == "idle"]
        queue = state.get("queue") or []
        position = queue.index(job.get("id")) + 1 if job.get("id") in queue else None
        ahead = f" (number {position} in line)" if position else ""
        if not workers:
            return True, "No worker is running; the scaler starts one when there is work."
        if not idle:
            return True, f"Every worker is busy{ahead}; it starts as soon as one is free."
        return True, f"Queued{ahead}; a free worker picks it up within seconds."
    if status == "awaiting_review":
        if job.get("merge_queue_state") in ("queued", "waiting"):
            return True, f"Approved; in the merge queue: {(job.get('merge_queue_reason') or 'merges once current on main')[:200]}"
        for kind, label in (("review", "Its review"), ("repair", "Its repair")):
            provider = job.get(f"{kind}_wait_provider")
            if provider and (kind != "repair" or job.get("repair_status") == "waiting"):
                until = (state.get("held") or {}).get(provider)
                when = f" until about {_clock(until)}" if until else ""
                return True, f"{label} waits for {provider}, which is paused{when}; no attempt is used."
        if job.get("integration_status") == "running":
            return True, "Being put on top of the latest main and tested (integration)."
        if job.get("review_status") != "complete":
            return True, "Waiting for its independent review."
        if job.get("review_verdict") == "changes_required":
            return True, "The review asked for changes; a repair is being prepared."
        return True, "Ready: waiting for your approval."
    if status in ("failed", "test_failed", "integration_failed"):
        return True, f"Failed: {(job.get('error') or job.get('integration_error') or '')[:200]} — LAIka retries it if it can."
    return False, status or "unknown"


def live_state():
    main = _main()
    r = main.redis
    state = {"paused": False, "held": {}, "workers": [], "queue": [], "claude_full": False, "statuses": {}}
    try:
        state["paused"] = bool(r.get(PAUSE_KEY))
        for provider in ("claude", "codex"):
            left = r.ttl(f"laika:provider-cooldown:{provider}")
            if isinstance(left, int) and left > 0:
                state["held"][provider] = time.time() + left
        state["workers"] = [main._hash(key) for key in main._keys("laika:workers:*")]
        state["queue"] = [json.loads(raw).get("id") for raw in r.lrange("laika:jobs", 0, 200) or []]
        import provider_routes
        room = provider_routes.capacity("claude")
        state["claude_full"] = not room["unlimited"] and room["free"] <= 0
        state["statuses"] = main._job_statuses()
    except Exception:
        pass
    return state


@router.get("/api/jobs/{job_id}/why")
def why(job_id: str):
    if not _ID.match(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    job = _main()._hash(f"laika:jobs:{job_id}")
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    waiting, reason = explain({**job, "id": job_id}, live_state())
    return {"waiting": waiting, "reason": reason}


@router.get("/api/system/pause")
def pause_state():
    try:
        raw = _main().redis.get(PAUSE_KEY)
    except Exception:
        raw = None
    if not raw:
        return {"paused": False}
    try:
        detail = json.loads(raw)
    except ValueError:
        detail = {}
    return {"paused": True, **{k: detail.get(k) for k in ("by", "at", "reason")}}


@router.post("/api/system/pause")
def set_pause(payload: Pause):
    import access
    main = _main()
    if payload.paused:
        main.redis.set(PAUSE_KEY, json.dumps({"by": access.current_name() or "operator", "at": time.time(),
                                              "reason": (payload.reason or "")[:200]}))
    else:
        main.redis.delete(PAUSE_KEY)
    return pause_state()
