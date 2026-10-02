#!/bin/bash
# End-to-end test of a one-click update on a fresh machine: install this
# commit, publish a signed newer release on a local web server inside the
# machine, run `laika update`, and check the result; then a tampered
# release must be refused without changing anything.
#
#   RELEASE_KEY=/path/to/release_ed25519 tests/install/update-test.sh [IMAGE]
#
# RELEASE_KEY must be the private half of deploy/release-key.pub.
set -uo pipefail

IMAGE=${1:-debian:12}
KEY=${RELEASE_KEY:?RELEASE_KEY: the release signing key}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
NAME="laika-update-test"
TAG="laika-install-base:$(echo "$IMAGE" | tr ':/' '--')"
WORK=$(mktemp -d)
LOG="${LOG_DIR:-$WORK}/$NAME.log"
failures=0
pass() { printf 'PASS %s\n' "$*" | tee -a "$LOG"; }
bad() { printf 'FAIL %s\n' "$*" | tee -a "$LOG"; failures=$((failures + 1)); }
in_box() { docker exec "$NAME" bash -lc "$*" >>"$LOG" 2>&1; }
out_box() { docker exec "$NAME" bash -lc "$*" 2>>"$LOG"; }
cleanup() { [ "${KEEP:-0}" = 1 ] || docker rm -f -v "$NAME" >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT

case "$IMAGE" in
  fedora*) PKG="dnf -y -q install systemd procps-ng iproute sudo && dnf clean all" ;;
  *) PKG="apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q systemd systemd-sysv dbus iproute2 sudo ca-certificates curl python3 && rm -rf /var/lib/apt/lists/*" ;;
esac
printf 'FROM %s\nRUN %s\nSTOPSIGNAL SIGRTMIN+3\nCMD ["/sbin/init"]\n' "$IMAGE" "$PKG" > "$WORK/Dockerfile"
docker build -q -t "$TAG" "$WORK" >>"$LOG" 2>&1 || { bad "base image"; exit 1; }
docker rm -f -v "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --hostname laika-test --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
  -v /var/lib/docker -v /var/lib/containerd --memory=4g --cpus=2 "$TAG" >>"$LOG" 2>&1 || { bad "container"; exit 1; }
for _ in $(seq 1 30); do case "$(out_box 'systemctl is-system-running' || true)" in running|degraded) break ;; esac; sleep 2; done

# The installed version (HEAD) and a newer release made from the same code.
git -C "$REPO" archive --format=tar --prefix=laika-src/ HEAD > "$WORK/src.tar"
docker cp "$WORK/src.tar" "$NAME:/root/src.tar" >>"$LOG" 2>&1
in_box "tar -xf /root/src.tar -C /root"
in_box "command -v git >/dev/null || (apt-get update -q >/dev/null 2>&1; apt-get install -y -q git >/dev/null 2>&1 || dnf -y -q install git >/dev/null 2>&1); bash /root/laika-src/install.sh --source /root/laika-src --yes" \
  && pass "installed $(out_box 'cat /opt/laika/VERSION')" || { bad "install"; exit 1; }

mkdir -p "$WORK/release/laika-9.9.9"
tar -xf "$WORK/src.tar" -C "$WORK/release/laika-9.9.9" --strip-components=1
echo "9.9.9" > "$WORK/release/laika-9.9.9/VERSION"
tar -czf "$WORK/release/laika-9.9.9.tar.gz" -C "$WORK/release" laika-9.9.9
rm -rf "$WORK/release/laika-9.9.9"
sha=$(sha256sum "$WORK/release/laika-9.9.9.tar.gz" | cut -d' ' -f1)
printf '{"version": "9.9.9", "tarball": "laika-9.9.9.tar.gz", "sha256": "%s", "notes": "test release"}\n' "$sha" > "$WORK/release/manifest.json"
ssh-keygen -q -Y sign -f "$KEY" -n laika-release "$WORK/release/manifest.json" || { bad "signing"; exit 1; }
docker cp "$WORK/release" "$NAME:/root/release" >>"$LOG" 2>&1
in_box "cd /root/release && nohup python3 -m http.server 8099 --bind 127.0.0.1 >/dev/null 2>&1 &"
sleep 2
url=http://127.0.0.1:8099/

check=$(out_box "UPDATE_URL=$url laika update --check")
echo "$check" >> "$LOG"
echo "$check" | grep -q '"newer": true' && pass "laika update --check sees 9.9.9" || bad "check: $check"

# A tampered release is refused and changes nothing.
in_box "cp /root/release/manifest.json /root/manifest.good && sed -i 's/test release/tampered/' /root/release/manifest.json"
out_box "UPDATE_URL=$url laika update --yes" >/dev/null 2>&1
[ "$(out_box 'cat /opt/laika/VERSION')" != 9.9.9 ] && pass "tampered manifest refused, nothing changed" || bad "tampered release was installed"
in_box "cp /root/manifest.good /root/release/manifest.json"

# The real update.
started=$(date +%s)
in_box "UPDATE_URL=$url laika update --yes" && pass "update finished in $(( $(date +%s) - started ))s" || bad "update failed"
[ "$(out_box 'cat /opt/laika/VERSION')" = 9.9.9 ] && pass "now running 9.9.9" || bad "version after update: $(out_box 'cat /opt/laika/VERSION')"
out_box "test -f /opt/laika.previous/VERSION" && pass "previous version kept for rollback" || bad "no /opt/laika.previous"
state=$(out_box "/var/lib/laika/venv/bin/python -c \"import sys; sys.path.insert(0,'/opt/laika/services'); import laika_redis, redis; print(redis.Redis(password=laika_redis.password(), decode_responses=True).get('laika:update:status'))\"")
echo "$state" | grep -q '"state": "done"' && pass "status recorded: done" || bad "status: $state"
doctor=$(out_box "laika doctor --json" || true)
fails=$(echo "$doctor" | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin) if c["level"] == "fail"))' 2>/dev/null || echo unreadable)
[ -z "$fails" ] && pass "doctor: no failures after the update" || bad "doctor after update: $fails"
out_box "ls /etc/laika/redis.env /etc/laika/compose.env >/dev/null" && pass "secrets kept" || bad "secrets lost"

printf '\nupdate test on %s: %s (%d failures). Log: %s\n' "$IMAGE" "$([ $failures = 0 ] && echo PASSED || echo FAILED)" "$failures" "$LOG"
exit $((failures > 0))
