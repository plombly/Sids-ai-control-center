"""Goal assistant: apps/api/goal_assist.py and scripts/laika-assist.py."""

import json
import subprocess
import sys

import pytest

from laika_testing import ROOT, MemoryRedis, load_module

sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(ROOT / "services"))
import goal_assist  # noqa: E402

BRIEF = {"brief": {"title": "Pause menu", "summary": "Adds a pause menu.", "goal": "Add a pause menu in scenes/menu.gd.", "atomic": True}}


def test_parse_reply_accepts_questions_and_briefs_in_prose():
    kind, questions = goal_assist.parse_reply(
        'Looked around.\n```json\n{"questions": [{"question": "Which key?", "options": ["Esc", "P", "", 4, 5, 6]}, "Sound?",'
        ' {"question": "a"}, {"question": "d"}]}\n```')
    assert kind == "questions" and len(questions) == 3
    assert questions[0] == {"question": "Which key?", "options": ["Esc", "P", "4", "5"]}
    assert questions[1] == {"question": "Sound?", "options": []}
    kind, brief = goal_assist.parse_reply("Here it is: " + json.dumps(BRIEF))
    assert kind == "brief" and brief["atomic"] is True and brief["title"] == "Pause menu"
    for bad in ("no json", '{"brief": {"goal": ""}}', '{"questions": []}', "[1, 2]"):
        with pytest.raises(ValueError):
            goal_assist.parse_reply(bad)


def test_prompt_allows_one_round_of_questions_only():
    first = [{"from": "you", "idea": "add a pause menu"}]
    prompt = goal_assist.build_prompt("Game", "game", {"type": "game", "stack": "godot"}, first, ["Add a level"])
    assert "add a pause menu" in prompt and "stack: godot" in prompt and "- Add a level" in prompt
    assert '"questions"' in prompt
    answered = first + [{"from": "assistant", "questions": [{"question": "Which key?"}]},
                        {"from": "you", "answers": [{"question": "Which key?", "answer": ""}]}]
    later = goal_assist.build_prompt("Game", "game", {}, answered)
    assert '{"questions"' not in later and "Write the brief now" in later
    assert "no preference" in later
    feedback = first + [{"from": "assistant", "brief": BRIEF["brief"]}, {"from": "you", "feedback": "use P too"}]
    assert "Write the brief now" in goal_assist.build_prompt("Game", "game", {}, feedback)


class AssistRedis(MemoryRedis):
    def __init__(self):
        super().__init__()
        self.zsets = {}

    def eval(self, script, count, key, *args):
        members = self.zsets.setdefault(key, set())
        if len(members) < int(args[3]):
            members.add(args[2])
            return 1
        return 0

    def zrem(self, key, *members):
        self.zsets.get(key, set()).difference_update(members)


class Run:
    def __init__(self, text, ok=True, unavailable=False):
        self.text, self.ok, self.unavailable = text, ok, unavailable
        self.result = {"total_cost_usd": 0.01}

    def describe_error(self):
        return "boom"


@pytest.fixture
def assist(tmp_path, monkeypatch):
    module = load_module(ROOT / "scripts/laika-assist.py")
    monkeypatch.setattr(module, "LOG_ROOT", tmp_path)
    r = AssistRedis()
    repo = tmp_path / "repo"
    repo.mkdir()
    r.records["laika:projects:game"] = {"id": "game", "name": "Game", "repo": str(repo), "root": str(tmp_path),
                                      "worktrees": str(tmp_path / "w"), "logs": str(tmp_path / "l")}
    goal_assist.save(r, "a" * 16, status="queued", project_id="game", turns=[{"from": "you", "idea": "pause menu"}])
    calls = []

    def runner_for(*replies):
        replies = list(replies)

        def runner(role, prompt, cwd, log_path, timeout, **kwargs):
            calls.append({"prompt": prompt, "cwd": cwd, **kwargs})
            return replies.pop(0)
        return runner
    return module, r, runner_for, calls


def test_a_turn_stores_questions_with_read_only_haiku(assist):
    module, r, runner_for, calls = assist
    assert module.main("a" * 16, r=r, runner=runner_for(Run('{"questions": [{"question": "Key?"}]}'))) == 0
    session = goal_assist.load(r, "a" * 16)
    assert session["status"] == "questions" and session["questions"] == [{"question": "Key?", "options": []}]
    assert session["turns"][-1] == {"from": "assistant", "questions": [{"question": "Key?", "options": []}]}
    call = calls[0]
    assert call["model"].startswith("claude-haiku") and call["tools"] == "Read,Grep,Glob"
    assert call["system_prompt"] == goal_assist.SYSTEM_PROMPT and "wrap" in call
    assert not r.zsets[goal_assist.SLOTS_KEY]  # slot released


def test_questions_after_answers_get_one_retry_for_a_brief(assist):
    module, r, runner_for, calls = assist
    goal_assist.save(r, "a" * 16, turns=[{"from": "you", "idea": "x"}, {"from": "assistant", "questions": [{"question": "q"}]},
                                         {"from": "you", "answers": [{"question": "q", "answer": "y"}]}])
    module.main("a" * 16, r=r, runner=runner_for(Run('{"questions": ["again?"]}'), Run(json.dumps(BRIEF))))
    session = goal_assist.load(r, "a" * 16)
    assert session["status"] == "brief" and session["brief"]["title"] == "Pause menu"
    assert "Write the brief now" in calls[1]["prompt"] and session["cost_usd"] == "0.02"


def test_failures_are_explained(assist, monkeypatch):
    module, r, runner_for, calls = assist
    module.main("a" * 16, r=r, runner=runner_for(Run("", ok=False, unavailable=True)))
    session = goal_assist.load(r, "a" * 16)
    assert session["status"] == "failed" and "not available" in session["error"]
    assert r.get("laika:provider-cooldown:claude")
    # While Claude cools down nothing runs.
    goal_assist.save(r, "a" * 16, status="queued")
    module.main("a" * 16, r=r, runner=runner_for())
    assert "paused" in goal_assist.load(r, "a" * 16)["error"] and len(calls) == 1




def test_cancelled_sessions_do_nothing(assist):
    module, r, runner_for, calls = assist
    goal_assist.save(r, "a" * 16, status="cancelled")
    assert module.main("a" * 16, r=r, runner=runner_for()) == 0 and not calls


def test_busy_assistant_gives_up(assist, monkeypatch):
    module, r, runner_for, calls = assist
    r.zsets[goal_assist.SLOTS_KEY] = {"x", "y"}
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(module.time, "time", lambda: next(clock))
    module.main("a" * 16, r=r, runner=runner_for(), wait=lambda s: None)
    assert "busy" in goal_assist.load(r, "a" * 16)["error"] and not calls


def test_a_turn_cancelled_while_thinking_is_not_overwritten(assist):
    module, r, runner_for, calls = assist

    def runner(*args, **kwargs):
        goal_assist.save(r, "a" * 16, status="cancelled")
        return Run(json.dumps(BRIEF))
    module.main("a" * 16, r=r, runner=runner)
    assert goal_assist.load(r, "a" * 16)["status"] == "cancelled"


def test_apps_service_starts_queued_turns_only(monkeypatch):
    module = load_module(ROOT / "services/apps/laika_apps.py")
    module.redis = AssistRedis()
    started = []
    monkeypatch.setattr(module, "run", lambda args, **k: started.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    goal_assist.save(module.redis, "b" * 16, status="queued")
    goal_assist.save(module.redis, "c" * 16, status="cancelled")
    for session in ("b" * 16, "c" * 16, "../etc"):
        module.launch_assist(session)
    assert len(started) == 1 and started[0][-1] == "b" * 16 and started[0][-2].endswith("scripts/laika-assist.py")
