#!/bin/sh
# Install the Flutter SDK for Flutter projects (builders and gates use it
# through services/project_sandbox.TOOLCHAINS, each sandbox getting a
# throwaway overlay of it). Usage: scripts/install-flutter.sh [VERSION]
# (default: the current stable). Root only; installs to /opt/flutter.
set -eu
DEST=/opt/flutter
[ -e "$DEST" ] && { echo "$DEST exists; remove it first to reinstall" >&2; exit 1; }
RELEASES=https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json
INFO=$(curl -fsSL "$RELEASES" | python3 -c '
import json, sys
d = json.load(sys.stdin); want = sys.argv[1] if len(sys.argv) > 1 else ""
h = d["current_release"]["stable"]
r = next(x for x in d["releases"] if (x["version"] == want and x["channel"] == "stable") or (not want and x["hash"] == h))
print(d["base_url"] + "/" + r["archive"], r["sha256"], r["version"])' "${1:-}")
set -- $INFO
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading Flutter $3"
curl -fsSL -o "$TMP/flutter.tar.xz" "$1"
echo "$2  $TMP/flutter.tar.xz" | sha256sum -c -
tar -xJf "$TMP/flutter.tar.xz" -C /opt
chown -R root:root "$DEST"   # git refuses SDK checkouts owned by someone else
# The SDK's own tool packages live inside the SDK, so sandboxes (which hide
# /root) and per-project PUB_CACHEs still find them offline.
export PATH="$DEST/bin:$PATH" PUB_CACHE="$DEST/.pub-cache"
flutter --disable-analytics >/dev/null 2>&1 || true
dart --disable-analytics >/dev/null 2>&1 || true
flutter precache --universal
flutter --version
