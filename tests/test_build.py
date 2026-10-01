"""scripts/sid-build.py and project_catalog.resolve_recipe: Build button."""

import json
import subprocess
import sys
import zipfile

import pytest

from sid_testing import ROOT, MemoryRedis, load_module

sys.path.insert(0, str(ROOT / "apps/api"))
import project_catalog  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def build(tmp_path, monkeypatch):
    module = load_module(ROOT / "scripts/sid-build.py")
    base = tmp_path / "projects"
    monkeypatch.setattr(module.sid_projects, "PROJECTS_BASE", base)
    repo = base / "game" / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "main.lua").write_text("function love.draw() end\n")
    (repo / "conf.lua").write_text("\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "v1", cwd=repo)
    r = MemoryRedis()
    r.records["sid:projects:game"] = {"id": "game", "root": str(base / "game"), "repo": str(repo),
                                      "worktrees": str(base / "game/worktrees"), "logs": str(base / "game/logs")}
    r.set("sid:project-type:game", json.dumps({"type": "game", "stack": "love2d"}))
    runs = []

    def runner(command, stdout=None, stderr=None):
        runs.append(command)
        work = next(a.split(":")[0] for a in command if a.endswith(":/src"))
        out = __import__("pathlib").Path(work) / "build"
        out.mkdir()
        (out / "game.love").write_text("zip")
        (out / ".git").write_text("gitdir: /elsewhere\n")
        stdout.write("built\n")
        return subprocess.CompletedProcess(command, 0)

    return module, r, repo, base / "game" / "builds", runs, runner


def start(r, build_id):
    r.lpush("sid:builds:game", build_id)
    r.hset(f"sid:build:game:{build_id}", mapping={"id": build_id, "status": "queued"})


def test_a_build_runs_in_docker_and_keeps_a_zip(build):
    module, r, repo, folder, runs, runner = build
    start(r, "b1")
    assert module.main("game", "b1", r=r, runner=runner) == 0
    record = r.hgetall("sid:build:game:b1")
    assert record["status"] == "succeeded" and record["files"] == "1"
    assert record["commit"] == subprocess.run(["git", "-C", str(repo), "rev-parse", "main"], capture_output=True,
                                              text=True).stdout.strip()
    with zipfile.ZipFile(folder / "b1.zip") as archive:
        assert archive.namelist() == ["game.love"]
    assert "built" in (folder / "b1.log").read_text()
    command = runs[0]
    assert command[:2] == ["docker", "run"] and "--rm" in command and "--memory" in command
    assert "alpine:3" in command and "sid-build-cache-game:/root/.cache" in command
    # Nothing of this server but the checkout and the cache is mounted.
    mounts = [command[i + 1] for i, a in enumerate(command) if a == "-v"]
    assert len(mounts) == 2
    # The checkout is gone and git forgot it.
    assert not (folder / "work-b1").exists()
    assert "work-b1" not in subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                                           text=True).stdout
    assert json.loads(r.lrange("sid:events:game", 0, 0)[0])["kind"] == "build"


def test_a_failing_build_keeps_its_log(build):
    module, r, repo, folder, runs, runner = build
    start(r, "b1")

    def failing(command, stdout=None, stderr=None):
        stdout.write("error: boom\n")
        return subprocess.CompletedProcess(command, 2)

    assert module.main("game", "b1", r=r, runner=failing) == 1
    record = r.hgetall("sid:build:game:b1")
    assert record["status"] == "failed" and "exit 2" in record["error"]
    assert "boom" in (folder / "b1.log").read_text() and not (folder / "b1.zip").exists()


def test_missing_output_and_escaping_output_fail(build):
    module, r, repo, folder, runs, runner = build
    start(r, "b1")
    module.main("game", "b1", r=r, runner=lambda c, stdout=None, stderr=None: subprocess.CompletedProcess(c, 0))
    assert "was not created" in r.hgetall("sid:build:game:b1")["error"]
    r.hset("sid:projects:game", mapping={"build_command": "true", "build_output": "../.."})
    start(r, "b2")
    module.main("game", "b2", r=r, runner=runner)
    assert r.hgetall("sid:build:game:b2")["status"] == "failed"


def test_symlinks_in_the_output_are_not_followed(build, tmp_path):
    module, r, repo, folder, runs, runner = build
    secret = tmp_path / "secret.txt"
    secret.write_text("do not ship")

    def sneaky(command, stdout=None, stderr=None):
        work = next(a.split(":")[0] for a in command if a.endswith(":/src"))
        out = __import__("pathlib").Path(work) / "build"
        out.mkdir()
        (out / "game.love").write_text("zip")
        (out / "leak.txt").symlink_to(secret)
        (out / "dir").symlink_to(tmp_path)
        return subprocess.CompletedProcess(command, 0)

    start(r, "b1")
    module.main("game", "b1", r=r, runner=sneaky)
    with zipfile.ZipFile(folder / "b1.zip") as archive:
        assert archive.namelist() == ["game.love"]
    assert secret.read_text() == "do not ship"


def test_unsupported_stacks_are_refused_before_anything_runs(build):
    module, r, repo, folder, runs, runner = build
    r.set("sid:project-type:game", json.dumps({"type": "game", "stack": "unity"}))
    start(r, "b1")
    assert module.main("game", "b1", r=r, runner=runner) == 1
    assert "Unity" in r.hgetall("sid:build:game:b1")["error"] and not runs


def test_only_the_newest_builds_are_kept(build, monkeypatch):
    module, r, repo, folder, runs, runner = build
    monkeypatch.setattr(module, "KEEP", 2)
    for build_id in ("b1", "b2", "b3"):
        start(r, build_id)
        module.main("game", build_id, r=r, runner=runner)
    assert r.lrange("sid:builds:game", 0, -1) == ["b3", "b2"]
    assert sorted(p.name for p in folder.iterdir()) == ["b2.log", "b2.zip", "b3.log", "b3.zip"]
    assert not r.hgetall("sid:build:game:b1")


def test_recipes_resolve_overrides_and_placeholders():
    recipe = project_catalog.resolve_recipe({}, {"stack": "pygame", "entry": "src/my game.py"}, "space")
    assert "--name space 'src/my game.py'" in recipe["command"] and recipe["source"] == "detected"
    custom = project_catalog.resolve_recipe({"build_command": "make dist", "build_output": "out"},
                                            {"stack": "xcode"}, "app")
    assert custom["command"] == "make dist" and "unsupported" not in custom and custom["output"] == "out"
    assert project_catalog.resolve_recipe({}, {}, "x")["source"] == "none"
    assert project_catalog.resolve_recipe({"build_command": "make"}, {}, "x")["image"] == "debian:bookworm"
    for stack, recipe in project_catalog.RECIPES.items():
        assert recipe.get("unsupported") or (recipe.get("image") and recipe.get("command") and recipe.get("output")), stack
        assert "{{" not in recipe.get("command", ""), stack
    assert "-x '.git' '.git/*'" in project_catalog.RECIPES["love2d"]["command"]


@pytest.mark.parametrize("name,ok", [("node:22-bookworm", True), ("ghcr.io/cirruslabs/flutter:stable", True),
                                     ("--privileged", False), ("a b", False), ("x;rm", False), ("", False)])
def test_image_names(name, ok):
    assert project_catalog.valid_image(name) is ok
