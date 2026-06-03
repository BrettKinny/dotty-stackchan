#!/usr/bin/env bash
# Deploy dotty-bridge (the admin dashboard) to Unraid: tar-over-ssh →
# image build → `docker compose up -d` → healthcheck against /health.
#
# Mirrors scripts/deploy-behaviour.sh — tracked-files-only deploy set,
# per-deploy backup of the previous source tree, md5 round-trip
# verification, /health poll instead of a fixed sleep.
#
# This is the post-#36 Unraid-targeted deploy for the bridge dashboard.
# The legacy scripts/deploy-bridge.sh + install-bridge.sh (which targeted
# the retired RPi at /root/zeroclaw-bridge/ + the systemd
# zeroclaw-bridge.service) have been removed. NOTE: the `dotty-deploy-bridge`
# Claude skill is also stale — it still pushes to dietpi@<ZEROCLAW_HOST> and
# restarts zeroclaw-bridge; use THIS script for bridge deploys until that
# skill is retired/rewritten.
#
# Usage:
#   BRIDGE_HOST=root@<UNRAID_HOST> bash scripts/deploy-bridge-unraid.sh
#
# Environment overrides:
#   BRIDGE_HOST   SSH user@host running Docker (required)
#   REMOTE_DIR    Source dir on host (default: /mnt/user/appdata/dotty-bridge-src)
#   IMAGE_TAG     Image tag built + run (default: dotty-bridge:0.1.0)
#   HEALTH_PORT   Port to poll /health on (default: 8081)
#
# Requires root login (or passwordless sudo) on the SSH user.

set -euo pipefail

BRIDGE_HOST="${BRIDGE_HOST:?set BRIDGE_HOST=user@host}"
REMOTE_DIR="${REMOTE_DIR:-/mnt/user/appdata/dotty-bridge-src}"
IMAGE_TAG="${IMAGE_TAG:-dotty-bridge:0.1.0}"
HEALTH_PORT="${HEALTH_PORT:-8081}"
TS="$(date +%Y%m%d-%H%M%S)"
LOCAL_TGZ="$(mktemp -t dotty-bridge.XXXXXX.tgz)"
trap 'rm -f "$LOCAL_TGZ"' EXIT

cd "$(git rev-parse --show-toplevel)"

# 1. Enumerate the deploy set from HEAD — only tracked files, so
#    __pycache__ / .venv / in-progress edits are skipped. The build
#    needs bridge.py at the repo root, the bridge/ tree (Dockerfile +
#    compose + dashboard + templates + static + requirements), and
#    custom-providers/ (bridge.py imports textUtils from there via a
#    sys.path shim).
mapfile -t FILES < <(git ls-tree -r --name-only HEAD bridge.py bridge/ custom-providers/)
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "ERROR: no tracked bridge files at HEAD" >&2
    exit 1
fi
DEPLOY_SHA="$(git rev-parse --short HEAD)"
echo "Deploy set: ${#FILES[@]} files (HEAD $DEPLOY_SHA)"

# 2. SSH preflight — fail fast on bad creds.
ssh -o BatchMode=yes -o ConnectTimeout=5 "$BRIDGE_HOST" true \
    || { echo "ERROR: ssh preflight failed for $BRIDGE_HOST" >&2; exit 1; }

# 3. Pre-deploy snapshot. Keep last 3.
ssh "$BRIDGE_HOST" "
    set -euo pipefail
    if [ -d $REMOTE_DIR ]; then
        cp -a $REMOTE_DIR ${REMOTE_DIR}.bak-deploy-$TS
        sh -c 'ls -1dt ${REMOTE_DIR}.bak-deploy-* 2>/dev/null | tail -n +4 | xargs -r rm -rf' || true
    fi
    mkdir -p $REMOTE_DIR
    mkdir -p /mnt/user/appdata/dotty-bridge/{state,logs,secrets}
"

# 4. Pack + ship via cat (no rsync dependency).
tar -czf "$LOCAL_TGZ" "${FILES[@]}"
cat "$LOCAL_TGZ" | ssh "$BRIDGE_HOST" "cat > /tmp/dotty-bridge.tgz"

# 5. Extract + build + recreate container. No --strip-components — the
#    Dockerfile copies `bridge.py`, `bridge/`, and `custom-providers/`
#    as siblings under build context root, which matches the natural
#    tar layout from `git ls-tree` (paths are already repo-relative).
#    docker compose runs from inside bridge/ so it finds the compose
#    file there; build context is `..` for the same reason.
ssh "$BRIDGE_HOST" "
    set -euo pipefail
    tar -xzf /tmp/dotty-bridge.tgz -C $REMOTE_DIR
    rm -f /tmp/dotty-bridge.tgz
    cd $REMOTE_DIR
    docker build --build-arg BRIDGE_VERSION=$DEPLOY_SHA -t $IMAGE_TAG -f bridge/Dockerfile .
    cd $REMOTE_DIR/bridge
    docker compose up -d --force-recreate
"

# 6. Healthcheck — poll /health for up to 30 s.
ssh "$BRIDGE_HOST" "
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
    docker logs --tail 40 dotty-bridge >&2 || true
    exit 1
"

# 7. md5 round-trip on the deploy set — guards against silent transport
#    corruption. No --strip-components, so the file paths under
#    REMOTE_DIR exactly match the local relative paths.
LOCAL_MD5="$(md5sum "${FILES[@]}" | sort -k2)"
REMOTE_MD5_LIST="$(printf '%q ' "${FILES[@]}")"
REMOTE_MD5="$(ssh "$BRIDGE_HOST" "cd $REMOTE_DIR && md5sum $REMOTE_MD5_LIST" | sort -k2)"
if [[ "$LOCAL_MD5" != "$REMOTE_MD5" ]]; then
    echo "ERROR: md5 mismatch after deploy" >&2
    diff <(echo "$LOCAL_MD5") <(echo "$REMOTE_MD5") >&2 || true
    exit 1
fi

echo "OK — deployed ${#FILES[@]} files, container healthy, md5s match"
