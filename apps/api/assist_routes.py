"""Goal assistant sessions for the dashboard (apps/api/goal_assist.py).

The API only records the operator's messages and queues a turn
(laika:assist-queue); the host's apps service runs it (scripts/laika-assist.py)
and writes the questions or brief back. Submitting a brief is an ordinary
goal submission for the session's project.
"""

import json
import secrets
import time
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import goal_assist
import project_routes as projects

router = APIRouter()
MAX_ACTIVE = 6  # queued or thinking, across all projects


class AssistStart(BaseModel):
    idea: str = Field(min_length=1, max_length=4000)
    mode: Literal["plan", "chat"] = "plan"


class AssistReply(BaseModel):
    answers: Optional[List[str]] = Field(default=None, max_length=goal_assist.MAX_QUESTIONS)
    feedback: Optional[str] = Field(default=None, max_length=2000)
    message: Optional[str] = Field(default=None, max_length=goal_assist.MAX_MESSAGE)


class AssistSubmit(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    atomic: bool = False
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _redis():
    return projects._redis().redis


def view(session_id, session):
    return {"id": session_id, "project_id": session.get("project_id"), "status": session.get("status"),
            "turns": session.get("turns") or [], "questions": session.get("questions") or [],
            "brief": session.get("brief") or None, "error": session.get("error") or "",
            "goal_id": session.get("goal_id") or "", "cost_usd": projects._numeric(session.get("cost_usd")) or 0,
            "mode": session.get("mode") or "plan", "want": session.get("want") or "",
            "proposal": session.get("proposal") or None,
            "updated_at": projects._numeric(session.get("updated_at"))}


def _session(session_id):
    if not goal_assist.SESSION_ID.fullmatch(session_id or ""):
        raise HTTPException(status_code=404, detail="Assistant session not found")
    session = goal_assist.load(_redis(), session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Assistant session not found (sessions are kept for a day)")
    return session


def _queue(session_id, **fields):
    r = _redis()
    goal_assist.save(r, session_id, status="queued", error="", **fields)
    r.rpush(goal_assist.QUEUE_KEY, session_id)


def _active_count():
    r = _redis()
    return sum(1 for key in r.scan_iter(goal_assist.SESSION_PREFIX + "*")
               if goal_assist.SESSION_ID.fullmatch(key.rsplit(":", 1)[-1])
               and r.hget(key, "status") in goal_assist.ACTIVE)


@router.post("/api/projects/{project_id}/assistant", status_code=202)
def start(project_id: str, payload: AssistStart, request: Request):
    project_id = projects._id(project_id)
    if not projects._known(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if projects._project_data(project_id).get("status") in {"archived", "pending_key", "deleting"}:
        raise HTTPException(status_code=409, detail="Project is not accepting goals")
    idea = payload.idea.strip()
    if not idea:
        raise HTTPException(status_code=422, detail="Describe what you want first")
    if _active_count() >= MAX_ACTIVE:
        raise HTTPException(status_code=429, detail="The assistant is busy; try again in a minute or use Send as written")
    projects.check_spending(project_id, request)
    session_id = secrets.token_hex(8)
    import access
    if payload.mode == "chat":
        _queue(session_id, project_id=project_id, idea=idea, created_at=time.time(), mode="chat", want="reply",
               turns=[{"from": "you", "message": idea}], questions=[], brief={}, started_by=access.current_name())
    else:
        _queue(session_id, project_id=project_id, idea=idea, created_at=time.time(),
               turns=[{"from": "you", "idea": idea}], questions=[], brief={}, started_by=access.current_name())
    return view(session_id, goal_assist.load(_redis(), session_id))


@router.get("/api/assistant/{session_id}")
def get(session_id: str):
    return view(session_id, _session(session_id))


@router.post("/api/assistant/{session_id}/reply", status_code=202)
def reply(session_id: str, payload: AssistReply, request: Request):
    session = _session(session_id)
    projects.check_spending(session.get("project_id") or "laika", request)
    turns = session["turns"]
    if (payload.message or "").strip():
        # Conversation: any time the assistant is not busy, also after a brief.
        if session.get("mode") != "chat":
            raise HTTPException(status_code=409, detail="This session plans a goal; start a Conversation to chat")
        if session.get("status") not in ("reply", "brief", "failed"):
            raise HTTPException(status_code=409, detail="Wait for the reply first")
        if len([t for t in turns if t.get("from") == "you" and t.get("message")]) >= goal_assist.MAX_CHAT_MESSAGES:
            raise HTTPException(status_code=409, detail="This conversation is long: press Write the goal, or start a new one")
        _queue(session_id, turns=turns + [{"from": "you", "message": payload.message.strip()}], want="reply", proposal={})
        return view(session_id, goal_assist.load(_redis(), session_id))
    if len([t for t in turns if t.get("from") == "you"]) >= goal_assist.MAX_TURNS + goal_assist.MAX_CHAT_MESSAGES:
        raise HTTPException(status_code=409, detail="This conversation is long enough: edit the brief yourself or start over")
    if payload.answers is not None:
        if session.get("status") != "questions":
            raise HTTPException(status_code=409, detail="There are no open questions")
        asked = session.get("questions") or []
        answers = [{"question": q["question"], "answer": (payload.answers[i] if i < len(payload.answers) else "").strip()[:1000]}
                   for i, q in enumerate(asked)]
        turn = {"from": "you", "answers": answers}
    elif (payload.feedback or "").strip():
        if session.get("status") != "brief":
            raise HTTPException(status_code=409, detail="There is no brief to change yet")
        turn = {"from": "you", "feedback": payload.feedback.strip()}
    else:
        raise HTTPException(status_code=422, detail="Send answers or the changes you want")
    _queue(session_id, turns=turns + [turn], questions=[])
    return view(session_id, goal_assist.load(_redis(), session_id))


@router.post("/api/assistant/{session_id}/write-goal", status_code=202)
def write_goal(session_id: str, request: Request):
    """Conversation -> the goal brief, from the whole conversation."""
    session = _session(session_id)
    projects.check_spending(session.get("project_id") or "laika", request)
    if session.get("mode") != "chat" or session.get("status") not in ("reply", "brief", "failed"):
        raise HTTPException(status_code=409, detail="Nothing to write a goal from yet")
    _queue(session_id, want="brief")
    return view(session_id, goal_assist.load(_redis(), session_id))


@router.post("/api/assistant/{session_id}/adopt")
def adopt(session_id: str):
    """The goal the Conversation proposed becomes the brief (no AI call), to
    edit or start like any other."""
    session = _session(session_id)
    proposal = session.get("proposal") or {}
    if session.get("mode") != "chat" or session.get("status") != "reply" or not proposal.get("goal"):
        raise HTTPException(status_code=409, detail="There is no proposed goal to use")
    goal_assist.save(_redis(), session_id, status="brief", brief=proposal, want="brief", proposal={})
    return view(session_id, goal_assist.load(_redis(), session_id))


@router.post("/api/assistant/{session_id}/retry", status_code=202)
def retry(session_id: str):
    """The last turn failed (a limit, a timeout): run it again as it was."""
    session = _session(session_id)
    if session.get("status") != "failed":
        raise HTTPException(status_code=409, detail="Nothing failed")
    _queue(session_id)
    return view(session_id, goal_assist.load(_redis(), session_id))


@router.post("/api/assistant/{session_id}/cancel")
def cancel(session_id: str):
    session = _session(session_id)
    if session.get("status") != "submitted":
        goal_assist.save(_redis(), session_id, status="cancelled")
    return view(session_id, goal_assist.load(_redis(), session_id))


@router.post("/api/assistant/{session_id}/submit", status_code=202)
def submit(session_id: str, payload: AssistSubmit, request: Request):
    """Submit the (possibly edited) brief as a goal of the session's project."""
    session = _session(session_id)
    if session.get("status") == "submitted":
        return {"id": session.get("goal_id"), "status": "accepted", "project_id": session.get("project_id")}
    if session.get("status") != "brief":
        raise HTTPException(status_code=409, detail="There is no finished brief to submit")
    result = projects.submit_project_goal(
        session.get("project_id") or "laika",
        projects.ProjectGoal(goal=payload.goal, atomic=payload.atomic, request_id=payload.request_id), request)
    goal_assist.save(_redis(), session_id, status="submitted", goal_id=result.get("id") or "",
                     submitted=json.dumps({"edited": payload.goal.strip() != (session.get("brief") or {}).get("goal", "").strip()}))
    return result
