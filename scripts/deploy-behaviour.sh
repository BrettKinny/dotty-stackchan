#!/usr/bin/env bash
# Deploy dotty-behaviour to Unraid: rsync source → image build →
# `docker compose up -d` → healthcheck against /health.
#
# Models scripts/deploy-bridge.sh's defensive shape: tracked-files-only
# deploy set, per-deploy backup of the previous source tree, md5
# round-trip verification, journal/healthcheck polling instead of a
# fixed sleep.
#
# Usage:
#   BEHAVIOUR_HOST=root@<UNRAID_HOST> bash scripts/deploy-behaviour.sh
#
# Environment overrides:
#   BEHAVIOUR_HOST   SSH user@host running Docker (required)
#   REMOTE_DIR       Source dir on host (default: /mnt/user/appdata/dotty-behaviour-src)
#   IMAGE_TAG        Image tag built + run (default: dotty-behaviour:0.1.0)
#   HEALTH_PORT      Port to poll /health on (default: 8090)
#
# Requires passwordless sudo for the SSH user (or root login).

set -euo pipefail

BEHAVIOUR_HOST="${BEHAVIOUR_HOST:?set BEHAVIOUR_HOST=user@host}"
REMOTE_DIR="${REMOTE_DIR:-/mnt/user/appdata/dotty-behaviour-src}"
IMAGE_TAG="${IMAGE_TAG:-dotty-behaviour:0.1.0}"
HEALTH_PORT="${HEALTH_PORT:-8090}"
TS="$(date +%Y%m%d-%H%M%S)"
LOCAL_TGZ="$(mktemp -t dotty-behaviour.XXXXXX.tgz)"
trap 'rm -f "$LOCAL_TGZ"' EXIT

cd "$(git rev-parse --show-toplevel)"

# 1. Enumerate the deploy set from HEAD — only tracked files, so __pycache__
#    / .venv / in-progress edits are skipped.
mapfile -t FILES < <(git ls-tree -r --name-only HEAD dotty-behaviour/)
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "ERROR: no tracked dotty-behaviour files at HEAD" >&2
    exit 1
fi
echo "Deploy set: ${#FILES[@]} files (HEAD $(git rev-parse --short HEAD))"

# 2. SSH preflight — fail fast on bad creds.
ssh -o BatchMode=yes -o ConnectTimeout=5 "$BEHAVIOUR_HOST" true \
    || { echo "ERROR: ssh preflight failed for $BEHAVIOUR_HOST" >&2; exit 1; }

# 3. Pre-deploy snapshot. Keep last 3.
ssh "$BEHAVIOUR_HOST" "
    set -euo pipefail
    if [ -d $REMOTE_DIR ]; then
        cp -a $REMOTE_DIR ${REMOTE_DIR}.bak-deploy-$TS
        sh -c 'ls -1dt ${REMOTE_DIR}.bak-deploy-* 2>/dev/null | tail -n +4 | xargs -r rm -rf' || true
    fi
    mkdir -p $REMOTE_DIR
    mkdir -p /mnt/user/appdata/dotty-behaviour/{state,logs,secrets}
"

# 4. Pack + ship via cat (no rsync dependency).
tar -czf "$LOCAL_TGZ" "${FILES[@]}"
cat "$LOCAL_TGZ" | ssh "$BEHAVIOUR_HOST" "cat > /tmp/dotty-behaviour.tgz"

# 5. Extract + build + recreate container. Use --strip-components=1 so the
#    files land directly under $REMOTE_DIR (matching the Dockerfile's
#    relative paths) rather than under dotty-behaviour/.
ssh "$BEHAVIOUR_HOST" "
    set -euo pipefail
    tar -xzf /tmp/dotty-behaviour.tgz -C $REMOTE_DIR --strip-components=1
    rm -f /tmp/dotty-behaviour.tgz
    cd $REMOTE_DIR
    docker build -t $IMAGE_TAG .
    docker compose up -d --force-recreate
"

# 6. Healthcheck — poll /health for up to 30 s.
ssh "$BEHAVIOUR_HOST" "
    set -euo pipefail
    DEADLINE=\$((\$(date +%s) + 30))
    while [ \$(date +%s) -lt \$DEADLINE ]; do
        if curl -fsS http://localhost:$HEALTH_PORT/health >/dev/null 2>&1; then
            curl -s http://localhost:$HEALTH_PORT/health
            echo
            exit 0
        fi
        sleep 1
    done
    echo 'ERROR: /health never returned 2xx within 30s' >&2
    docker logs --tail 40 dotty-behaviour >&2 || true
    exit 1
"

# 7. md5 round-trip on the deploy set. --strip-components=1 means the file
#    paths under REMOTE_DIR drop the leading dotty-behaviour/ segment.
LOCAL_MD5="$(md5sum "${FILES[@]}" | sed 's|dotty-behaviour/||' | sort -k2)"
REMOTE_MD5_LIST="$(printf '%q ' "${FILES[@]/#dotty-behaviour\//}")"
REMOTE_MD5="$(ssh "$BEHAVIOUR_HOST" "cd $REMOTE_DIR && md5sum $REMOTE_MD5_LIST" | sort -k2)"
if [[ "$LOCAL_MD5" != "$REMOTE_MD5" ]]; then
    echo "ERROR: md5 mismatch after deploy" >&2
    diff <(echo "$LOCAL_MD5") <(echo "$REMOTE_MD5") >&2 || true
    exit 1
fi

echo "OK — deployed ${#FILES[@]} files, container healthy, md5s match"
