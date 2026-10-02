"""Rebranding on the host: checked files, served in place of LAIka's own."""

import json

import pytest

from laika_testing import ROOT, load_module


@pytest.fixture
def br():
    return load_module(ROOT / "scripts/laika-branding.py", "laika_branding_test")


SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3"/></svg>'


def test_apply_and_reset(br, tmp_path):
    (tmp_path / "logo.svg").write_text(SVG)
    (tmp_path / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    source = tmp_path / "branding.json"
    source.write_text(json.dumps({"name": "Acme", "wordmark": ["Ac", "me"], "tagline": "Ours",
                                  "logo": str(tmp_path / "logo.svg"), "favicon": str(tmp_path / "icon.png")}))
    target = tmp_path / "out"
    brand = br.apply(source, target)
    assert brand == {"name": "Acme", "wordmark": ["Ac", "me"], "tagline": "Ours", "logo": "brand/logo.svg",
                     "favicon": "brand/favicon.png"}
    assert sorted(p.name for p in target.iterdir()) == ["brand.json", "favicon.png", "logo.svg"]
    assert json.loads((target / "brand.json").read_text())["name"] == "Acme"
    br.reset(target)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("svg", [
    '<svg><script>alert(1)</script></svg>',
    '<svg onload="x()"></svg>',
    '<svg><image href="https://evil.example/x.png"/></svg>',
    '<svg><a xlink:href="javascript:x()"></a></svg>',
    '<svg><style>@import url(https://evil.example/x.css);</style></svg>',
])
def test_unsafe_svgs_are_refused(br, tmp_path, svg):
    (tmp_path / "logo.svg").write_text(svg)
    with pytest.raises(ValueError, match="scripts, event handlers or external references"):
        br.check_image(tmp_path / "logo.svg", "logo")


def test_bad_inputs(br, tmp_path):
    with pytest.raises(ValueError, match="name"):
        br.build({"name": ""})
    with pytest.raises(ValueError, match="wordmark"):
        br.build({"name": "A", "wordmark": "nope"})
    (tmp_path / "logo.gif").write_bytes(b"GIF89a")
    with pytest.raises(ValueError, match=".svg or .png"):
        br.check_image(tmp_path / "logo.gif", "logo")
    (tmp_path / "big.svg").write_text("<svg>" + " " * 600_000 + "</svg>")
    with pytest.raises(ValueError, match="512 KB"):
        br.check_image(tmp_path / "big.svg", "logo")
