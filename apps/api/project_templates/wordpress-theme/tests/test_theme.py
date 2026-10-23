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


# --- theme.json the way WordPress reads it (a wrong shape is silently ignored) -----------------

CSS_VALUE = re.compile(r"^(-?\d*\.?\d+(px|rem|em|%|vw|vh|ch|ex|vmin|vmax)?|0|clamp\(.+\)|min\(.+\)|max\(.+\)|calc\(.+\)|var\(--.+\))$")
COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\(.+\)|hsla?\(.+\)|var\(--.+\)|transparent|currentColor|[a-z]+)$")
# Presets WordPress itself always offers (core theme.json), usable without defining them.
CORE = {"font-size": {"small", "medium", "large", "x-large", "xx-large"},
        "spacing": {"20", "30", "40", "50", "60", "70", "80"},
        "color": {"black", "cyan-bluish-gray", "white", "pale-pink", "vivid-red", "luminous-vivid-orange",
                  "luminous-vivid-amber", "light-green-cyan", "vivid-green-cyan", "pale-cyan-blue",
                  "vivid-cyan-blue", "vivid-purple"},
        "font-family": set(), "gradient": set(), "shadow": {"natural", "deep", "sharp", "outlined", "crisp"}}


def theme():
    return json.loads((THEME / "theme.json").read_text())


def presets():
    """{kind: slugs} this theme offers (plus WordPress's own)."""
    settings = theme().get("settings", {})
    found = {kind: set(slugs) for kind, slugs in CORE.items()}
    for kind, items in (("font-size", settings.get("typography", {}).get("fontSizes", [])),
                        ("font-family", settings.get("typography", {}).get("fontFamilies", [])),
                        ("color", settings.get("color", {}).get("palette", [])),
                        ("gradient", settings.get("color", {}).get("gradients", [])),
                        ("spacing", settings.get("spacing", {}).get("spacingSizes", [])),
                        ("shadow", settings.get("shadow", {}).get("presets", []))):
        found[kind] |= {str(item.get("slug")) for item in items if isinstance(item, dict)}
    return found


def test_font_sizes_have_the_shape_wordpress_reads():
    """size is a CSS value (a string); fluid is true/false or {"min": "...", "max": "..."} next to it."""
    sizes = theme().get("settings", {}).get("typography", {}).get("fontSizes", [])
    for item in sizes:
        assert isinstance(item, dict) and item.get("slug") and item.get("name"), f"font size needs slug and name: {item}"
        size = item.get("size")
        assert isinstance(size, str) and CSS_VALUE.match(size.strip()), \
            f"font size {item.get('slug')}: size must be a CSS value like \"1.25rem\" (fluid goes next to it), not {size!r}"
        fluid = item.get("fluid", False)
        assert isinstance(fluid, bool) or (isinstance(fluid, dict) and set(fluid) <= {"min", "max"}
                                           and all(isinstance(v, str) and CSS_VALUE.match(v) for v in fluid.values())), \
            f"font size {item.get('slug')}: fluid must be true/false or {{\"min\": \"…\", \"max\": \"…\"}}"


def test_other_presets_have_the_shape_wordpress_reads():
    settings = theme().get("settings", {})
    for item in settings.get("color", {}).get("palette", []):
        assert item.get("slug") and isinstance(item.get("color"), str) and COLOR.match(item["color"].strip()), f"colour: {item}"
    for item in settings.get("typography", {}).get("fontFamilies", []):
        assert item.get("slug") and isinstance(item.get("fontFamily"), str) and item["fontFamily"].strip(), f"font family: {item}"
    for item in settings.get("spacing", {}).get("spacingSizes", []):
        assert item.get("slug") and isinstance(item.get("size"), str) and CSS_VALUE.match(item["size"].strip()), f"spacing: {item}"
    for key in ("contentSize", "wideSize"):
        value = settings.get("layout", {}).get(key)
        assert value is None or (isinstance(value, str) and CSS_VALUE.match(value)), f"layout {key}: {value!r}"


def _style_values(node, path=""):
    """Every value under styles, with where it is."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _style_values(value, f"{path}.{key}")
    elif isinstance(node, list):
        for value in node:
            yield from _style_values(value, path)
    else:
        yield path, node


def test_styles_use_css_values():
    for where, value in _style_values(theme().get("styles", {}), "styles"):
        assert isinstance(value, (str, int, float, bool)), f"{where}: {value!r} is not a value WordPress can use"
        if where.endswith((".fontSize", ".lineHeight", ".letterSpacing")) and isinstance(value, str) and value:
            assert not value.startswith("{"), f"{where}: {value!r}"


def test_every_preset_used_exists():
    """var(--wp--preset--…), var:preset|…|… and block attributes like "fontSize":"huge" must name a preset
    this theme (or WordPress) defines, or the value is silently dropped."""
    known = presets()
    attribute = {"fontSize": "font-size", "fontFamily": "font-family", "backgroundColor": "color", "textColor": "color",
                 "borderColor": "color", "overlayColor": "color", "gradient": "gradient"}
    files = [THEME / "theme.json", *THEME.rglob("*.css"), *markup_files()]
    for path in files:
        text = path.read_text()
        for kind, slug in re.findall(r"var\(--wp--preset--([a-z-]+?)--([a-z0-9-]+)\)", text) + \
                re.findall(r"var:preset\|([a-z-]+)\|([a-z0-9-]+)", text):
            assert kind in known and slug in known[kind], f"{path.name}: preset {kind} '{slug}' is not defined"
        for name, kind in attribute.items():
            for slug in re.findall(rf'"{name}":"([a-z0-9-]+)"', text):
                assert slug in known[kind], f"{path.name}: {name} '{slug}' is not a {kind} preset"


def test_headings_get_bigger_than_text():
    """h1 must be set to a size (WordPress's default theme sizes would otherwise apply only partly)."""
    elements = theme().get("styles", {}).get("elements", {})
    if elements.get("h1"):
        assert elements["h1"].get("typography", {}).get("fontSize"), "styles.elements.h1 needs a fontSize"


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
