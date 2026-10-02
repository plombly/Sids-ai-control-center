#!/usr/bin/env python3
"""Rebrand this LAIka server (deliberately not a dashboard setting).

    sudo laika branding apply    use /etc/laika/branding.json
    sudo laika branding reset    back to LAIka's own name and logo
    sudo laika branding show     what is in use

/etc/laika/branding.json:

    {
      "name": "Acme Builder",
      "wordmark": ["Acme ", "Builder"],      parts alternate plain / accent colour
      "tagline": "Our software builder",
      "logo": "/etc/laika/branding/logo.svg",      SVG or PNG, up to 512 KB
      "favicon": "/etc/laika/branding/favicon.svg"
    }

The files are checked (an SVG may not carry scripts, event handlers or
external references) and copied to /var/lib/laika/branding, which the
dashboard serves in place of its own brand files: no rebuild, no restart.
Browsers pick it up on their next page load.
"""

import json
import re
import shutil
import sys
from pathlib import Path

SOURCE = Path("/etc/laika/branding.json")
TARGET = Path("/var/lib/laika/branding")
MAX_BYTES = 512 * 1024
UNSAFE_SVG = re.compile(r"<\s*(script|foreignObject|iframe|embed|object)\b|\bon[a-z]+\s*=|javascript:|"
                        r"(?:xlink:)?href\s*=\s*['\"]\s*(?!#)|@import|url\(\s*['\"]?\s*(?:https?:|//)", re.I)


def check_image(path, label):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{label}: {path} not found")
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"{label}: larger than 512 KB")
    if path.suffix.lower() == ".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"{label}: not a PNG file")
        return data, "png"
    if path.suffix.lower() == ".svg":
        text = data.decode("utf-8", errors="replace")
        if "<svg" not in text:
            raise ValueError(f"{label}: not an SVG file")
        if UNSAFE_SVG.search(text):
            raise ValueError(f"{label}: the SVG contains scripts, event handlers or external references")
        return data, "svg"
    raise ValueError(f"{label}: use an .svg or .png file")


def build(config):
    name = str(config.get("name") or "").strip()
    if not name or len(name) > 40:
        raise ValueError("name: 1 to 40 characters")
    wordmark = config.get("wordmark") or [name]
    if not (isinstance(wordmark, list) and 0 < len(wordmark) <= 5 and all(isinstance(p, str) and len(p) <= 30 for p in wordmark)):
        raise ValueError("wordmark: a list of up to 5 short texts")
    tagline = str(config.get("tagline") or "")[:80]
    files, brand = {}, {"name": name, "wordmark": wordmark, "tagline": tagline}
    for key in ("logo", "favicon"):
        if config.get(key):
            data, kind = check_image(config[key], key)
            files[f"{key}.{kind}"] = data
            brand[key] = f"brand/{key}.{kind}"
    return brand, files


def apply(source=SOURCE, target=TARGET):
    config = json.loads(Path(source).read_text())
    brand, files = build(config)
    staging = target.with_name(target.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name, data in files.items():
        (staging / name).write_bytes(data)
    (staging / "brand.json").write_text(json.dumps(brand, indent=1) + "\n")
    for path in [staging, *staging.iterdir()]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return brand


def reset(target=TARGET):
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o755)


def main(argv):
    action = argv[1] if len(argv) > 1 else "show"
    try:
        if action == "apply":
            brand = apply()
            print(f"Branding applied: {brand['name']}. Reload the dashboard to see it.")
        elif action == "reset":
            reset()
            print("Back to LAIka's own name and logo.")
        elif action == "show":
            current = TARGET / "brand.json"
            print(current.read_text() if current.exists() else "LAIka's own name and logo.")
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (OSError, ValueError) as exc:
        print(f"Not applied: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
