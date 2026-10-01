"""What kinds of projects SID knows: types, goal templates and build recipes.

Pure data, shared by the host (services/project_detect.py detects a
project's type and stack from its files) and the API / dashboard (labels,
templates, the Build button). A *type* is what the project is for (a game,
an Android app); a *stack* is how it is built (Godot, Flutter, Rust) and
decides the build recipe.

Build recipes run in the stack's official Docker image with only the
project's files mounted (services/apps -> scripts/sid-build.py), so nothing
has to be installed on the server. Some stacks cannot be built on a Linux
server at all (iOS needs Xcode on a Mac; Unity and Unreal need their
licensed editors); their recipe says so instead of offering a broken button.
"""

import re
import shlex

TYPES = {
    "website": {
        "label": "Website / web app", "icon": "🌐",
        "description": "Sites and web apps that run in a browser: static sites, React/Vue/Svelte apps, full-stack frameworks.",
        "keywords": ["website", "web app", "webapp", "landing page", "frontend", "react", "vue", "svelte", "next.js", "blog", "portfolio", "dashboard", "shop", "store"],
        "runs": True,
        "templates": [
            {"title": "Add a page", "text": "Add a new page called ___ that shows ___. Link it from the navigation."},
            {"title": "Add a form", "text": "Add a form on the ___ page that collects ___ and ___. Validate the input and show a confirmation."},
            {"title": "Fix a bug", "text": "Fix this bug: when I ___, ___ happens instead of ___."},
            {"title": "Improve speed", "text": "Make the site load faster: find the slowest pages and fix the biggest causes (large images, unneeded scripts, slow queries)."},
            {"title": "Mobile layout", "text": "Make every page work well on phones and tablets: no sideways scrolling, readable text, buttons easy to tap."},
            {"title": "Add tests", "text": "Add tests for the main user flows (___, ___) so they keep working."},
        ],
    },
    "api_service": {
        "label": "API / backend service", "icon": "🔌",
        "description": "Servers other programs talk to: REST or GraphQL APIs, webhooks, background workers.",
        "keywords": ["api", "backend", "server", "service", "rest", "graphql", "webhook", "microservice", "endpoint"],
        "runs": True,
        "templates": [
            {"title": "Add an endpoint", "text": "Add an endpoint ___ that takes ___ and returns ___. Validate input, handle errors, and add tests."},
            {"title": "Add a data model", "text": "Store ___ with fields ___, with create/read/update/delete endpoints and tests."},
            {"title": "Add authentication", "text": "Require a login (API key / user accounts) for ___ endpoints."},
            {"title": "Fix a bug", "text": "Fix this bug: calling ___ with ___ returns ___ instead of ___."},
            {"title": "Add tests", "text": "Add tests covering every endpoint's success and error cases."},
        ],
    },
    "ios_app": {
        "label": "iOS app", "icon": "📱",
        "description": "Native iPhone and iPad apps (Swift / SwiftUI / Xcode projects).",
        "keywords": ["ios", "iphone", "ipad", "swiftui", "swift app", "app store", "xcode"],
        "templates": [
            {"title": "Add a screen", "text": "Add a screen called ___ that shows ___, reachable from ___."},
            {"title": "Fix a bug", "text": "Fix this bug: on the ___ screen, when I ___, ___ happens."},
            {"title": "Talk to an API", "text": "Load ___ from ___ (an API) and show it on the ___ screen, with loading and error states."},
            {"title": "Add tests", "text": "Add unit tests for ___ and UI tests for the main flow."},
        ],
    },
    "android_app": {
        "label": "Android app", "icon": "🤖",
        "description": "Native Android apps (Kotlin / Java, Gradle).",
        "keywords": ["android", "kotlin app", "apk", "play store", "jetpack compose"],
        "templates": [
            {"title": "Add a screen", "text": "Add a screen called ___ that shows ___, reachable from ___."},
            {"title": "Fix a bug", "text": "Fix this bug: on the ___ screen, when I ___, ___ happens."},
            {"title": "Talk to an API", "text": "Load ___ from ___ (an API) and show it on the ___ screen, with loading and error states."},
            {"title": "Build an APK", "text": "Make the project build a release APK with a stable version number."},
        ],
    },
    "mobile_cross": {
        "label": "Cross-platform mobile app", "icon": "📲",
        "description": "One codebase for iOS and Android: React Native / Expo, Flutter, Ionic / Capacitor.",
        "keywords": ["react native", "expo", "flutter", "ionic", "capacitor", "mobile app", "phone app", "cross-platform app"],
        "runs": True,
        "templates": [
            {"title": "Add a screen", "text": "Add a screen called ___ that shows ___, reachable from ___."},
            {"title": "Fix a bug", "text": "Fix this bug: on the ___ screen, when I ___, ___ happens."},
            {"title": "Offline support", "text": "Keep ___ working without a connection and sync when back online."},
            {"title": "Add tests", "text": "Add tests for ___ and the main navigation flow."},
        ],
    },
    "desktop_app": {
        "label": "Desktop app", "icon": "🖥",
        "description": "Programs with windows for Windows, macOS or Linux: Electron, Tauri, Qt, .NET, GTK.",
        "keywords": ["desktop app", "electron", "tauri", "qt", "wpf", "winforms", "gtk", "windows app", "mac app"],
        "templates": [
            {"title": "Add a window", "text": "Add a ___ window that lets me ___."},
            {"title": "Add a setting", "text": "Add a setting for ___ (saved between runs) in the settings window."},
            {"title": "Fix a bug", "text": "Fix this bug: when I ___, ___ happens instead of ___."},
            {"title": "Package it", "text": "Make the project build an installable package for ___ (Windows / macOS / Linux)."},
        ],
    },
    "game": {
        "label": "Game", "icon": "🎮",
        "description": "Games in Godot, Unity, Unreal, pygame, LÖVE, Phaser and other web engines, Bevy, MonoGame, raylib.",
        "keywords": ["game", "godot", "unity", "unreal", "pygame", "love2d", "phaser", "platformer", "rpg", "shooter", "puzzle", "level", "player", "enemy", "sprite"],
        "runs": False,
        "templates": [
            {"title": "Add a mechanic", "text": "Add a ___ mechanic: when the player ___, ___ happens. Tune it so it feels ___."},
            {"title": "Add a level", "text": "Add a new level with ___ (layout, enemies, goal). It should be harder than level ___."},
            {"title": "Add an enemy", "text": "Add an enemy that ___ (movement / attack pattern) and takes ___ hits to defeat."},
            {"title": "Menus and settings", "text": "Add a start menu, a pause menu and a settings screen (volume, controls)."},
            {"title": "Balance pass", "text": "Balance ___: it is currently too ___. Adjust values so ___."},
            {"title": "Fix a bug", "text": "Fix this bug: when the player ___, ___ happens instead of ___."},
        ],
    },
    "cli_tool": {
        "label": "Command-line tool", "icon": "⌨",
        "description": "Programs you run in a terminal: scripts with options, developer tools, utilities.",
        "keywords": ["cli", "command line", "command-line", "terminal", "tool", "utility", "script"],
        "templates": [
            {"title": "Add a command", "text": "Add a `___` command that takes ___ and prints ___."},
            {"title": "Add an option", "text": "Add a `--___` option that ___ (default: ___)."},
            {"title": "Better errors", "text": "Make error messages clear: say what went wrong and how to fix it, with a non-zero exit code."},
            {"title": "Add tests", "text": "Add tests for every command, including bad input."},
        ],
    },
    "library": {
        "label": "Library / package", "icon": "📦",
        "description": "Code other projects import: npm packages, Python packages, Rust crates, Go modules.",
        "keywords": ["library", "package", "sdk", "crate", "module", "npm package", "pypi"],
        "templates": [
            {"title": "Add a function", "text": "Add `___(___)` that returns ___. Document it and add tests."},
            {"title": "Write docs", "text": "Write a README with install instructions, a quick example and the full API."},
            {"title": "Fix a bug", "text": "Fix this bug: calling ___ with ___ returns ___ instead of ___."},
            {"title": "Add tests", "text": "Raise test coverage of ___ and add edge cases for ___."},
        ],
    },
    "networking_tool": {
        "label": "Networking tool", "icon": "🛰",
        "description": "Proxies, scanners, packet tools, VPN / DNS / SSH helpers, network monitors.",
        "keywords": ["network", "networking", "proxy", "packet", "scanner", "port scan", "vpn", "dns", "ssh", "tcp", "udp", "socket", "sniffer", "firewall", "monitor", "ping", "router", "openvpn", "wireguard"],
        "runs": True,
        "templates": [
            {"title": "Add a check", "text": "Add a check that ___ (e.g. whether host ___ answers on port ___) and reports ___."},
            {"title": "Add a protocol", "text": "Support ___ (protocol) for ___."},
            {"title": "Add logging", "text": "Log ___ with timestamps to ___ and keep the last ___ days."},
            {"title": "Fix a bug", "text": "Fix this bug: when ___, the tool ___ instead of ___."},
        ],
    },
    "bot": {
        "label": "Bot", "icon": "💬",
        "description": "Chat and automation bots: Discord, Telegram, Slack, Twitch, Matrix.",
        "keywords": ["bot", "discord bot", "telegram bot", "slack bot", "twitch bot", "chatbot"],
        "runs": True,
        "templates": [
            {"title": "Add a command", "text": "Add a `/___` command that ___ and replies with ___."},
            {"title": "React to events", "text": "When ___ happens in the server, the bot should ___."},
            {"title": "Fix a bug", "text": "Fix this bug: when someone ___, the bot ___ instead of ___."},
        ],
    },
    "browser_extension": {
        "label": "Browser extension", "icon": "🧩",
        "description": "Chrome / Firefox / Edge extensions.",
        "keywords": ["extension", "chrome extension", "firefox add-on", "addon", "browser plugin"],
        "templates": [
            {"title": "Add a feature", "text": "On ___ pages, the extension should ___."},
            {"title": "Add a popup option", "text": "Add an option in the popup to ___ (saved between sessions)."},
            {"title": "Fix a bug", "text": "Fix this bug: on ___, the extension ___ instead of ___."},
        ],
    },
    "data_ml": {
        "label": "Data / machine learning", "icon": "📊",
        "description": "Analysis notebooks, data pipelines, models and training code.",
        "keywords": ["data", "analysis", "notebook", "machine learning", "ml", "model", "training", "dataset", "pandas", "pytorch", "tensorflow", "ai"],
        "templates": [
            {"title": "Analyse data", "text": "Load ___ and show ___ (summary, charts) in a notebook or report."},
            {"title": "Add a pipeline step", "text": "Add a step that cleans / transforms ___ into ___."},
            {"title": "Train a model", "text": "Train a model that predicts ___ from ___ and report its accuracy."},
        ],
    },
    "embedded_iot": {
        "label": "Embedded / IoT", "icon": "🔧",
        "description": "Firmware and devices: Arduino, ESP32, Raspberry Pi Pico, PlatformIO, Zephyr, MicroPython.",
        "keywords": ["arduino", "esp32", "esp8266", "raspberry pi", "pico", "firmware", "microcontroller", "iot", "sensor", "platformio", "micropython"],
        "templates": [
            {"title": "Read a sensor", "text": "Read ___ from the ___ sensor every ___ seconds and ___ (print / send / display)."},
            {"title": "Add Wi-Fi reporting", "text": "Send ___ readings to ___ over Wi-Fi."},
            {"title": "Fix a bug", "text": "Fix this bug: the device ___ when ___."},
        ],
    },
    "infrastructure": {
        "label": "Infrastructure / DevOps", "icon": "🏗",
        "description": "Terraform, Pulumi, Ansible, Kubernetes / Helm, Docker Compose setups.",
        "keywords": ["terraform", "ansible", "kubernetes", "k8s", "helm", "pulumi", "infrastructure", "devops", "deployment", "docker compose"],
        "templates": [
            {"title": "Add a resource", "text": "Add ___ (server / bucket / database) with ___ settings."},
            {"title": "Add monitoring", "text": "Add monitoring and alerts for ___."},
            {"title": "Harden", "text": "Review the setup for security problems and fix the important ones."},
        ],
    },
    "docs_site": {
        "label": "Documentation site", "icon": "📚",
        "description": "Docs and knowledge bases: MkDocs, Docusaurus, Hugo, Jekyll, Sphinx.",
        "keywords": ["docs", "documentation", "wiki", "knowledge base", "mkdocs", "docusaurus", "hugo", "jekyll", "sphinx"],
        "runs": True,
        "templates": [
            {"title": "Write a page", "text": "Write a page about ___ for ___ (audience), with examples."},
            {"title": "Reorganise", "text": "Reorganise the navigation so ___ is easy to find."},
            {"title": "Fix broken links", "text": "Find and fix broken links and outdated instructions."},
        ],
    },
    "other": {
        "label": "Something else", "icon": "✳",
        "description": "Anything that does not fit above; describe it and SID will treat it accordingly.",
        "keywords": [],
        "templates": [
            {"title": "Add a feature", "text": "Add ___: when ___, it should ___."},
            {"title": "Fix a bug", "text": "Fix this bug: when I ___, ___ happens instead of ___."},
            {"title": "Add tests", "text": "Add tests for ___."},
        ],
    },
}

# Build recipes per stack: Docker image, command run in /src (the project),
# and the folder or file that becomes the downloadable build. {name} is the
# project id. unsupported: why it cannot be built on this server.
RECIPES = {
    "node": {"label": "Node.js (npm run build)", "image": "node:22-bookworm",
             "command": "npm ci || npm install; npm run build", "output": "dist"},
    "vite": {"label": "Vite", "image": "node:22-bookworm", "command": "npm ci || npm install; npm run build", "output": "dist"},
    "next": {"label": "Next.js", "image": "node:22-bookworm", "command": "npm ci || npm install; npm run build", "output": ".next"},
    "static": {"label": "Static site (zip the files)", "image": "alpine:3",
               "command": "mkdir -p /tmp/out && cp -r . /tmp/out/site && rm -rf /tmp/out/site/.git && mv /tmp/out/site build-site", "output": "build-site"},
    "python_app": {"label": "Python app (PyInstaller executable)", "image": "python:3.12-bookworm",
                   "command": "pip install -q pyinstaller && (pip install -q -r requirements.txt || true) && pyinstaller --onefile --name {name} {entry}",
                   "output": "dist"},
    "python_lib": {"label": "Python package (wheel)", "image": "python:3.12-bookworm",
                   "command": "pip install -q build && python -m build", "output": "dist"},
    "rust": {"label": "Rust (cargo build --release)", "image": "rust:1-bookworm",
             "command": "cargo build --release", "output": "target/release"},
    "go": {"label": "Go (go build)", "image": "golang:1-bookworm",
           "command": "mkdir -p build && go build -o build/ ./...", "output": "build"},
    "dotnet": {"label": ".NET (dotnet publish)", "image": "mcr.microsoft.com/dotnet/sdk:8.0",
               "command": "dotnet publish -c Release -o build", "output": "build"},
    "c_make": {"label": "C / C++ (make)", "image": "gcc:14", "command": "make", "output": "."},
    "c_cmake": {"label": "C / C++ (CMake)", "image": "gcc:14",
                "command": "apt-get update -qq && apt-get install -y -qq cmake >/dev/null && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j", "output": "build"},
    "godot": {"label": "Godot (headless export)", "image": "barichello/godot-ci:4.3",
              "command": "mkdir -p build && godot --headless --export-release \"$(sed -n 's/^name=\"\\(.*\\)\"/\\1/p' export_presets.cfg | head -1)\" build/{name}",
              "output": "build", "needs": "an export preset (export_presets.cfg): create one in Godot under Project → Export"},
    "love2d": {"label": "LÖVE (.love file)", "image": "alpine:3",
               "command": "apk add -q zip && mkdir -p build && zip -9 -q -r build/{name}.love . -x '.git/*' 'build/*'", "output": "build"},
    "pygame": {"label": "pygame (PyInstaller executable)", "image": "python:3.12-bookworm",
               "command": "pip install -q pyinstaller pygame && (pip install -q -r requirements.txt || true) && pyinstaller --onefile --windowed --name {name} {entry}",
               "output": "dist"},
    "flutter": {"label": "Flutter (web + APK)", "image": "ghcr.io/cirruslabs/flutter:stable",
                "command": "flutter pub get && flutter build web && (flutter build apk --release || true) && mkdir -p sid-build && cp -r build/web sid-build/web && (cp build/app/outputs/flutter-apk/*.apk sid-build/ || true)",
                "output": "sid-build"},
    "android_gradle": {"label": "Android (Gradle APK)", "image": "mingc/android-build-box:latest",
                       "command": "chmod +x gradlew 2>/dev/null; (./gradlew assembleDebug || gradle assembleDebug) && mkdir -p sid-build && find . -name '*.apk' -path '*outputs*' -exec cp {} sid-build/ \\;",
                       "output": "sid-build", "note": "The Android build image is large (several GB) and downloads on the first build."},
    "expo": {"label": "Expo / React Native (web export)", "image": "node:22-bookworm",
             "command": "npm ci || npm install; npx expo export --platform web --output-dir sid-build", "output": "sid-build",
             "note": "Phone builds of Expo apps are made with Expo's EAS service; this builds the web version."},
    "electron": {"label": "Electron", "image": "node:22-bookworm",
                 "command": "npm ci || npm install; npm run build --if-present; npx --yes electron-builder --linux --dir || true", "output": "dist"},
    "tauri": {"label": "Tauri", "image": "rust:1-bookworm", "unsupported": "Tauri builds need the system webview libraries; build on your own machine for now."},
    "mkdocs": {"label": "MkDocs", "image": "python:3.12-bookworm", "command": "pip install -q mkdocs mkdocs-material && mkdocs build", "output": "site"},
    "hugo": {"label": "Hugo", "image": "hugomods/hugo:exts", "command": "hugo --minify", "output": "public"},
    "platformio": {"label": "PlatformIO firmware", "image": "python:3.12-bookworm",
                   "command": "pip install -q platformio && pio run && mkdir -p sid-build && find .pio/build -name 'firmware.*' -exec cp {} sid-build/ \\;", "output": "sid-build"},
    "xcode": {"label": "Xcode (iOS / macOS)", "unsupported": "iOS and macOS apps can only be built on a Mac with Xcode (Apple's rule). SID can still write and review the code."},
    "unity": {"label": "Unity", "unsupported": "Unity builds need a licensed Unity editor; build from Unity on your machine. SID can still write and review the code."},
    "unreal": {"label": "Unreal Engine", "unsupported": "Unreal builds need the Unreal editor and toolchain; build from Unreal on your machine. SID can still write and review the code."},
}


def catalog():
    """What the dashboard needs (no detection rules)."""
    return {"types": {key: {k: v for k, v in value.items() if k != "keywords"} for key, value in TYPES.items()},
            "recipes": {key: {k: v for k, v in value.items() if k in ("label", "note", "unsupported", "needs")}
                        for key, value in RECIPES.items()}}


def type_from_description(text):
    """Best type for a free-text description (keyword match), or 'other'."""
    words = f" {str(text or '').lower()} "
    best, score = "other", 0
    for key, value in TYPES.items():
        hits = sum(1 for keyword in value["keywords"] if keyword in words)
        if hits > score:
            best, score = key, hits
    return best


def resolve_recipe(fields, detection, project_id):
    """The build to run: the project's own settings (build_image /
    build_command / build_output) override the recipe of its detected stack.
    Returns {label, image, command, output, note, unsupported, source}."""
    fields = fields or {}
    detection = detection or {}
    stack = fields.get("build_stack") or detection.get("stack") or ""
    base = dict(RECIPES.get(stack, {}))
    custom = {k: fields.get(f"build_{k}") for k in ("image", "command", "output") if fields.get(f"build_{k}")}
    if not base and not custom.get("command"):
        return {"label": "No build recipe", "unsupported": "SID does not know how to build this project yet. "
                "Set a build command (and the Docker image to run it in) in Settings.", "source": "none"}
    recipe = {**base, **custom}
    if custom.get("command"):
        recipe.pop("unsupported", None)
        recipe.setdefault("image", "debian:bookworm")
        recipe.setdefault("output", ".")
        recipe["label"] = "Custom build" if not base else f"{base.get('label', stack)} (customised)"
    entry = shlex.quote(detection.get("entry") or "main.py")
    for key in ("command", "output"):
        if recipe.get(key):
            recipe[key] = recipe[key].replace("{name}", project_id).replace("{entry}", entry)
    recipe["source"] = "custom" if custom else "detected"
    recipe["stack"] = stack
    return recipe


IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9._-]{1,128})?(@sha256:[a-f0-9]{64})?")


def valid_image(name):
    """A plain Docker image reference (no options, no spaces)."""
    return bool(IMAGE_NAME.fullmatch(name or "")) and ".." not in name
