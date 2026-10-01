"""Shared SDKs (Flutter) in project sandboxes, Dart/Flutter detection and
re-running setup when dependencies change."""

import sys

import pytest

from sid_testing import ROOT, load_module

sys.path.insert(0, str(ROOT / "services"))
import project_sandbox  # noqa: E402
import sid_projects  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(project_sandbox, "enabled", lambda: True)
    sdk = tmp_path / "flutter"
    (sdk / "bin").mkdir(parents=True)
    monkeypatch.setattr(project_sandbox, "TOOLCHAINS", (str(sdk),))
    (tmp_path / "repo").mkdir()
    return type("P", (), {"id": "app", "is_sid": False, "repo": tmp_path / "repo", "root": tmp_path})(), sdk


def pairs(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


@pytest.mark.parametrize("kind", ["gate", "setup", "agent", "app"])
def test_every_sandbox_gets_a_throwaway_overlay_of_the_sdk(project, kind):
    p, sdk = project
    args = project_sandbox.command(["true"], p, p.repo, kind=kind)
    assert str(sdk) in pairs(args, "--overlay-src") and str(sdk) in pairs(args, "--tmp-overlay")
    assert project_sandbox.toolchain_paths() == [str(sdk / "bin")]


def test_pub_cache_is_shared_but_only_setup_and_agents_write_it(project):
    p, _ = project
    cache = str(p.root / "cache")
    for kind, writable in (("setup", True), ("agent", True), ("gate", False)):
        args = project_sandbox.command(["true"], p, p.repo, kind=kind)
        env = dict(zip(pairs(args, "--setenv"), [args[i + 2] for i, a in enumerate(args) if a == "--setenv"]))
        assert env["PUB_CACHE"] == cache + "/.pub-cache"
        assert (cache in pairs(args, "--bind")) is writable, kind


def test_flutter_and_dart_projects_are_detected(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\ndependencies:\n  flutter:\n    sdk: flutter\n")
    assert sid_projects.detect_setup(tmp_path) == "flutter pub get"
    assert sid_projects.detect_gate(tmp_path) == "flutter analyze --no-pub"
    (tmp_path / "test").mkdir()
    assert sid_projects.detect_gate(tmp_path) == "flutter analyze --no-pub && flutter test --no-pub"
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    assert sid_projects.detect_setup(tmp_path) == "dart pub get"
    assert sid_projects.detect_gate(tmp_path) == "dart analyze --no-pub && dart test --no-pub"


def test_setup_runs_again_when_dependencies_change(tmp_path):
    worker = load_module(ROOT / "services/worker/worker.py")
    (tmp_path / "pubspec.yaml").write_text("dependencies: {}\n")
    first = worker.setup_fingerprint(tmp_path, "flutter pub get")
    assert first == worker.setup_fingerprint(tmp_path, "flutter pub get")
    (tmp_path / "pubspec.yaml").write_text("dependencies: {http: ^1.0.0}\n")
    assert worker.setup_fingerprint(tmp_path, "flutter pub get") != first
    assert worker.setup_fingerprint(tmp_path, "npm ci") != worker.setup_fingerprint(tmp_path, "flutter pub get")
    assert ".dart_tool/" in worker.STANDARD_EXCLUDES
