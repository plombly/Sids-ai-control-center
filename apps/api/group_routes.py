"""Approve everything a goal produced, through the merge queue (v1.0).

POST /api/goals/{goal_id}/approve-all {request_id, candidates: {job_id: sha}}

The human approves exactly the changes they saw: `candidates` names each
job and the integrated candidate commit shown to them. Every named job must
be ready (tests and independent review passed on that exact candidate);
each becomes an ordinary queued approval (operator action queue_approve,
contract item 13): the merge queue re-integrates the same sources whenever
main moves, and merges only through the unchanged approve() after the gate
and a fresh review. Ready jobs the human did not see are left alone.
Projects the LAIka builder manages are never approved this way.
"""

from typing import Dict

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()
SHA = r"^[0-9a-f]{40}$"


class ApproveAll(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,59}$")
    candidates: Dict[str, str] = Field(min_length=1, max_length=50)


def _sub_request_id(request_id, number):
    return f"{request_id}-{number:02d}"


@router.post("/api/goals/{goal_id}/approve-all", status_code=202)
def approve_all(goal_id: str, payload: ApproveAll, request: Request, response: Response):
    import re
    import main
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", goal_id) or not main._hash(f"laika:goals:{goal_id}"):
        raise HTTPException(status_code=404, detail="Goal not found")
    result = _approve_listed(payload, request,
                             lambda job: main._text(job.get("goal_id"), "") == goal_id, "not a change of this goal")
    return {"goal_id": goal_id, **result}


@router.post("/api/projects/{project_id}/approve-all", status_code=202)
def approve_all_in_group(project_id: str, payload: ApproveAll, request: Request):
    """Every ready change the person saw in a parent project and its
    children (any goal), through the merge queue like a goal's approve-all."""
    import main
    import project_routes as projects
    project_id = projects._id(project_id)
    if not projects._known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    members = {project_id} | {pid for pid in main.redis.smembers("laika:projects") or []
                              if main._text(main.redis.hget(f"laika:projects:{pid}", "parent"), "") == project_id}
    result = _approve_listed(payload, request,
                             lambda job: main._text(job.get("project_id"), "laika") in members,
                             "not a change of this project or its children")
    return {"project_id": project_id, **result}


def _approve_listed(payload, request, belongs, outside):
    """Queue the exact candidates the person saw; refuse everything if any is not ready."""
    import re
    import main
    import access
    import managed
    from schemas import OperatorActionRequest
    problems, ready = [], []
    for job_id, candidate in sorted(payload.candidates.items()):
        job = main._hash(f"laika:jobs:{job_id}") if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id) else {}
        if not job or not belongs(job):
            problems.append(f"{job_id}: {outside}")
        elif managed.view_only(main.redis, main._text(job.get("project_id"), "laika")):
            problems.append(f"{job_id}: {managed.MESSAGE}")
        elif not access.may(main.redis, getattr(request.state, "ctx", None), main._text(job.get("project_id"), "laika"), "approve"):
            problems.append(f"{job_id}: you need approve access to its project")
        elif not re.fullmatch(SHA, candidate or ""):
            problems.append(f"{job_id}: the candidate must be a full commit id")
        elif not main._approval_ready(job):
            problems.append(f"{job_id}: not ready to approve (status {job.get('status')})")
        elif main._text(job.get("integrated_candidate_commit"), "") != candidate:
            problems.append(f"{job_id}: changed since you looked (new candidate); refresh and decide again")
        else:
            ready.append((job_id, candidate, main._text(job.get("status"), "")))
    if problems:
        raise HTTPException(status_code=409, detail={"message": "Nothing was approved", "problems": problems})
    results = []
    for number, (job_id, candidate, status) in enumerate(ready, start=1):
        action = OperatorActionRequest(action="queue_approve", request_id=_sub_request_id(payload.request_id, number),
                                       expected_status=status, expected_candidate=candidate)
        try:
            result = main.request_job_action(job_id, action, request, Response())
            results.append({"job_id": job_id, "request_id": action.request_id, "status": result.get("status")})
        except HTTPException as exc:
            results.append({"job_id": job_id, "request_id": action.request_id, "error": str(exc.detail)})
    return {"queued": results}
