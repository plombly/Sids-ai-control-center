"""laika_projects.detect_run: what runs a project's app when its owner set nothing."""

import sys

from laika_testing import ROOT

sys.path.insert(0, str(ROOT / "services"))
import laika_projects  # noqa: E402


def files(**named):
    return lambda name: named.get(name.replace(".", "_"))


def test_start_script_procfile_and_python_servers():
    assert laika_projects.detect_run(files(package_json='{"scripts": {"start": "node server/index.js"}}')) == "npm start"
    assert laika_projects.detect_run(files(package_json='{"scripts": {"test": "x"}}')) == ""
    assert laika_projects.detect_run(files(Procfile="worker: x\nweb: gunicorn app:app -b 0.0.0.0:$PORT")) == "gunicorn app:app -b 0.0.0.0:$PORT"
    assert laika_projects.detect_run(files(manage_py="")).endswith("manage.py runserver 0.0.0.0:$PORT")
    assert laika_projects.detect_run(files(app_py="port = int(os.environ['PORT'])")).endswith(" app.py")
    assert laika_projects.detect_run(files(main_py="print('a script, not a server')")) == ""
    assert laika_projects.detect_run(files(package_json="not json")) == ""


def test_the_apps_service_uses_the_detection_unless_the_owner_chose(monkeypatch, tmp_path):
    import subprocess
    from laika_testing import load_module, MemoryRedis
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "package.json").write_text('{"scripts": {"start": "node server.js"}}')
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    apps = load_module(ROOT / "services/apps/laika_apps.py", "laika_apps_detect_test")

    class R(MemoryRedis):
        def hdel(self, key, *fields):
            for field in fields:
                self.records.get(key, {}).pop(field, None)
    monkeypatch.setattr(apps, "redis", R())
    project = type("P", (), {"id": "game", "repo": repo, "default_branch": "main", "run_command": ""})()
    assert apps.effective_run(project) == "npm start"
    assert apps.redis.records["laika:projects:game"]["detected_run_command"] == "npm start"
    project.run_command = "off"
    assert apps.effective_run(project) == ""
    project.run_command = "node other.js"
    assert apps.effective_run(project) == "node other.js"


def test_a_browser_game_with_a_ws_server_is_a_game():
    sys.path.insert(0, str(ROOT / "apps/api"))
    import project_detect
    paths = ["package.json", "server/index.js", "server/game.js", "client/index.html", "client/main.js"]
    found = project_detect.detect(paths, {"package.json": '{"dependencies": {"ws": "^8"}}'})
    assert found["type"] == "game"
