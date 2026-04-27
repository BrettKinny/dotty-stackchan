#!/usr/bin/env bash
# Deploy the bridge code (top-level bridge.py + bridge/ package) from this repo
# to the host running zeroclaw-bridge. Designed for both manual ad-hoc use and
# post-commit auto-sync routines.
#
# Why a script instead of inline routine logic: the prior cloud routine pushed
# only bridge.py and missed new bridge/*.py modules — bit us with
# bridge/privacy_signal.py during the Layer 6 deploy (ModuleNotFoundError on
# restart). git ls-tree HEAD enumerates the deploy set from the repo's actual
# tracked-file list, so any newly tracked module is auto-included.
#
# Usage:
#   BRIDGE_HOST=user@host bash scripts/deploy-bridge.sh
#
# Environment overrides:
#   BRIDGE_HOST  SSH user@host running zeroclaw-bridge (required)
#   REMOTE_DIR   Bridge install dir on the host  (default: /root/zeroclaw-bridge)
#
# Requirements on the host: passwordless sudo for the SSH user.

set -euo pipefail

BRIDGE_HOST="${BRIDGE_HOST:?set BRIDGE_HOST=user@host}"
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
ssh -o BatchMode=yes -o ConnectTimeout=5 "$BRIDGE_HOST" sudo -n true \
    || { echo "ERROR: ssh+sudo preflight failed for $BRIDGE_HOST" >&2; exit 1; }

# 3. Pre-deploy snapshot on the bridge host. Keep the last 3 deploy snapshots.
#    Prune pipeline runs under `sudo sh -c` because /root/ isn't readable
#    by a non-root SSH user without sudo — bare `ls /root/...` would
#    "Permission denied" and kill the script under set -e + pipefail.
ssh "$BRIDGE_HOST" "
    set -euo pipefail
    sudo cp -a $REMOTE_DIR ${REMOTE_DIR}.bak-deploy-$TS
    sudo sh -c \"ls -1dt ${REMOTE_DIR}.bak-deploy-* 2>/dev/null | tail -n +4 | xargs -r rm -rf\" || true
"

# 4. Pack + ship via cat (avoids needing sftp-server / rsync on the host).
tar -czf "$LOCAL_TGZ" "${FILES[@]}"
cat "$LOCAL_TGZ" | ssh "$BRIDGE_HOST" "cat > /tmp/dotty-bridge.tgz"

# 5. Extract under root, chmod the deployed paths only, restart, poll until
#    the new uvicorn prints "Application startup complete" or until 30 s
#    elapses. Bridge cold-start runs face_db / face_recognizer / perception
#    consumers / piper warm-up, typically 8–15 s — a fixed sleep races it.
ssh "$BRIDGE_HOST" "
    set -euo pipefail
    sudo tar -xzf /tmp/dotty-bridge.tgz -C $REMOTE_DIR \
        --owner=root --group=root --no-same-owner
    sudo chmod -R u=rwX,go=rX $REMOTE_DIR/bridge.py $REMOTE_DIR/bridge
    rm -f /tmp/dotty-bridge.tgz
    sudo systemctl restart zeroclaw-bridge
    DEADLINE=\$((\$(date +%s) + 30))
    while [ \$(date +%s) -lt \$DEADLINE ]; do
        JOURNAL=\$(sudo journalctl -u zeroclaw-bridge --since '40 seconds ago' --no-pager)
        if echo \"\$JOURNAL\" | grep -q 'Traceback'; then
            echo 'ERROR: traceback in journal after restart' >&2
            echo \"\$JOURNAL\" | tail -40 >&2
            exit 1
        fi
        if echo \"\$JOURNAL\" | grep -q 'Application startup complete'; then
            break
        fi
        sleep 1
    done
    sudo journalctl -u zeroclaw-bridge --since '40 seconds ago' --no-pager \
        | grep -q 'Application startup complete' \
        || { echo 'ERROR: \"Application startup complete\" not seen within 30s' >&2; \
             sudo journalctl -u zeroclaw-bridge --since '40 seconds ago' --no-pager | tail -20 >&2; exit 1; }
    sudo systemctl is-active zeroclaw-bridge >/dev/null \
        || { echo 'ERROR: zeroclaw-bridge not active after startup' >&2; \
             sudo systemctl status zeroclaw-bridge --no-pager | tail -20 >&2; exit 1; }
"

# 6. md5 round-trip on the deploy set — belt-and-suspenders against silent
#    transport corruption. /root/ isn't readable by a non-root SSH user without sudo,
#    so the cd + md5sum runs under `sudo bash -c`.
LOCAL_MD5="$(md5sum "${FILES[@]}" | sort -k2)"
REMOTE_MD5="$(ssh "$BRIDGE_HOST" "sudo bash -c 'cd $REMOTE_DIR && md5sum $(printf '%q ' "${FILES[@]}")'" | sort -k2)"
if [[ "$LOCAL_MD5" != "$REMOTE_MD5" ]]; then
    echo "ERROR: md5 mismatch after deploy" >&2
    diff <(echo "$LOCAL_MD5") <(echo "$REMOTE_MD5") >&2 || true
    exit 1
fi

echo "OK — deployed ${#FILES[@]} files, service active, md5s match"
