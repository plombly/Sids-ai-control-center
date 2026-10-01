"""services/project_detect.py: what a project is, from its files."""

import json
import sys

import pytest

from sid_testing import ROOT

sys.path.insert(0, str(ROOT / "services"))
import project_detect  # noqa: E402


def pkg(deps=None, **extra):
    return json.dumps({"name": "x", "dependencies": deps or {}, **extra})


CASES = [
    ("godot game", ["project.godot", "scenes/main.tscn"], {"project.godot": "config_version=5"}, "game", "godot"),
    ("unity game", ["Assets/Scenes/a.unity", "ProjectSettings/ProjectVersion.txt"], {}, "game", "unity"),
    ("unreal game", ["Shooter.uproject", "Source/x.cpp"], {"Shooter.uproject": "{}"}, "game", "unreal"),
    ("love2d game", ["main.lua", "conf.lua"], {"conf.lua": "function love.conf(t) end"}, "game", "love2d"),
    ("phaser web game", ["package.json", "src/main.js"], {"package.json": pkg({"phaser": "3", "vite": "5"})}, "game", "vite"),
    ("pygame", ["main.py", "requirements.txt"], {"requirements.txt": "pygame==2.5\n"}, "game", "pygame"),
    ("bevy", ["Cargo.toml", "src/main.rs"], {"Cargo.toml": '[package]\nname="g"\n[dependencies]\nbevy = "0.14"\n'}, "game", "rust"),
    ("ios app", ["App.xcodeproj/project.pbxproj", "App/ContentView.swift"], {}, "ios_app", "xcode"),
    ("android app", ["app/build.gradle", "app/src/main/AndroidManifest.xml", "gradlew"],
     {"app/build.gradle": "plugins { id 'com.android.application' }"}, "android_app", "android_gradle"),
    ("react native", ["package.json", "App.tsx"], {"package.json": pkg({"react-native": "0.74", "react": "18"})}, "mobile_cross", "node"),
    ("expo", ["package.json", "app.json"], {"package.json": pkg({"expo": "51", "react-native": "0.74"})}, "mobile_cross", "expo"),
    ("flutter", ["pubspec.yaml", "lib/main.dart"], {"pubspec.yaml": "name: a\ndependencies:\n  flutter:\n    sdk: flutter\n"}, "mobile_cross", "flutter"),
    ("electron", ["package.json", "main.js"], {"package.json": pkg({"electron": "30"})}, "desktop_app", "electron"),
    ("wpf", ["App.csproj", "MainWindow.xaml"], {"App.csproj": "<Project><PropertyGroup><UseWPF>true</UseWPF></PropertyGroup></Project>"}, "desktop_app", "dotnet"),
    ("vite react site", ["package.json", "index.html", "src/App.jsx"], {"package.json": pkg({"react": "18", "vite": "5"})}, "website", "vite"),
    ("next site", ["package.json", "app/page.tsx"], {"package.json": pkg({"next": "14", "react": "18"})}, "website", "next"),
    ("static site", ["index.html", "style.css"], {}, "website", "static"),
    ("express api", ["package.json", "server.js"], {"package.json": pkg({"express": "4"})}, "api_service", "node"),
    ("fastapi", ["main.py", "requirements.txt"], {"requirements.txt": "fastapi\nuvicorn\n"}, "api_service", "python_app"),
    ("go api", ["go.mod", "main.go"], {"go.mod": "module x\nrequire github.com/gin-gonic/gin v1.9.0\n"}, "api_service", "go"),
    ("scapy tool", ["scan.py", "requirements.txt"], {"requirements.txt": "scapy\n"}, "networking_tool", "python_app"),
    ("go net tool", ["go.mod", "main.go"], {"go.mod": "module x\nrequire github.com/google/gopacket v1.1.19\n"}, "networking_tool", "go"),
    ("discord bot", ["bot.py", "requirements.txt"], {"requirements.txt": "discord.py>=2\n"}, "bot", "python_app"),
    ("discord.js bot", ["package.json", "index.js"], {"package.json": pkg({"discord.js": "14"})}, "bot", "node"),
    ("extension", ["manifest.json", "popup.html"], {"manifest.json": '{"manifest_version": 3, "name": "x"}'}, "browser_extension", "static"),
    ("notebooks", ["analysis.ipynb", "requirements.txt"], {"requirements.txt": "pandas\nmatplotlib\n"}, "data_ml", None),
    ("platformio", ["platformio.ini", "src/main.cpp"], {"platformio.ini": "[env:esp32]"}, "embedded_iot", "platformio"),
    ("arduino", ["Blink.ino"], {}, "embedded_iot", "platformio"),
    ("terraform", ["main.tf", "variables.tf"], {}, "infrastructure", None),
    ("mkdocs", ["mkdocs.yml", "docs/index.md"], {"mkdocs.yml": "site_name: x"}, "docs_site", "mkdocs"),
    ("rust cli", ["Cargo.toml", "src/main.rs"], {"Cargo.toml": '[package]\nname="t"\n[dependencies]\nclap = "4"\n'}, "cli_tool", "rust"),
    ("npm cli", ["package.json", "bin/cli.js"], {"package.json": pkg(bin={"tool": "bin/cli.js"})}, "cli_tool", "node"),
    ("rust lib", ["Cargo.toml", "src/lib.rs"], {"Cargo.toml": '[package]\nname="l"\n'}, "library", "rust"),
    ("python lib", ["pyproject.toml", "src/lib/__init__.py"], {"pyproject.toml": '[project]\nname="lib"\nversion="1"\n'}, "library", "python_lib"),
    ("raylib c game", ["CMakeLists.txt", "main.c"], {"CMakeLists.txt": "find_package(raylib)"}, "game", "c_cmake"),
]


@pytest.mark.parametrize("name,paths,texts,kind,stack", CASES, ids=[c[0] for c in CASES])
def test_detects_type_and_stack(name, paths, texts, kind, stack):
    result = project_detect.detect(paths, texts)
    assert result["type"] == kind, (name, result)
    if stack:
        assert result["stack"] == stack, (name, result)
    assert result["evidence"]


def test_empty_and_vague_projects_are_unknown():
    assert project_detect.detect([], {})["type"] == "unknown"
    assert project_detect.detect(["notes.txt"], {})["type"] == "unknown"


def test_readme_breaks_ties_lightly():
    result = project_detect.detect(["README.md", "x.py"], {"README.md": "A discord bot that posts game scores"})
    assert result["type"] == "bot"


def test_python_entry_point():
    assert project_detect.python_entry(["src/x.py", "game.py"]) == "game.py"
    assert project_detect.python_entry(["pkg/__main__.py"]) == "pkg/__main__.py"
