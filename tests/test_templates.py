"""Every starter template (apps/api/project_templates) passes its own tests once filled in."""

import shutil
import subprocess
import sys

import pytest

from laika_testing import ROOT

sys.path.insert(0, str(ROOT / "services"))
import laika_projects  # noqa: E402

TEMPLATES = sorted(laika_projects.templates())


@pytest.mark.parametrize("template_id", TEMPLATES)
def test_a_template_works_out_of_the_box(template_id, tmp_path):
    project = tmp_path / "project"
    meta = laika_projects.apply_template(template_id, project, "my-thing", "My Thing")
    assert meta["name"] and meta["description"] and meta["type"]
    texts = [path.read_text() for path in project.rglob("*") if path.is_file()]
    assert not any("{{" in text and "}}" in text and ("{{NAME}}" in text or "{{ID}}" in text or "{{PKG}}" in text) for text in texts)
    assert (project / "README.md").is_file() and not (project / "template.json").exists()
    if (project / "package.json").is_file():
        if not shutil.which("npm"):
            pytest.skip("npm is not installed")
        result = subprocess.run(["npm", "test"], cwd=project, capture_output=True, text=True, timeout=120)
    else:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=project,
                                capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    gate = laika_projects.detect_gate(project)
    assert gate in ("npm test", "python3 -m pytest -q")


def test_apps_with_a_page_or_port_have_a_run_command():
    for template_id in TEMPLATES:
        meta = laika_projects.templates()[template_id]
        files = {}
        root = laika_projects.TEMPLATES / template_id
        read = lambda name: (root / name).read_text() if (root / name).is_file() else None
        detected = laika_projects.detect_run(read)
        if template_id == "cli-tool":
            assert not detected and not meta["run_command"]
        else:
            assert detected or meta["run_command"], template_id
