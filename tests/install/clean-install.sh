#!/bin/bash
# Clean-machine test of install.sh: a fresh distro in a throwaway systemd
# container (its own Docker inside), then checks, reinstall and uninstall.
#
#   tests/install/clean-install.sh IMAGE [SOURCE]
#   IMAGE:  ubuntu:22.04 | ubuntu:24.04 | debian:12 | fedora:42 ...
#   SOURCE: the commit to install (default HEAD of this repository)
#
# Needs Docker and internet access (packages, images). Leaves nothing
# behind: the container and its volume are removed at the end (KEEP=1 keeps
# the container for a look inside). Exit 0 only if every step passed.
set -uo pipefail

IMAGE=${1:?image, e.g. ubuntu:24.04}
REF=${2:-HEAD}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
NAME="laika-install-test-$(echo "$IMAGE" | tr -c 'a-z0-9' '-' | sed 's/-*$//')"
TAG="laika-install-base:$(echo "$IMAGE" | tr ':/' '--')"
WORK=$(mktemp -d)
LOG="${LOG_DIR:-$WORK}/$NAME.log"
failures=0

step() { printf '\n=== %s\n' "$*" | tee -a "$LOG"; }
pass() { printf 'PASS %s\n' "$*" | tee -a "$LOG"; }
bad() { printf 'FAIL %s\n' "$*" | tee -a "$LOG"; failures=$((failures + 1)); }
in_box() { docker exec "$NAME" bash -lc "$*" >>"$LOG" 2>&1; }
out_box() { docker exec "$NAME" bash -lc "$*" 2>>"$LOG"; }
cleanup() {
  if [ "${KEEP:-0}" = 1 ]; then echo "kept container $NAME"; else
    docker rm -f -v "$NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

step "base image $IMAGE with systemd"
case "$IMAGE" in
  fedora*|rocky*|alma*) PKG="dnf -y -q install systemd procps-ng iproute sudo && dnf clean all" ;;
  *) PKG="apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q systemd systemd-sysv dbus iproute2 sudo ca-certificates curl && rm -rf /var/lib/apt/lists/*" ;;
esac
cat > "$WORK/Dockerfile" <<EOF
FROM $IMAGE
RUN $PKG
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
EOF
docker build -q -t "$TAG" "$WORK" >>"$LOG" 2>&1 || { bad "base image"; exit 1; }

step "start a fresh machine"
docker rm -f -v "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --hostname laika-test --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock -v /var/lib/docker --memory=4g --cpus=2 "$TAG" >>"$LOG" 2>&1 \
  || { bad "container start"; exit 1; }
for _ in $(seq 1 30); do
  state=$(out_box "systemctl is-system-running" || true)
  case "$state" in running|degraded) break ;; esac
  sleep 2
done
pass "systemd is up ($state)"

step "copy LAIka ($REF) into the machine"
git -C "$REPO" archive --format=tar --prefix=laika-src/ "$REF" > "$WORK/src.tar"
docker cp "$WORK/src.tar" "$NAME:/root/src.tar" >>"$LOG" 2>&1
in_box "tar -xf /root/src.tar -C /root && git config --global user.email t@t && git config --global user.name t" \
  || true

step "install"
started=$(date +%s)
if in_box "apt-get update -q >/dev/null 2>&1 || true; command -v git >/dev/null || (apt-get install -y -q git >/dev/null 2>&1 || dnf -y -q install git >/dev/null 2>&1); bash /root/laika-src/install.sh --source /root/laika-src --yes"; then
  pass "install.sh finished in $(( $(date +%s) - started ))s"
else
  bad "install.sh failed (see $LOG)"; KEEP=${KEEP:-0}; exit 1
fi

step "doctor"
doctor=$(out_box "laika doctor --json" || true)
echo "$doctor" >> "$LOG"
fails=$(echo "$doctor" | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin) if c["level"] == "fail"))' 2>/dev/null || echo "unreadable")
[ -z "$fails" ] && pass "doctor: no failures" || bad "doctor failures: $fails"

step "first-run setup through the API (setup code -> admin -> sign in)"
code=$(out_box "/var/lib/laika/venv/bin/python /opt/laika/scripts/laika-admin.py setup-code" | tail -1)
api() { out_box "curl -s -m 10 -c /root/jar -b /root/jar -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' $*"; }
created=$(api "-X POST http://127.0.0.1:8080/api/setup/admin -d '{\"code\": \"$code\", \"username\": \"tester\", \"password\": \"correct horse battery\"}'")
echo "$created" | grep -q '"user"' && pass "administrator created" || bad "administrator: $created"
api "-X POST http://127.0.0.1:8080/api/auth/login -d '{\"username\": \"tester\", \"password\": \"correct horse battery\"}'" >/dev/null
state=$(api "http://127.0.0.1:8080/api/auth/state")
echo "$state" | grep -q '"signed_in":true' && pass "signed in" || bad "sign-in: $state"
projects=$(api "http://127.0.0.1:8080/api/projects")
echo "$projects" | grep -q '"laika"' && bad "production install shows the built-in project" || pass "no built-in project (production profile)"

step "a project created through the dashboard, worked on by the laika user"
api "-X POST http://127.0.0.1:8080/api/projects -d '{\"id\": \"demo\", \"name\": \"Demo\", \"source\": \"empty\", \"request_id\": \"install-test-0001\"}'" >> "$LOG"
for _ in $(seq 1 30); do
  out_box "test -d /var/lib/laika/projects/demo/repo/.git" && break
  sleep 2
done
if out_box "test -d /var/lib/laika/projects/demo/repo/.git"; then
  pass "project repository created by the operator service"
  shared=$(out_box "git -C /var/lib/laika/projects/demo/repo config core.sharedRepository")
  [ "$shared" = group ] && pass "repository shared with the laika group" || bad "core.sharedRepository=$shared"
  if in_box "cd /tmp && setpriv --reuid=laika --regid=laika --init-groups env HOME=/var/lib/laika/home GIT_CONFIG_GLOBAL=/etc/laika/gitconfig sh -c 'umask 0007; cd /var/lib/laika/projects/demo/repo && git worktree add -q -b t /var/lib/laika/worktrees/job-test && cd /var/lib/laika/worktrees/job-test && echo hi > f && git add f && git -c user.name=t -c user.email=t@t commit -qm t'"; then
    pass "the laika user can branch and commit in it"
  else
    bad "the laika user cannot work in the repository"
  fi
else
  bad "project was not created"
fi
sandbox=$(out_box "setpriv --reuid=laika --regid=laika --init-groups bwrap --die-with-parent --unshare-all --cap-drop ALL --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --tmpfs /etc/laika sh -c 'cat /etc/laika/redis.env 2>&1; id -un'" || true)
echo "$sandbox" | grep -q 'REDIS_PASSWORD' && bad "sandbox can read LAIka's secrets" || pass "sandbox runs as laika without LAIka's secrets ($sandbox)"
out_box "ps -o user= -C python | sort | uniq -c" >> "$LOG"
workers_as=$(out_box "ps -eo user=,args= | grep '[w]orker/worker.py' | awk '{print \$1}' | sort -u | tr '\n' ' '")
[ "$(echo $workers_as)" = laika ] && pass "workers run as laika" || bad "workers run as: ${workers_as:-none}"

step "repair is harmless"
in_box "laika repair --yes" && pass "laika repair" || bad "laika repair"
api "http://127.0.0.1:8080/api/auth/state" | grep -q '"signed_in":true' && pass "still signed in after repair (secrets kept)" || bad "repair changed secrets"

step "uninstall keeping data, then install again"
in_box "laika uninstall --yes" && pass "uninstall" || bad "uninstall"
out_box "test ! -e /opt/laika && test -d /var/lib/laika/projects/demo && ! systemctl list-units --plain --no-legend 'laika-*' | grep -q ." \
  && pass "program removed, data kept" || bad "uninstall left the wrong things"
in_box "bash /root/laika-src/install.sh --source /root/laika-src --yes" && pass "reinstall" || bad "reinstall"
state=$(api "-X POST http://127.0.0.1:8080/api/auth/login -d '{\"username\": \"tester\", \"password\": \"correct horse battery\"}'")
echo "$state" | grep -q '"user"' && pass "the old administrator still signs in" || bad "after reinstall: $state"

step "uninstall --purge"
in_box "laika uninstall --purge --yes" && pass "purge" || bad "purge"
out_box "test ! -e /var/lib/laika && test ! -e /etc/laika && ! id laika" && pass "nothing left" || bad "purge left files or the user"

printf '\n%s: %s (%d failures). Log: %s\n' "$IMAGE" "$([ $failures = 0 ] && echo PASSED || echo FAILED)" "$failures" "$LOG"
[ "${LOG_DIR:-}" ] || cp "$LOG" "/tmp/$NAME.log" 2>/dev/null
exit $((failures > 0))
