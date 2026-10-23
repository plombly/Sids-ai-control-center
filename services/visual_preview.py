"""Looking at a change before it is reviewed: rendered previews on LNMT.

For a project that deploys to an LNMT app (laika:link:deploy:<project>),
every integrated candidate is pushed to the app's repository as branch
lnmt-preview/<job>; LNMT builds it and runs a throwaway copy (WordPress
with its own empty database), and answers "LNMT preview: <url>" in the
push's output. scripts/laika-visual.py (a unit of the apps service, as
root) then opens the front page and the pages in its menu in headless
Chrome (the play-test's browser) at desktop and phone width, takes
full-page screenshots, and checks what a visitor would notice:
horizontal scrolling, words broken across lines in headings, links and
buttons, a main heading barely bigger than the text, images that do not
load, page errors.

Job fields (on the builder): visual_status pending | running | done |
failed, visual_commit (the candidate it is for), visual_preview (JSON:
url, pages [{path, title, shots {desktop, phone}, problems {desktop,
phone}}], errors), visual_error. Screenshots live in
<project root>/visual/<job>/ (readable by agents of that project).

The reviewer waits for it (up to VISUAL_WAIT_SECONDS), gets the problems
and the screenshot paths in its prompt and reads the images; the dashboard
shows them on the approval card.
"""

import json
import os
import re
import time
from pathlib import Path

QUEUE = "laika:visual-queue"
WAIT_SECONDS = int(os.environ.get("VISUAL_WAIT_SECONDS", "600"))
WIDTHS = (("desktop", 1280, 900), ("phone", 390, 844))
MAX_PAGES = 4
URL = re.compile(r"LNMT preview: (http://[A-Za-z0-9.-]+:\d+/)")
FAILED = re.compile(r"LNMT preview failed: (.*)")


def enabled():
    return os.environ.get("VISUAL_PREVIEWS", "true").lower() not in ("0", "false", "no", "off")


def wanted(redis, project_id):
    """Does this project deploy to an LNMT app (so a preview can be made)?"""
    return enabled() and bool(project_id) and project_id != "laika" and bool(redis.get(f"laika:link:deploy:{project_id}"))


def request(redis, job_id, project_id, commit):
    """Ask for a preview of an integrated candidate (worker, after the gate passed)."""
    if not wanted(redis, project_id):
        return False
    redis.hset(f"laika:jobs:{job_id}", mapping={"visual_status": "pending", "visual_commit": commit,
                                                "visual_error": "", "visual_preview": ""})
    redis.rpush(QUEUE, job_id)
    return True


def wait(redis, job_id, commit, seconds=None, sleep=time.sleep, tick=None):
    """The builder's preview for this candidate once finished (or None: none
    was asked for, or it took too long)."""
    deadline = time.time() + (WAIT_SECONDS if seconds is None else seconds)
    while True:
        data = redis.hgetall(f"laika:jobs:{job_id}") or {}
        if data.get("visual_commit") != commit or not data.get("visual_status"):
            return None
        if data["visual_status"] in ("done", "failed"):
            return data
        if time.time() >= deadline:
            return None
        if tick:
            tick()
        sleep(5)


def review_section(data):
    """What the reviewer is told about the rendered candidate (and the image
    paths it should look at), or ""."""
    if not data:
        return ""
    if data.get("visual_status") == "failed":
        return ("\nRendered preview: LAIka could not render this candidate on LNMT "
                f"({data.get('visual_error', 'unknown error')}). If the reason is in this change "
                "(for example a build or startup error), that is BLOCKING.\n")
    try:
        preview = json.loads(data.get("visual_preview") or "{}")
    except ValueError:
        return ""
    lines = []
    for page in preview.get("pages", []):
        for width, _, _ in WIDTHS:
            shot = (page.get("shots") or {}).get(width)
            if shot:
                problems = (page.get("problems") or {}).get(width) or []
                lines.append(f"- {page.get('path', '/')} at {width} width: {shot}"
                             + (f"\n  checks found: {'; '.join(problems)}" if problems else "\n  checks found nothing"))
    if not lines:
        return ""
    errors = preview.get("errors") or []
    return ("\nRendered preview (LAIka built this exact candidate on LNMT and opened it in a browser):\n"
            + "\n".join(lines)
            + (f"\nPage errors: {'; '.join(errors[:10])}" if errors else "")
            + "\nOpen every screenshot above with the Read tool and judge what a visitor sees. A visible "
              "defect that comes from this change is BLOCKING: text far too large or too small, words broken "
              "across lines, overlapping or cut-off content, the page scrolling sideways on a phone, images "
              "that do not load, an empty or broken page, or a look that clearly misses what the task asked "
              "for. Name the page and width for each visible problem.\n")


def screenshot_dirs(data):
    """Folders the reviewer needs to read the screenshots from."""
    try:
        preview = json.loads((data or {}).get("visual_preview") or "{}")
    except ValueError:
        return []
    return sorted({str(Path(shot).parent) for page in preview.get("pages", [])
                   for shot in (page.get("shots") or {}).values() if shot})


def preview_url(push_output):
    """(url, error) from the output of a push to lnmt-preview/<job>."""
    match = URL.search(push_output or "")
    if match:
        return match.group(1), ""
    failed = FAILED.search(push_output or "")
    return "", (failed.group(1).strip() if failed else "LNMT did not make a preview (no answer in the push output)")


# Run in the page: what a visitor would notice (a list of plain sentences).
CHECKS_JS = r"""(() => {
  const out = [];
  const vw = window.innerWidth;
  const doc = document.documentElement;
  if (doc.scrollWidth > vw + 2) out.push(`the page scrolls sideways (${doc.scrollWidth}px wide in a ${vw}px window)`);
  const broken = new Set();
  for (const el of document.querySelectorAll('h1,h2,h3,h4,h5,h6,a,button,.wp-block-button__link,nav li,th,label')) {
    if (!el.offsetParent) continue;
    for (const node of el.childNodes) {
      if (node.nodeType !== 3) continue;
      const re = /[^\s\-–—/]{4,}/g;
      let m;
      while ((m = re.exec(node.data))) {
        const range = document.createRange();
        range.setStart(node, m.index);
        range.setEnd(node, m.index + m[0].length);
        const lines = new Set([...range.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.top)));
        if (lines.size > 1) broken.add(m[0]);
      }
    }
  }
  if (broken.size) out.push(`words broken across lines: ${[...broken].slice(0, 8).join(', ')}`);
  const body = parseFloat(getComputedStyle(document.body).fontSize) || 16;
  const h1 = [...document.querySelectorAll('h1')].find(e => e.offsetParent);
  if (h1) {
    const size = parseFloat(getComputedStyle(h1).fontSize);
    if (size < body * 1.4) out.push(`the main heading is ${Math.round(size)}px, barely bigger than the ${Math.round(body)}px text`);
  }
  const missing = [...document.images].filter(i => i.complete && i.naturalWidth === 0).map(i => i.getAttribute('src') || '?');
  if (missing.length) out.push(`images that do not load: ${missing.slice(0, 5).join(', ')}`);
  const wide = [...document.querySelectorAll('body *')].filter(e => {
    if (!e.offsetParent) return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.right > vw + 2 && getComputedStyle(e).position !== 'fixed';
  });
  if (wide.length) out.push(`${wide.length} element(s) reach past the right edge (first: <${wide[0].tagName.toLowerCase()}${wide[0].className ? ' class="' + String(wide[0].className).slice(0, 60) + '"' : ''}>)`);
  if (!(document.body.innerText || '').trim()) out.push('the page shows no text');
  const links = [...document.querySelectorAll('nav a[href]')].map(a => a.href)
    .filter(h => h.startsWith(location.origin) && !h.includes('#')).map(h => new URL(h).pathname);
  return {problems: out, title: document.title, links: [...new Set(links)]};
})()"""
