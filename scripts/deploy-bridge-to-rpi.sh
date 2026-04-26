#!/usr/bin/env bash
# Deploy the bridge code (top-level bridge.py + bridge/ package) from this repo
# to the RPi running zeroclaw-bridge. Designed for both manual ad-hoc use and
# the cloud post-commit auto-sync routine.
#
# Why a script instead of inline routine logic: the prior cloud routine pushed
# only bridge.py and missed new bridge/*.py modules — bit us with
# bridge/privacy_signal.py during the Layer 6 deploy (ModuleNotFoundError on
# restart). git ls-tree HEAD enumerates the deploy set from the repo's actual
# tracked-file list, so any newly tracked module is auto-included.
#
# Usage:
#   bash scripts/deploy-bridge-to-rpi.sh
#
# Environment overrides:
#   RPI_HOST    SSH user@host of the RPi   (default: dietpi@192.168.1.54)
#   REMOTE_DIR  Bridge install dir on RPi  (default: /root/zeroclaw-bridge)
#
# Requirements on the RPi: passwordless sudo for the SSH user (DietPi default).

set -euo pipefail

RPI_HOST="${RPI_HOST:-dietpi@192.168.1.54}"
REMOTE_DIR="${REMOTE_DIR:-/root/zeroclaw-bridge}"
TS="$(date +%Y%m%d-%H%M%S)"
LOCAL_TGZ="$(mktemp -t dotty-bridge.XXXXXX.tgz)"
trap 'rm -f "$LOCAL_TGZ"' EXIT

cd "$(git rev-parse --show-toplevel)"

# 1. Enumerate deploy set from HEAD (tracked-only — auto-includes new modules,
#    skips __pycache__ / .venv / in-progress edits).
mapfile -t FILES < <(git ls-tree -r --name-only HEAD bridge.py bridge/)
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "ERROR: no tracked bridge files found at HEAD" >&2
    exit 1
fi
echo "Deploy set: ${#FILES[@]} files (HEAD $(git rev-parse --short HEAD))"

# 2. SSH preflight — fail fast if creds / sudo broken.
ssh -o BatchMode=yes -o ConnectTimeout=5 "$RPI_HOST" sudo -n true \
    || { echo "ERROR: ssh+sudo preflight failed for $RPI_HOST" >&2; exit 1; }

# 3. Pre-deploy snapshot on the RPi. Keep the last 3 deploy snapshots.
ssh "$RPI_HOST" "
    set -euo pipefail
    sudo cp -a $REMOTE_DIR ${REMOTE_DIR}.bak-deploy-$TS
    ls -1dt ${REMOTE_DIR}.bak-deploy-* 2>/dev/null | tail -n +4 | xargs -r sudo rm -rf
"

# 4. Pack + ship via cat (DietPi has no sftp-server / rsync server).
tar -czf "$LOCAL_TGZ" "${FILES[@]}"
cat "$LOCAL_TGZ" | ssh "$RPI_HOST" "cat > /tmp/dotty-bridge.tgz"

# 5. Extract under root, chmod the deployed paths only, restart, verify clean.
ssh "$RPI_HOST" "
    set -euo pipefail
    sudo tar -xzf /tmp/dotty-bridge.tgz -C $REMOTE_DIR \
        --owner=root --group=root --no-same-owner
    sudo chmod -R u=rwX,go=rX $REMOTE_DIR/bridge.py $REMOTE_DIR/bridge
    rm -f /tmp/dotty-bridge.tgz
    sudo systemctl restart zeroclaw-bridge
    sleep 5
    sudo systemctl is-active zeroclaw-bridge >/dev/null \
        || { echo 'ERROR: zeroclaw-bridge not active after restart' >&2; \
             sudo systemctl status zeroclaw-bridge --no-pager | tail -20 >&2; exit 1; }
    JOURNAL=\$(sudo journalctl -u zeroclaw-bridge --since '30 seconds ago' --no-pager)
    if echo \"\$JOURNAL\" | grep -q 'Traceback'; then
        echo 'ERROR: traceback in journal after restart' >&2
        echo \"\$JOURNAL\" | tail -40 >&2
        exit 1
    fi
    echo \"\$JOURNAL\" | grep -q 'Application startup complete' \
        || { echo 'ERROR: \"Application startup complete\" not found in journal' >&2; \
             echo \"\$JOURNAL\" | tail -20 >&2; exit 1; }
"

# 6. md5 round-trip on the deploy set — belt-and-suspenders against silent
#    transport corruption. Each side computes md5sum of the same path list.
LOCAL_MD5="$(md5sum "${FILES[@]}" | sort -k2)"
REMOTE_MD5="$(ssh "$RPI_HOST" "cd $REMOTE_DIR && sudo md5sum $(printf '%q ' "${FILES[@]}")" | sort -k2)"
if [[ "$LOCAL_MD5" != "$REMOTE_MD5" ]]; then
    echo "ERROR: md5 mismatch after deploy" >&2
    diff <(echo "$LOCAL_MD5") <(echo "$REMOTE_MD5") >&2 || true
    exit 1
fi

echo "OK — deployed ${#FILES[@]} files, service active, md5s match"
