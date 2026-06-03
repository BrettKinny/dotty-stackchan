#!/usr/bin/env bash
# Deploy dotty-pi (the pi voice-tool brain) to Unraid: ship build context +
# config + extension source → build the pinned image → `docker compose up -d`
# → healthcheck via `docker exec`.
#
# Mirrors scripts/deploy-behaviour.sh's defensive shape — tracked-files-only
# deploy set, per-deploy backup of the whole state tree, md5 round-trip
# verification, and a non-HTTP healthcheck (dotty-pi has no /health; it's a
# `sleep infinity` container driven by `docker exec`).
#
# dotty-pi is unlike the other two services in three ways, all handled below:
#   1. THREE destinations, not one:
#        - build context (Dockerfile + docker-compose.yml) → $SRC_DIR
#        - config (models.json)                            → $STATE_DIR/agent/
#        - extension source (dotty-pi-ext/)                → $STATE_DIR/extensions/dotty-pi-ext/
#   2. The extension carries a hand-compiled native module (better-sqlite3) in
#      node_modules/ that is NOT in git. We refresh the SOURCE and PRESERVE the
#      existing node_modules — deps are unchanged and node stays the same major
#      (same C++ ABI), so no `npm ci`/native rebuild inside Alpine is needed.
#   3. No HTTP healthcheck — we exec `pi --version` and assert the extension
#      tool files landed. A functional voice-tool smoke test is a MANUAL
#      post-deploy step (see end of script): a real RPC turn must target
#      qwen3.5:4b only — qwen3.6:27b evicts the resident voice pair (a 30-50s
#      cold reload), per dotty-pi/README.md.
#
# HARD GUARD: this script writes ONLY to $SRC_DIR, $STATE_DIR/agent/models.json,
# and $STATE_DIR/extensions/dotty-pi-ext/. It NEVER touches $STATE_DIR/memory/
# (the live brain.db), $STATE_DIR/persona/, $STATE_DIR/agent/auth.json, or
# $STATE_DIR/agent/sessions/ — those are live migrated state.
#
# Usage:
#   DOTTY_PI_HOST=root@<UNRAID_HOST> bash scripts/deploy-dotty-pi.sh
#
# Environment overrides:
#   DOTTY_PI_HOST   SSH user@host running Docker (required)
#   SRC_DIR         Build-context dir on host (default: /mnt/user/appdata/dotty-pi-src)
#   STATE_DIR       Bind-mount state dir on host (default: /mnt/user/appdata/dotty-pi)
#   IMAGE_TAG       Image tag built + run (default: dotty-pi:0.1.0)
#
# Requires root login (or passwordless sudo) on the SSH user.

set -euo pipefail

DOTTY_PI_HOST="${DOTTY_PI_HOST:?set DOTTY_PI_HOST=user@host}"
SRC_DIR="${SRC_DIR:-/mnt/user/appdata/dotty-pi-src}"
STATE_DIR="${STATE_DIR:-/mnt/user/appdata/dotty-pi}"
IMAGE_TAG="${IMAGE_TAG:-dotty-pi:0.1.0}"
TS="$(date +%Y%m%d-%H%M%S)"
CTX_TGZ="$(mktemp -t dotty-pi-ctx.XXXXXX.tgz)"
EXT_TGZ="$(mktemp -t dotty-pi-ext.XXXXXX.tgz)"
trap 'rm -f "$CTX_TGZ" "$EXT_TGZ"' EXIT

cd "$(git rev-parse --show-toplevel)"

# 1. Enumerate the deploy sets from HEAD — tracked files only, so __pycache__ /
#    node_modules / in-progress edits are skipped. node_modules is gitignored,
#    so the extension set is pure source.
CTX_FILES=(dotty-pi/Dockerfile dotty-pi/docker-compose.yml)
CFG_FILE=dotty-pi/models.json
mapfile -t EXT_FILES < <(git ls-tree -r --name-only HEAD dotty-pi-ext/)
for f in "${CTX_FILES[@]}" "$CFG_FILE"; do
    [[ -f "$f" ]] || { echo "ERROR: missing tracked file $f" >&2; exit 1; }
done
if [[ ${#EXT_FILES[@]} -eq 0 ]]; then
    echo "ERROR: no tracked dotty-pi-ext files at HEAD" >&2
    exit 1
fi
DEPLOY_SHA="$(git rev-parse --short HEAD)"
echo "Deploy set: ${#CTX_FILES[@]} context + 1 config + ${#EXT_FILES[@]} extension files (HEAD $DEPLOY_SHA)"

# 2. SSH preflight — fail fast on bad creds.
ssh -o BatchMode=yes -o ConnectTimeout=5 "$DOTTY_PI_HOST" true \
    || { echo "ERROR: ssh preflight failed for $DOTTY_PI_HOST" >&2; exit 1; }

# 3. Pre-deploy snapshot of the WHOLE state tree (brain.db + persona + old
#    extension). Rollback insurance. Keep last 3.
ssh "$DOTTY_PI_HOST" "
    set -euo pipefail
    if [ -d $STATE_DIR ]; then
        cp -a $STATE_DIR ${STATE_DIR}.bak-deploy-$TS
        sh -c 'ls -1dt ${STATE_DIR}.bak-deploy-* 2>/dev/null | tail -n +4 | xargs -r rm -rf' || true
    fi
    mkdir -p $SRC_DIR $STATE_DIR/agent $STATE_DIR/extensions/dotty-pi-ext
"

# 4. Pack the two tarballs locally.
#    - context: Dockerfile + compose (paths carry the dotty-pi/ prefix).
#    - extension: all tracked dotty-pi-ext/ files (prefix dotty-pi-ext/).
tar -czf "$CTX_TGZ" "${CTX_FILES[@]}"
tar -czf "$EXT_TGZ" "${EXT_FILES[@]}"

# 5. Ship + place each destination.
#    5a. Build context → $SRC_DIR (strip the dotty-pi/ segment).
cat "$CTX_TGZ" | ssh "$DOTTY_PI_HOST" "cat > /tmp/dotty-pi-ctx.tgz"
#    5b. models.json → $STATE_DIR/agent/ (single file; repo-owned config).
cat "$CFG_FILE" | ssh "$DOTTY_PI_HOST" "cat > $STATE_DIR/agent/models.json"
#    5c. Extension → $STATE_DIR/extensions/dotty-pi-ext/ (clean-replace,
#        preserving node_modules so the compiled better-sqlite3 survives).
cat "$EXT_TGZ" | ssh "$DOTTY_PI_HOST" "cat > /tmp/dotty-pi-ext.tgz"

ssh "$DOTTY_PI_HOST" "
    set -euo pipefail
    # build context
    tar -xzf /tmp/dotty-pi-ctx.tgz -C $SRC_DIR --strip-components=1
    rm -f /tmp/dotty-pi-ctx.tgz
    # extension: remove everything EXCEPT node_modules, then extract fresh
    # source. This drops orphaned spike files (e.g. stale set_led) while
    # keeping the hand-compiled native module.
    find $STATE_DIR/extensions/dotty-pi-ext -mindepth 1 -maxdepth 1 \
        ! -name node_modules -exec rm -rf {} +
    tar -xzf /tmp/dotty-pi-ext.tgz -C $STATE_DIR/extensions/dotty-pi-ext --strip-components=1
    rm -f /tmp/dotty-pi-ext.tgz
"

# 6. Build the pinned image + recreate. Repo compose has no build: directive,
#    so build then up (like the sibling scripts). The previous image (e.g.
#    dotty-pi:spike) is left in place as a rollback target.
#
#    `docker rm -f dotty-pi` first: the old container was created by compose
#    from $STATE_DIR (a different compose project than $SRC_DIR), but the
#    compose file pins container_name: dotty-pi — so `compose up` from the new
#    dir would hit a name conflict with the orphaned container. Removing the
#    container by name only drops the container; the image and the on-disk
#    bind-mount state ($STATE_DIR) are untouched.
ssh "$DOTTY_PI_HOST" "
    set -euo pipefail
    docker build -t $IMAGE_TAG $SRC_DIR
    docker rm -f dotty-pi 2>/dev/null || true
    cd $SRC_DIR
    docker compose up -d
"

# 7. Healthcheck — no HTTP endpoint. Assert the container is up, pi runs in the
#    new image, and the extension's tool source landed. (Functional voice-tool
#    smoke is a manual step — see the note printed at the end.)
ssh "$DOTTY_PI_HOST" "
    set -euo pipefail
    DEADLINE=\$((\$(date +%s) + 30))
    while [ \$(date +%s) -lt \$DEADLINE ]; do
        if docker exec dotty-pi pi --version >/dev/null 2>&1; then
            echo -n 'pi --version: '; docker exec dotty-pi pi --version
            break
        fi
        sleep 1
    done
    if ! docker exec dotty-pi pi --version >/dev/null 2>&1; then
        echo 'ERROR: pi --version never succeeded within 30s' >&2
        docker logs --tail 40 dotty-pi >&2 || true
        exit 1
    fi
    MISSING=0
    for t in memory_lookup remember recall_person remember_person think_hard take_photo play_song; do
        if [ ! -f $STATE_DIR/extensions/dotty-pi-ext/src/tools/\$t.ts ]; then
            echo \"ERROR: missing tool source: \$t.ts\" >&2; MISSING=1
        fi
    done
    [ \$MISSING -eq 0 ] || exit 1
    echo 'Extension: all 7 tool sources present.'
"

# 8. md5 round-trip on every shipped file, path-mapped per destination.
ctx_local="$(md5sum "${CTX_FILES[@]}" | sed 's|dotty-pi/||' | sort -k2)"
ctx_remote="$(ssh "$DOTTY_PI_HOST" "cd $SRC_DIR && md5sum Dockerfile docker-compose.yml" | sort -k2)"
cfg_local="$(md5sum "$CFG_FILE" | sed 's|dotty-pi/||' | sort -k2)"
cfg_remote="$(ssh "$DOTTY_PI_HOST" "cd $STATE_DIR/agent && md5sum models.json" | sort -k2)"
ext_local="$(md5sum "${EXT_FILES[@]}" | sed 's|dotty-pi-ext/||' | sort -k2)"
ext_remote_list="$(printf '%q ' "${EXT_FILES[@]/#dotty-pi-ext\//}")"
ext_remote="$(ssh "$DOTTY_PI_HOST" "cd $STATE_DIR/extensions/dotty-pi-ext && md5sum $ext_remote_list" | sort -k2)"

fail=0
for pair in "context:$ctx_local|$ctx_remote" "config:$cfg_local|$cfg_remote" "extension:$ext_local|$ext_remote"; do
    name="${pair%%:*}"; rest="${pair#*:}"; l="${rest%%|*}"; r="${rest#*|}"
    if [[ "$l" != "$r" ]]; then
        echo "ERROR: md5 mismatch in $name set" >&2
        diff <(echo "$l") <(echo "$r") >&2 || true
        fail=1
    fi
done
[[ $fail -eq 0 ]] || exit 1

echo "OK — deployed $IMAGE_TAG (HEAD $DEPLOY_SHA), container healthy, md5s match."
echo
echo "MANUAL post-deploy smoke (does NOT run automatically — avoids model eviction):"
echo "  Trigger a voice turn that exercises a tool (e.g. ask Dotty to remember/recall"
echo "  something). The agent loop must stay on qwen3.5:4b; never pi --model qwen3.6:27b"
echo "  from this container (it evicts qwen3.6:27b-think and breaks think_hard)."
