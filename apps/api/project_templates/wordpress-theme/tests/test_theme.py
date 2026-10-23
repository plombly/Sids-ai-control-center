"""Checks every change to the theme must pass (no WordPress needed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "theme"
BLOCK = re.compile(r"<!--\s+(/?)wp:([a-z0-9-]+(?:/[a-z0-9-]+)?)(\s+\{.*?\})?\s*(/?)-->", re.S)


CONTENT = ROOT / "content"


def markup_files():
    return sorted([*THEME.glob("templates/*.html"), *THEME.glob("parts/*.html"), *THEME.glob("patterns/*.php"),
                   *CONTENT.glob("pages/*.html")])


def test_theme_header():
    header = (THEME / "style.css").read_text()
    assert re.search(r"Theme Name:\s*\S", header), "style.css needs a Theme Name"
    assert (THEME / "templates" / "index.html").exists(), "a block theme needs templates/index.html"


def test_theme_json_is_valid():
    data = json.loads((THEME / "theme.json").read_text())
    assert data.get("version") in (2, 3)
    for part in data.get("templateParts", []):
        assert (THEME / "parts" / f"{part['name']}.html").exists(), f"missing parts/{part['name']}.html"


@pytest.mark.parametrize("path", markup_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_block_markup_is_balanced(path):
    stack = []
    for closing, name, attrs, self_closing in BLOCK.findall(path.read_text()):
        if attrs:
            json.loads(attrs)  # block attributes must be valid JSON
        if self_closing:
            continue
        if closing:
            assert stack and stack[-1] == name, f"{path.name}: /wp:{name} closes {stack[-1] if stack else 'nothing'}"
            stack.pop()
        else:
            stack.append(name)
    assert not stack, f"{path.name}: unclosed blocks {stack}"


def test_templates_use_parts_that_exist():
    for path in THEME.glob("templates/*.html"):
        for slug in re.findall(r'wp:template-part \{"slug":"([a-z0-9-]+)"', path.read_text()):
            assert (THEME / "parts" / f"{slug}.html").exists(), f"{path.name} uses missing part {slug}"


def test_php_files():
    for path in [*THEME.rglob("*.php"), *(ROOT / "mu-plugins").glob("*.php")]:
        text = path.read_text()
        assert text.startswith("<?php"), f"{path.name} must start with <?php"
        if path.parent.name == "patterns":
            assert re.search(r"Title:\s*\S", text) and re.search(r"Slug:\s*site/", text), f"{path.name}: pattern header (Title, Slug: site/…)"
    if shutil.which("php"):
        for path in [*THEME.rglob("*.php"), *(ROOT / "mu-plugins").glob("*.php")]:
            result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr


def test_no_external_assets():
    for path in THEME.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".php", ".css", ".json"):
            assert not re.search(r"https?://(?!schemas\.wp\.org)[^\s\"')]+\.(?:js|css|woff2?)", path.read_text()), \
                f"{path.name}: bundle assets in the theme instead of loading them from elsewhere"


def test_docker_files():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "FROM docker.io/library/wordpress" in dockerfile and "lnmt-entrypoint.sh" in dockerfile
    entry = (ROOT / "lnmt-entrypoint.sh").read_text()
    assert entry.startswith("#!/bin/bash") and "themes/site" in entry and 'exec docker-entrypoint.sh "$@"' in entry


def page_meta(path):
    match = re.match(r"<!--(.*?)-->", path.read_text(), re.S)
    assert match and "wp:" not in match.group(1).split("\n")[0], f"{path.name} must start with its <!-- title: … --> header"
    return dict(re.findall(r"^\s*([a-z_]+)\s*:\s*(.*?)\s*$", match.group(1), re.M))


def test_content_pages_and_settings():
    pages = {p.stem: page_meta(p) for p in CONTENT.glob("pages/*.html")}
    for slug, meta in pages.items():
        assert re.fullmatch(r"[a-z0-9-]+", slug), f"page file names are their address: {slug}"
        assert meta.get("title"), f"pages/{slug}.html needs a title"
    site = json.loads((CONTENT / "site.json").read_text())
    for key in ("front_page", "posts_page"):
        if site.get(key):
            assert site[key] in pages, f"site.json {key}: no page {site[key]}"
    for slug in site.get("menu", []):
        assert slug in pages, f"site.json menu: no page {slug}"


def test_content_images_exist():
    for path in CONTENT.glob("pages/*.html"):
        for name in re.findall(r"\{\{media:([A-Za-z0-9._-]+)\}\}", path.read_text()):
            assert (CONTENT / "media" / name).is_file(), f"{path.name} uses missing image media/{name}"
    for path in (CONTENT / "media").glob("*"):
        assert path.name.startswith(".") or path.stat().st_size < 10 * 2 ** 20, f"{path.name}: keep images under 10 MB"
