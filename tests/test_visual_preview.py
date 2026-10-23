"""Rendered previews on LNMT before review (services/visual_preview.py, scripts/laika-visual.py)."""

import json
import subprocess
import sys

from laika_testing import ROOT, load_module

sys.path.insert(0, str(ROOT / "services"))
import visual_preview  # noqa: E402


class Redis:
    def __init__(self):
        self.hashes, self.strings, self.lists = {}, {}, {}

    def get(self, key):
        return self.strings.get(key)

    def hset(self, key, field=None, value=None, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {field: value})

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)


def test_only_sites_on_lnmt_ask_for_a_preview():
    r = Redis()
    assert visual_preview.request(r, "j1", "shop", "abc") is False and not r.lists
    r.strings["laika:link:deploy:shop"] = json.dumps({"app": "site"})
    assert visual_preview.request(r, "j1", "shop", "abc") is True
    assert r.lists[visual_preview.QUEUE] == ["j1"] and r.hashes["laika:jobs:j1"]["visual_status"] == "pending"
    assert visual_preview.request(r, "j2", "laika", "abc") is False


def test_the_reviewer_waits_for_this_candidates_preview_only():
    r = Redis()
    assert visual_preview.wait(r, "j1", "abc", sleep=lambda s: None) is None          # none asked for
    r.hashes["laika:jobs:j1"] = {"visual_status": "pending", "visual_commit": "old"}
    assert visual_preview.wait(r, "j1", "abc", sleep=lambda s: None) is None          # another candidate's
    r.hashes["laika:jobs:j1"] = {"visual_status": "pending", "visual_commit": "abc"}
    ticks = []

    def finish(_):
        ticks.append(1)
        r.hashes["laika:jobs:j1"]["visual_status"] = "done"
    assert visual_preview.wait(r, "j1", "abc", sleep=finish)["visual_status"] == "done" and ticks
    r.hashes["laika:jobs:j1"]["visual_status"] = "running"
    assert visual_preview.wait(r, "j1", "abc", seconds=0, sleep=lambda s: None) is None   # too long: review anyway


def test_the_review_prompt_names_every_screenshot_and_what_the_checks_found():
    data = {"visual_status": "done", "visual_preview": json.dumps({"url": "http://n:30000/", "errors": ["TypeError: x"], "pages": [
        {"path": "/", "shots": {"desktop": "/p/visual/j1/home-desktop.png", "phone": "/p/visual/j1/home-phone.png"},
         "problems": {"desktop": ["words broken across lines: Commuter"], "phone": []}}]})}
    section = visual_preview.review_section(data)
    assert "/ at desktop width: /p/visual/j1/home-desktop.png" in section and "words broken across lines: Commuter" in section
    assert "/ at phone width: /p/visual/j1/home-phone.png\n  checks found nothing" in section
    assert "Read tool" in section and "BLOCKING" in section and "TypeError: x" in section
    assert visual_preview.screenshot_dirs(data) == ["/p/visual/j1"]
    failed = visual_preview.review_section({"visual_status": "failed", "visual_error": "build failed: x"})
    assert "could not render" in failed and "build failed: x" in failed
    assert visual_preview.review_section(None) == ""


def test_the_preview_address_comes_from_lnmts_answer_to_the_push():
    out = "remote: Building a preview of 560f60f5896e …\nremote: LNMT preview: http://10.0.0.121:30000/\n"
    assert visual_preview.preview_url(out) == ("http://10.0.0.121:30000/", "")
    assert visual_preview.preview_url("remote: LNMT preview failed: build failed: x\n") == ("", "build failed: x")
    assert visual_preview.preview_url("")[0] == "" and "no answer" in visual_preview.preview_url("")[1]


def test_the_candidate_is_pushed_as_a_preview_branch_never_to_main(tmp_path):
    module = load_module(ROOT / "scripts/laika-visual.py", "laika_visual_test")
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "remote: LNMT preview: http://10.0.0.121:30001/\n")
    project = type("P", (), {"repo": tmp_path})()
    assert module.push_preview(project, "j1", "c" * 40, runner=runner) == ("http://10.0.0.121:30001/", "")
    assert calls[0][-1] == f"{'c' * 40}:refs/heads/lnmt-preview/j1" and "core.hooksPath=/dev/null" in calls[0]
    assert not any("main" in part for part in calls[0])


def test_the_page_checks_cover_what_the_first_site_got_wrong():
    js = visual_preview.CHECKS_JS
    for finding in ("scrolls sideways", "words broken across lines", "barely bigger than", "hidden from view",
                    "main headings (h1) on one page", "hardly be seen", "images that do not load", "reach past the right edge"):
        assert finding in js, finding
