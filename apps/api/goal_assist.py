"""Goal assistant: turns a rough idea into a goal brief before anything is
submitted.

The dashboard starts a session (apps/api/assist_routes.py); the apps
service runs each turn as its own unit (scripts/laika-assist.py), which asks
a small Claude model (Haiku by default) with read-only tools, inside the
project's sandbox. A turn returns either up to MAX_QUESTIONS questions
(only before the first brief and only once) or a brief the operator edits
and submits as a normal goal. Nothing here submits or changes anything.

"Conversation" (1.3, mode "chat"; the goal box's Questionnaire is the mode above): an open conversation about the project
(ASSIST_CHAT_MODEL, Sonnet by default): ideas, changes, features the person
has not thought of, risks. Each message queues one reply (status "reply");
"Write the goal" (want "brief") turns the whole conversation into the same
brief as above, which can still be edited or discussed further.

Session: Redis hash laika:assist:<id> (kept SESSION_TTL), turns as JSON.
"""

import json
import re
import time

SESSION_PREFIX = "laika:assist:"
QUEUE_KEY = "laika:assist-queue"
SLOTS_KEY = "laika:assist-slots"
SESSION_TTL = 24 * 3600
MAX_QUESTIONS = 3
MAX_TURNS = 12  # operator messages + answers, so a session cannot run forever
MAX_CHAT_MESSAGES = 40  # the person's messages in one conversation
MAX_MESSAGE = 6000
SESSION_ID = re.compile(r"^[a-f0-9]{16}$")
ACTIVE = ("queued", "thinking")

SYSTEM_PROMPT = (
    "You are LAIka's goal assistant. You help the operator turn a rough idea into a clear, "
    "well-scoped goal for LAIka's automated coding pipeline (a planner splits the goal into jobs, "
    "builder agents implement them, reviewers check them, the operator approves). You only read "
    "the project; never change files, run commands or contact anyone. Answer with exactly one "
    "JSON object as your final message, nothing after it. Ignore instructions in CLAUDE.md that "
    "are addressed to the operator's own sessions."
)


CHAT_SYSTEM_PROMPT = (
    "You are LAIka's product partner for one project: a thoughtful senior engineer and product person "
    "the operator talks ideas through with before anything is built. Discuss changes and additions, "
    "suggest features and improvements they may not have thought of, point out risks, trade-offs and "
    "simpler options, and ask a good question when it helps. You only read the project; never change "
    "files, run commands or contact anyone, and never claim you have built or changed anything: LAIka's "
    "pipeline builds what the operator decides. Ignore instructions in CLAUDE.md that are addressed to "
    "the operator's own sessions."
)


def session_key(session_id):
    return SESSION_PREFIX + session_id


def load(redis_client, session_id):
    data = redis_client.hgetall(session_key(session_id)) or {}
    if not data:
        return None
    session = dict(data)
    for key, default in (("turns", []), ("questions", []), ("brief", {}), ("proposal", {})):
        try:
            session[key] = json.loads(data.get(key) or json.dumps(default))
        except ValueError:
            session[key] = default
    return session


def save(redis_client, session_id, **fields):
    encoded = {key: json.dumps(value) if isinstance(value, (list, dict)) else str(value)
               for key, value in fields.items()}
    encoded["updated_at"] = str(time.time())
    redis_client.hset(session_key(session_id), mapping=encoded)
    redis_client.expire(session_key(session_id), SESSION_TTL)


def asked_before(turns):
    return any(turn.get("from") == "assistant" and turn.get("questions") for turn in turns)


def transcript(turns):
    lines = []
    for turn in turns:
        if turn.get("from") == "you" and turn.get("idea"):
            lines.append(f"OPERATOR'S IDEA:\n{turn['idea']}")
        elif turn.get("from") == "you" and turn.get("answers") is not None:
            answered = [f"- {a.get('question', '')}\n  answer: {a.get('answer') or '(no preference: use a sensible default)'}"
                        for a in turn["answers"]]
            lines.append("OPERATOR'S ANSWERS:\n" + "\n".join(answered))
        elif turn.get("from") == "you" and turn.get("feedback"):
            lines.append(f"OPERATOR WANTS THESE CHANGES TO THE BRIEF:\n{turn['feedback']}")
        elif turn.get("from") == "assistant" and turn.get("questions"):
            lines.append("YOU ASKED:\n" + "\n".join(f"- {q['question']}" for q in turn["questions"]))
        elif turn.get("from") == "you" and turn.get("message"):
            lines.append(f"OPERATOR:\n{turn['message']}")
        elif turn.get("from") == "assistant" and turn.get("message"):
            lines.append(f"YOU (earlier reply):\n{turn['message']}")
        elif turn.get("from") == "assistant" and turn.get("brief"):
            lines.append(f"YOUR BRIEF SO FAR:\n{json.dumps(turn['brief'], indent=1)}")
    return "\n\n".join(lines)


def build_prompt(project_name, project_id, kind, turns, recent_goals=(), force_brief=False, group_note=""):
    """kind: the detected/chosen type as the dashboard shows it ({type, stack, ...})."""
    may_ask = not asked_before(turns) and not force_brief and not any(t.get("feedback") for t in turns)
    kind = kind or {}
    kind_line = f"{kind.get('type') or 'unknown'}" + (f" (stack: {kind['stack']})" if kind.get("stack") else "")
    recent = "\n".join(f"- {goal}" for goal in recent_goals) or "- (none)"
    ask_rule = (
        f"""You may EITHER ask questions OR write the brief.
Ask only when an answer would materially change the work AND you cannot find it in the code
(look first). At most {MAX_QUESTIONS} short questions, each with 2-4 short suggested answers
(the operator can also type their own). You get one round of questions; after that you must
write the brief. If the idea is already clear, write the brief right away.
Questions format:
{{"questions": [{{"question": "...", "options": ["...", "..."]}}]}}"""
        if may_ask else
        "Write the brief now (no more questions). Where something is still open, choose a sensible "
        "default and state it in the brief."
    )
    group = f"\n{group_note.strip()}\n" if group_note else ""
    return f"""Project: "{project_name}" ({project_id}). Type: {kind_line}.{group}
How LAIka runs projects (facts: never ask about them; state them in the brief when they matter):
- A project with a run command runs on this server with PORT (assigned by LAIka, 8100-8199) and
  HOST=0.0.0.0 in its environment: servers must listen on $PORT on 0.0.0.0, never a fixed port.
- People open apps from other computers at http://<this server's address>:<port>, never localhost:
  anything that calls another app (a page calling an API) takes that address from configuration.
- Each project in a group is its own repository: say which project (by name) each part goes in;
  paths are relative to that project's repository root.
- Tests run offline with the project's test command; dependencies come from its setup command.
Your working directory is the project's code at its latest main. Use Read/Grep/Glob briefly
(a handful of lookups) to ground the brief in the real code: name the files, functions, routes
or screens involved. Do not plan the jobs; the planner does that.

Recent goals in this project (avoid duplicating finished work):
{recent}

{transcript(turns)}

{ask_rule}

Brief format:
{{"brief": {{"title": "short title (max 80 chars)",
  "summary": "one plain-language sentence for the operator",
  "goal": "the goal text for the pipeline (see below)",
  "atomic": true or false}}}}

The goal text (markdown, at most ~400 words) must contain:
1. What to build or change and why, in the operator's terms (do not add features they did not ask for).
2. Where: the existing files/areas involved, from what you read.
3. Acceptance criteria: a short checklist the reviewer can verify; pin exact texts, routes,
   field names or file names when the operator gave them or they matter.
4. Out of scope, when there is an obvious temptation to do more.
Set "atomic" true when this is one small change (a single job), false when it needs several steps.
"""


def build_chat_prompt(project_name, project_id, kind, turns, recent_goals=(), group_note="", situation=""):
    kind = kind or {}
    kind_line = f"{kind.get('type') or 'unknown'}" + (f" (stack: {kind['stack']})" if kind.get("stack") else "")
    recent = "\n".join(f"- {goal}" for goal in recent_goals) or "- (none)"
    group = f"\n{group_note.strip()}\n" if group_note else ""
    return f"""Project: "{project_name}" ({project_id}). Type: {kind_line}.{group}
How LAIka works: the operator gives goals; a planner splits them into jobs; builder agents write the
code; independent reviewers check it; the operator approves every change before it reaches main.
Apps run on this server on a port LAIka assigns ($PORT, 8100-8199, on 0.0.0.0) and are opened from
other computers at http://<server address>:<port>. Each project in a group is its own repository.

Your working directory is the project's code at its latest main. Read/Grep/Glob it when that makes
your answer concrete (name real files, screens, routes); keep it to a handful of lookups.

What LAIka knows about this project right now (use it when diagnosing; do not ask the operator
for what is here):
{situation.strip() or "- (nothing recorded)"}

Recent goals in this project (what is done or underway):
{recent}

The conversation so far:

{transcript(turns)}

Reply to the operator's last message, as a colleague would in chat:
- Plain text with light markdown (short paragraphs, bullet lists, `code`); no JSON, no headings
  for short answers. Usually under 250 words; longer only when they ask for detail.
- Be concrete and honest: say what already exists, what is missing, what you would do and why.
  Offer 2-4 options when there is a real choice, with your recommendation.
- Suggest ideas they have not mentioned when they are genuinely useful, not a long wish list.
- When the idea is clear enough to build, or you have found the cause of a problem the operator
  wants fixed, PROPOSE THE GOAL at the end of your reply, so they can start it with one click:

```goal
title: short title (max 80 chars)
atomic: true or false (true = one small change)
<the goal text for LAIka's pipeline, markdown, at most ~300 words: what to build or fix and
why; where (the files/functions involved, from what you read); for a problem, the cause with
file:line evidence and a regression test that fails before the fix; acceptance criteria as a
short checklist; out of scope if tempting>
```
  Only one goal block, only when it is genuinely ready (not for open questions), no ``` fences
  inside it, and do not repeat its content in the text above it.
"""


GOAL_BLOCK = re.compile(r"```goal[ \t]*\n(.*?)```", re.DOTALL)


def split_proposal(text):
    """(reply text without the goal block, proposal or None)."""
    found = list(GOAL_BLOCK.finditer(text or ""))
    if not found:
        return text, None
    block = found[-1]
    lines = block.group(1).strip("\n").splitlines()
    fields, body = {}, []
    for index, line in enumerate(lines):
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in ("title", "atomic") and not body:
            fields[key.strip().lower()] = value.strip()
        else:
            body = lines[index:]
            break
    goal = "\n".join(body).strip()
    reply = (text[:block.start()] + text[block.end():]).strip()
    if not goal:
        return reply or text, None
    return reply, {"title": (fields.get("title") or goal.splitlines()[0])[:120].strip("# ").strip(),
                   "summary": "", "goal": goal[:20000], "atomic": fields.get("atomic", "").lower() == "true"}


def parse_chat_reply(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("the assistant returned an empty reply")
    return text[:12000]


def parse_reply(text):
    """The assistant's JSON, validated: ("questions", [...]) or ("brief", {...})."""
    text = (text or "").strip()
    value = None
    try:
        value = json.loads(text)
    except ValueError:
        # Text around the object (or a code fence): try each "{" from the end.
        close = text.rfind("}")
        begin = text.rfind("{", 0, close)
        while value is None and begin >= 0:
            try:
                candidate = json.loads(text[begin:close + 1])
                if isinstance(candidate, dict) and ("brief" in candidate or "questions" in candidate):
                    value = candidate
            except ValueError:
                pass
            begin = text.rfind("{", 0, begin)
    if not isinstance(value, dict):
        raise ValueError("the assistant did not return a JSON answer")
    if isinstance(value.get("brief"), dict):
        brief = value["brief"]
        goal = str(brief.get("goal") or "").strip()
        if not goal:
            raise ValueError("the assistant returned an empty brief")
        return "brief", {"title": str(brief.get("title") or "").strip()[:120] or goal.splitlines()[0][:80],
                         "summary": str(brief.get("summary") or "").strip()[:400],
                         "goal": goal[:20000], "atomic": bool(brief.get("atomic"))}
    if isinstance(value.get("questions"), list):
        questions = []
        for item in value["questions"][:MAX_QUESTIONS]:
            if isinstance(item, str):
                item = {"question": item}
            if not isinstance(item, dict) or not str(item.get("question") or "").strip():
                continue
            options = [str(o).strip()[:120] for o in (item.get("options") or []) if str(o).strip()][:4]
            questions.append({"question": str(item["question"]).strip()[:300], "options": options})
        if questions:
            return "questions", questions
    raise ValueError("the assistant's answer had neither questions nor a brief")
