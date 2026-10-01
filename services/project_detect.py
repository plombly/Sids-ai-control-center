"""Work out what a project is (its type) and how it is built (its stack).

Reads the files committed on main (no checkout needed): manifests and their
dependencies, engine and IDE project files, and a little of the README.
Every matching rule adds points to a type and suggests a stack; the type
with the most points wins. Returns the evidence too, so the dashboard can
say why ("package.json depends on react-native"). An empty or unknown
project comes back as "unknown"; the services/apps loop re-checks whenever
main moves, so the type appears once the builder adds real files.
"""

import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
import project_catalog  # noqa: E402  (apps/api/project_catalog.py)

MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml", "go.mod", "pubspec.yaml",
             "manifest.json", "project.godot", "platformio.ini", "mkdocs.yml", "CMakeLists.txt", "Makefile",
             "build.gradle", "build.gradle.kts", "app/build.gradle", "app/build.gradle.kts", "Package.swift",
             "app.json", "conf.lua", "Chart.yaml", "Pulumi.yaml", "ansible.cfg", "docusaurus.config.js",
             "docusaurus.config.ts", "hugo.toml", "config.toml", "_config.yml", "README.md", "readme.md", "README")
MAX_FILES = 20000


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def read_tree(repo, ref="refs/heads/main"):
    """(paths, {manifest path: text}) of what is committed on ref."""
    paths = [p for p in _git(repo, "ls-tree", "-r", "--name-only", ref).splitlines() if p][:MAX_FILES]
    present = set(paths)
    texts = {}
    wanted = [m for m in MANIFESTS if m in present]
    wanted += [p for p in paths if p.endswith((".csproj", ".uproject")) or PurePosixPath(p).name == "AndroidManifest.xml"][:5]
    for path in wanted:
        texts[path] = _git(repo, "show", f"{ref}:{path}")[:200_000]
    return paths, texts


class Score:
    def __init__(self):
        self.points, self.stacks, self.evidence = {}, {}, []

    def add(self, kind, points, why, stack=None):
        self.points[kind] = self.points.get(kind, 0) + points
        if stack and points >= self.stacks.get(kind, ("", 0))[1]:
            self.stacks[kind] = (stack, points)
        self.evidence.append({"type": kind, "points": points, "why": why})


def _deps_package_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return {}, {}
    deps = {}
    for field in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(data.get(field), dict):
            deps.update(data[field])
    return data if isinstance(data, dict) else {}, deps


def _python_deps(texts):
    blob = (texts.get("requirements.txt", "") + "\n" + texts.get("pyproject.toml", "") + "\n" + texts.get("setup.py", "")).lower()
    return set(re.findall(r"[a-z0-9][a-z0-9_.\-]*", blob))


def _toml(text):
    if not tomllib or not text:
        return {}
    try:
        return tomllib.loads(text)
    except Exception:
        return {}


def python_entry(paths):
    for candidate in ("main.py", "app.py", "run.py", "game.py", "bot.py", "cli.py", "__main__.py", "src/main.py"):
        if candidate in paths:
            return candidate
    mains = [p for p in paths if p.endswith("/__main__.py")]
    return mains[0] if mains else "main.py"


def detect(paths, texts):
    """{type, stack, confidence, evidence, candidates, entry}."""
    score = Score()
    names = {PurePosixPath(p).name for p in paths}
    tops = {p.split("/", 1)[0] for p in paths}
    has = lambda name: name in names
    exts = {PurePosixPath(p).suffix.lower() for p in paths}

    # --- engines and IDE projects (very strong) ---------------------------------------
    if has("project.godot"):
        score.add("game", 10, "project.godot (a Godot project)", "godot")
    if any(p.endswith(".uproject") for p in paths):
        score.add("game", 10, "an .uproject file (Unreal Engine)", "unreal")
    if "ProjectSettings/ProjectVersion.txt" in paths or ("Assets" in tops and "ProjectSettings" in tops):
        score.add("game", 10, "Assets/ and ProjectSettings/ (a Unity project)", "unity")
    if has("conf.lua") and has("main.lua"):
        score.add("game", 9, "main.lua and conf.lua (a LÖVE game)", "love2d")
    if any(".xcodeproj/" in p or ".xcworkspace/" in p for p in paths):
        score.add("ios_app", 8, "an Xcode project", "xcode")
    if "Package.swift" in texts and ".iOS" in texts["Package.swift"]:
        score.add("ios_app", 6, "Package.swift targets iOS", "xcode")
    if has("Podfile"):
        score.add("ios_app", 3, "a CocoaPods Podfile", "xcode")
    gradle = " ".join(texts.get(k, "") for k in ("build.gradle", "build.gradle.kts", "app/build.gradle", "app/build.gradle.kts"))
    if "com.android.application" in gradle or has("AndroidManifest.xml"):
        score.add("android_app", 9, "an Android Gradle app (com.android.application / AndroidManifest.xml)", "android_gradle")
    if has("platformio.ini"):
        score.add("embedded_iot", 10, "platformio.ini", "platformio")
    if ".ino" in exts:
        score.add("embedded_iot", 8, "Arduino sketches (.ino)", "platformio")
    if has("sdkconfig") or has("prj.conf"):
        score.add("embedded_iot", 6, "ESP-IDF / Zephyr configuration")
    manifest = texts.get("manifest.json", "")
    if '"manifest_version"' in manifest:
        score.add("browser_extension", 10, "manifest.json with manifest_version (a browser extension)", "static")

    # --- JavaScript / TypeScript ------------------------------------------------------
    if "package.json" in texts:
        data, deps = _deps_package_json(texts["package.json"])
        dep = lambda *keys: [k for k in keys if k in deps]
        if found := dep("react-native", "expo"):
            score.add("mobile_cross", 9, f"package.json depends on {found[0]}", "expo" if "expo" in found else "node")
        if found := dep("@capacitor/core", "@ionic/angular", "@ionic/react", "@ionic/vue", "cordova"):
            score.add("mobile_cross", 8, f"package.json depends on {found[0]}", "node")
        if dep("electron"):
            score.add("desktop_app", 9, "package.json depends on electron", "electron")
        if dep("@tauri-apps/api", "@tauri-apps/cli") or "src-tauri" in tops:
            score.add("desktop_app", 9, "a Tauri app", "tauri")
        if found := dep("phaser", "pixi.js", "kaboom", "kaplay", "excalibur", "@babylonjs/core", "melonjs", "littlejsengine"):
            score.add("game", 8, f"package.json depends on {found[0]} (a game engine)", "vite" if dep("vite") else "node")
        if dep("three") and any(word in json.dumps(data).lower() for word in ("game", "player")):
            score.add("game", 4, "three.js with game wording", "vite" if dep("vite") else "node")
        if found := dep("discord.js", "telegraf", "node-telegram-bot-api", "@slack/bolt", "tmi.js", "grammy"):
            score.add("bot", 9, f"package.json depends on {found[0]}", "node")
        if found := dep("@docusaurus/core", "vitepress"):
            score.add("docs_site", 8, f"package.json depends on {found[0]}", "node")
        if dep("next"):
            score.add("website", 7, "a Next.js app", "next")
        if found := dep("react", "vue", "svelte", "@sveltejs/kit", "astro", "nuxt", "@angular/core", "gatsby", "solid-js", "preact"):
            score.add("website", 5, f"package.json depends on {found[0]}", "vite" if dep("vite") else "node")
        if found := dep("express", "fastify", "koa", "@nestjs/core", "@hapi/hapi", "hono", "apollo-server", "@apollo/server"):
            score.add("api_service", 6, f"package.json depends on {found[0]}", "node")
        if found := dep("ws", "socket.io", "node-pcap", "net-snmp", "dns2", "http-proxy"):
            score.add("networking_tool", 3, f"package.json depends on {found[0]}", "node")
        if isinstance(data.get("bin"), (str, dict)):
            score.add("cli_tool", 6, "package.json has a bin (a command-line tool)", "node")
        if (data.get("main") or data.get("exports")) and not data.get("private") and not score.points:
            score.add("library", 4, "an npm package with main/exports", "node")
        if not score.points and isinstance(data.get("scripts"), dict) and "build" in data["scripts"]:
            score.add("website", 2, "an npm project with a build script", "node")

    # --- Python -----------------------------------------------------------------------
    if any(k in texts for k in ("pyproject.toml", "requirements.txt", "setup.py")) or ".py" in exts:
        deps = _python_deps(texts)
        pyproject = _toml(texts.get("pyproject.toml", ""))
        pick = lambda *keys: [k for k in keys if k in deps]
        if found := pick("pygame", "pygame-ce", "arcade", "pyglet", "ursina", "panda3d"):
            score.add("game", 8, f"Python depends on {found[0]}", "pygame")
        if found := pick("discord.py", "discord", "py-cord", "nextcord", "python-telegram-bot", "aiogram", "slack_bolt", "slack-bolt", "twitchio"):
            score.add("bot", 8, f"Python depends on {found[0]}", "python_app")
        if found := pick("scapy", "pyshark", "netmiko", "paramiko", "dnspython", "pysnmp", "python-nmap", "impacket", "netaddr", "mitmproxy", "pyroute2"):
            score.add("networking_tool", 7, f"Python depends on {found[0]}", "python_app")
        if found := pick("fastapi", "flask", "aiohttp", "starlette", "sanic", "tornado", "litestar", "falcon"):
            score.add("api_service", 6, f"Python depends on {found[0]}", "python_app")
        if pick("django"):
            score.add("website", 6, "a Django project", "python_app")
        if found := pick("streamlit", "gradio", "dash", "nicegui"):
            score.add("website", 4, f"Python depends on {found[0]} (a web UI)", "python_app")
            score.add("data_ml", 2, f"{found[0]} is common for data apps")
        if found := pick("pandas", "numpy", "scikit-learn", "sklearn", "torch", "tensorflow", "keras", "jupyter", "matplotlib", "polars", "xgboost"):
            score.add("data_ml", 4, f"Python depends on {found[0]}")
        if ".ipynb" in exts:
            score.add("data_ml", 6, "Jupyter notebooks")
        if found := pick("click", "typer", "fire", "docopt", "rich-click"):
            score.add("cli_tool", 4, f"Python depends on {found[0]}", "python_app")
        if (pyproject.get("project") or {}).get("scripts"):
            score.add("cli_tool", 5, "pyproject defines command-line scripts", "python_app")
        if any("import machine" in texts.get(f, "") for f in ("main.py", "boot.py")) or has("boot.py"):
            score.add("embedded_iot", 6, "MicroPython boot.py / machine module")
        if pyproject.get("project") and not score.points:
            score.add("library", 5, "a Python package (pyproject [project])", "python_lib")
        if not score.points:
            score.add("cli_tool", 2, "Python scripts", "python_app")

    # --- Rust / Go / Dart / .NET / C ---------------------------------------------------
    cargo = _toml(texts.get("Cargo.toml", ""))
    if cargo:
        crates = set((cargo.get("dependencies") or {}).keys())
        if found := sorted(crates & {"bevy", "macroquad", "ggez", "fyrox", "piston", "raylib", "comfy"}):
            score.add("game", 9, f"Cargo depends on {found[0]}", "rust")
        if "tauri" in crates:
            score.add("desktop_app", 8, "Cargo depends on tauri", "tauri")
        if found := sorted(crates & {"axum", "actix-web", "rocket", "warp", "poem", "tide"}):
            score.add("api_service", 7, f"Cargo depends on {found[0]}", "rust")
        if found := sorted(crates & {"pnet", "trust-dns-resolver", "hickory-resolver", "pcap", "smoltcp", "rustls", "quinn"}):
            score.add("networking_tool", 6, f"Cargo depends on {found[0]}", "rust")
        if found := sorted(crates & {"embedded-hal", "cortex-m", "esp-hal", "rp2040-hal", "embassy-executor"}):
            score.add("embedded_iot", 8, f"Cargo depends on {found[0]}")
        if "clap" in crates or "src/main.rs" in paths:
            score.add("cli_tool", 4 if "clap" in crates else 2, "a Rust binary" + (" using clap" if "clap" in crates else ""), "rust")
        if "src/lib.rs" in paths and "src/main.rs" not in paths:
            score.add("library", 5, "a Rust library crate (src/lib.rs)", "rust")
    gomod = texts.get("go.mod", "")
    if gomod:
        mods = gomod.lower()
        if any(m in mods for m in ("gin-gonic", "labstack/echo", "gofiber", "go-chi", "gorilla/mux")):
            score.add("api_service", 7, "a Go web framework in go.mod", "go")
        if any(m in mods for m in ("gopacket", "miekg/dns", "golang.org/x/net", "wireguard", "vishvananda/netlink")):
            score.add("networking_tool", 6, "Go networking packages in go.mod", "go")
        if any(m in mods for m in ("spf13/cobra", "urfave/cli")):
            score.add("cli_tool", 6, "a Go command-line framework", "go")
        if any(m in mods for m in ("ebiten", "raylib-go", "pixel")):
            score.add("game", 8, "a Go game library", "go")
        if not any(p == "main.go" or p.endswith("/main.go") for p in paths):
            score.add("library", 3, "a Go module without a main package", "go")
        else:
            score.add("cli_tool", 2, "a Go program", "go")
    pubspec = texts.get("pubspec.yaml", "")
    if pubspec:
        if "flutter:" in pubspec:
            score.add("mobile_cross", 9, "a Flutter app (pubspec.yaml)", "flutter")
        else:
            score.add("cli_tool", 3, "a Dart package")
    for path, text in texts.items():
        if not path.endswith(".csproj"):
            continue
        if "Microsoft.NET.Sdk.Web" in text:
            score.add("api_service", 7, f"{path} is an ASP.NET project", "dotnet")
        elif any(k in text for k in ("<UseWPF>", "<UseWindowsForms>", "Avalonia")):
            score.add("desktop_app", 8, f"{path} is a desktop app", "dotnet")
        elif "Maui" in text:
            score.add("mobile_cross", 8, f"{path} is a .NET MAUI app", "dotnet")
        elif "MonoGame" in text:
            score.add("game", 8, f"{path} uses MonoGame", "dotnet")
        else:
            score.add("cli_tool", 3, f"{path} is a .NET program", "dotnet")
    cmake = texts.get("CMakeLists.txt", "")
    if cmake:
        if re.search(r"raylib|SDL2|SFML|glfw", cmake, re.I):
            score.add("game", 6, "CMake links a game library (raylib / SDL / SFML)", "c_cmake")
        else:
            score.add("cli_tool", 2, "a CMake project", "c_cmake")
    if "Makefile" in texts and {".c", ".cpp", ".cc"} & exts and not cmake:
        score.add("cli_tool", 2, "a C / C++ project with a Makefile", "c_make")

    # --- docs, infrastructure, static sites ---------------------------------------------
    if has("mkdocs.yml"):
        score.add("docs_site", 9, "mkdocs.yml", "mkdocs")
    if any(k in texts for k in ("hugo.toml",)) or ("config.toml" in texts and "baseURL" in texts["config.toml"]):
        score.add("website", 6, "a Hugo site", "hugo")
    if "_config.yml" in texts and "_posts" in tops:
        score.add("website", 6, "a Jekyll site", "static")
    if "docs/conf.py" in paths or "conf.py" in paths and "sphinx" in texts.get("requirements.txt", "").lower():
        score.add("docs_site", 6, "a Sphinx configuration")
    if ".tf" in exts:
        score.add("infrastructure", 9, "Terraform files (.tf)")
    if has("Chart.yaml") or has("Pulumi.yaml") or has("ansible.cfg") or any(p.endswith(("playbook.yml", "playbook.yaml")) for p in paths):
        score.add("infrastructure", 8, "Helm / Pulumi / Ansible files")
    if has("index.html") and not score.points:
        score.add("website", 4, "an index.html (a static site)", "static")

    # --- the README's words (light) -----------------------------------------------------
    readme = next((texts[k] for k in ("README.md", "readme.md", "README") if k in texts), "")
    if readme:
        guess = project_catalog.type_from_description(readme[:3000])
        if guess != "other":
            score.add(guess, 3, "the README describes it")

    if not score.points:
        return {"type": "unknown", "stack": "", "confidence": 0.0, "evidence": [], "candidates": [],
                "entry": python_entry(paths)}
    ranked = sorted(score.points.items(), key=lambda item: -item[1])
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top < 3:
        best = "unknown"
    confidence = round(top / (top + second), 2) if top else 0.0
    return {"type": best, "stack": score.stacks.get(ranked[0][0], ("", 0))[0], "confidence": confidence,
            "evidence": [e["why"] for e in score.evidence if e["type"] == ranked[0][0]][:6],
            "candidates": [{"type": kind, "points": points} for kind, points in ranked[:4]],
            "entry": python_entry(paths)}


def detect_repo(repo, ref="refs/heads/main"):
    paths, texts = read_tree(repo, ref)
    return detect(paths, texts)
