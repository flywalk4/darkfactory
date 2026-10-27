#!/bin/sh
# Host-side self-update: put the newest origin/main on this server, check that the factory is healthy, roll
# back otherwise. It runs from a systemd timer outside the containers, so a broken factory is still rolled back.
#
#   sh scripts/self-update.sh            # one round (the timer runs it every 5 minutes)
#
# Status: /data/system/self-update.json inside the data volume (shown on the factory page).
# A commit that failed its health check is remembered in bad-shas and is not installed again; the next commit
# on main is. Only fast-forwards are taken: local edits or a rewritten main stop the update with an error.
set -eu
cd "$(dirname "$0")/.."
# The merge below rewrites this very file while sh is still reading it: run from a private copy.
if [ -z "${DARKFACTORY_UPDATE_COPY:-}" ]; then
    copy=$(mktemp)
    cp "$0" "$copy"
    DARKFACTORY_UPDATE_COPY=1 DARKFACTORY_UPDATE_ROOT=$(pwd) exec sh "$copy"
fi
cd "$DARKFACTORY_UPDATE_ROOT"
trap 'rm -f "$0"' EXIT

REMOTE=${DARKFACTORY_UPDATE_REMOTE:-origin}
BRANCH=${DARKFACTORY_UPDATE_BRANCH:-main}
HEALTH_SECONDS=${DARKFACTORY_UPDATE_HEALTH_SECONDS:-600}
VOLUME=${DATA_VOLUME:-darkfactory_data}
SYSTEM="$(docker volume inspect -f '{{.Mountpoint}}' "$VOLUME")/system"
mkdir -p "$SYSTEM"
STATUS="$SYSTEM/self-update.json"
BAD="$SYSTEM/self-update-bad-shas"
LOG="$SYSTEM/self-update.log"
touch "$BAD"
PORT=$(sed -n 's/^PORT=//p' .env | tail -1)
ADDRESS=$(sed -n 's/^BIND_ADDRESS=//p' .env | tail -1)
URL="http://${ADDRESS:-127.0.0.1}:${PORT:-8080}/"

status() {
    # state, sha, previous, message
    python3 - "$STATUS" "$1" "$2" "$3" "$4" <<'EOF'
import json, sys, time
path, state, sha, previous, message = sys.argv[1:]
json.dump({"state": state, "sha": sha, "previous": previous, "message": message[-2000:], "at": time.time()},
          open(path + ".tmp", "w"))
import os
os.replace(path + ".tmp", path)
EOF
}

build() {
    # Docker Hub times out now and then; a network hiccup is not a bad version.
    for attempt in 1 2 3; do
        docker compose build -q app worker executor-image >>"$LOG" 2>&1 && return 0
        sleep 30
    done
    return 1
}

deploy() {
    docker compose up -d --no-deps app >>"$LOG" 2>&1 &&
        sh scripts/deploy-worker.sh >>"$LOG" 2>&1
}

healthy() {
    # The app answers and the new worker's scheduler ticks without failures for the whole window.
    started=$1
    deadline=$(($(date +%s) + HEALTH_SECONDS))
    good=0
    while [ "$(date +%s)" -lt "$deadline" ]; do
        sleep 15
        beat=$(cat "$SYSTEM/worker-heartbeat" 2>/dev/null || echo 0)
        now=$(date +%s)
        worker=$(docker compose ps -q worker | tail -1)
        if curl -fsS -o /dev/null --max-time 10 "$URL" &&
            [ "$beat" -ge "$started" ] && [ $((now - beat)) -lt 60 ] &&
            [ -n "$worker" ] &&
            ! docker logs --since "@$started" "$worker" 2>&1 | grep -q "Scheduler tick failed"; then
            good=$((good + 1))
            # Two minutes of a live, error-free scheduler.
            [ "$good" -ge 8 ] && return 0
        else
            good=0
        fi
    done
    return 1
}

back_to() {
    # --keep, not --hard: the checkout was clean at the start, but a hand edit made during the long build and
    # idle wait must survive. An edit to a file that differs between the two versions stops the reset instead.
    git reset -q --keep "$1"
}

git fetch -q "$REMOTE" "$BRANCH"
current=$(git rev-parse HEAD)
target=$(git rev-parse FETCH_HEAD)
dirty=$(git status --porcelain --untracked-files=no)
# Up to date, or ahead of origin after a hand deploy (a git bundle) that origin has not caught up with yet.
# A failed status left over from an earlier round no longer applies: the running version is current. Hand
# edits keep it, so they stay visible on the factory page.
if [ "$current" = "$target" ] || git merge-base --is-ancestor "$target" "$current"; then
    if [ -z "$dirty" ] && grep -q '"state": "failed"' "$STATUS" 2>/dev/null; then
        status updated "$current" "$current" "Up to date"
    fi
    exit 0
fi
if grep -qx "$target" "$BAD"; then
    exit 0
fi
if [ -n "$dirty" ] || ! git merge-base --is-ancestor "$current" "$target"; then
    status failed "$target" "$current" "Not a fast-forward or the checkout has local edits; update by hand"
    exit 1
fi

echo "$(date -Is) update $current -> $target" >>"$LOG"
status updating "$target" "$current" "Building the new version"
git merge -q --ff-only "$target"
# Build while the factory keeps working: a build restarts nothing, and a failed one (PyPI or Docker Hub
# down) used to hold all new work for every retry round.
if ! build; then
    # Nothing was restarted: go back to the running version's sources and try again next round.
    if ! back_to "$current"; then
        status failed "$target" "$current" "Build failed, and hand edits made during the update stop the return to $current; update by hand"
        exit 1
    fi
    status failed "$target" "$current" "Build failed (retried); the running version is untouched. $(tail -n 20 "$LOG")"
    exit 1
fi
status updating "$target" "$current" "Waiting for running steps, then starting the new version"
touch "$SYSTEM/hold"
# New work is held; let running agent steps finish so a restart does not cut them off mid-run.
idle_until=$(($(date +%s) + ${DARKFACTORY_UPDATE_IDLE_WAIT:-2400}))
while [ -n "$(docker ps -q --filter name=df-task-)" ] && [ "$(date +%s)" -lt "$idle_until" ]; do
    sleep 20
done
started=$(date +%s)
if deploy && rm -f "$SYSTEM/hold" && healthy "$started"; then
    status updated "$target" "$current" "Healthy after the update"
    echo "$(date -Is) healthy at $target" >>"$LOG"
    exit 0
fi

echo "$(date -Is) unhealthy at $target, rolling back to $current" >>"$LOG"
echo "$target" >>"$BAD"
reason=$(tail -n 30 "$LOG")
touch "$SYSTEM/hold"
if ! back_to "$current"; then
    rm -f "$SYSTEM/hold"
    status failed "$target" "$current" "Unhealthy, and hand edits made during the update stop the rollback to $current; needs a human. $reason"
    exit 1
fi
started=$(date +%s)
if build && deploy && rm -f "$SYSTEM/hold" && healthy "$started"; then
    status rolled_back "$target" "$current" "The new version was unhealthy; back on the previous one. $reason"
else
    rm -f "$SYSTEM/hold"
    status failed "$target" "$current" "Rollback did not come up healthy either; needs a human. $reason"
fi
exit 1
