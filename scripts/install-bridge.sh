#!/bin/bash
# install-bridge.sh — Install zeroclaw-bridge on a Linux host with systemd.
#
# Usage:
#   sudo ./install-bridge.sh [OPTIONS]
#
# Options:
#   --bridge-dir DIR     Install directory  (default: /root/zeroclaw-bridge)
#   --zeroclaw-bin PATH  Path to zeroclaw binary (default: /root/.cargo/bin/zeroclaw)
#   --port PORT          Bridge listen port (default: 8080)
#   --dry-run            Print what would happen without making changes
#   --help               Show this help
#
# The script is idempotent — safe to re-run. It will:
#   1. Verify prerequisites (Python 3.10+, pip, systemd, zeroclaw binary)
#   2. Create the bridge directory and copy bridge.py into it
#   3. Create a Python venv and install dependencies from bridge/requirements.txt
#   4. Write and install a systemd service file
#   5. Enable + start the service
#   6. Health-check the running bridge
#
# Run from the repo root, or from any directory — the script locates the
# repo-relative files (bridge.py, bridge/requirements.txt) from its own path.

set -euo pipefail

# ---------- resolve repo root from script location ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------- defaults ----------
BRIDGE_DIR="/root/zeroclaw-bridge"
ZEROCLAW_BIN="/root/.cargo/bin/zeroclaw"
PORT=8080
DRY_RUN=false
SERVICE_NAME="zeroclaw-bridge"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ---------- colors ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # no color

info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
err()   { printf "${RED}[ERR]${NC}   %s\n" "$*" >&2; }
step()  { printf "\n${BOLD}==> %s${NC}\n" "$*"; }

# ---------- usage ----------
usage() {
    sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
    exit 0
}

# ---------- parse args ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --bridge-dir)   BRIDGE_DIR="$2"; shift 2 ;;
        --zeroclaw-bin) ZEROCLAW_BIN="$2"; shift 2 ;;
        --port)         PORT="$2"; shift 2 ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --help|-h)      usage ;;
        *) err "Unknown option: $1"; usage ;;
    esac
done

# ---------- dry-run wrapper ----------
run() {
    if $DRY_RUN; then
        info "[dry-run] $*"
    else
        "$@"
    fi
}

# ---------- prerequisite checks ----------
step "Checking prerequisites"

# Python 3.10+
if ! command -v python3 &>/dev/null; then
    err "python3 not found. Install Python 3.10+ and retry."
    exit 1
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER##*.}"
if (( PY_MAJOR < 3 )) || (( PY_MAJOR == 3 && PY_MINOR < 10 )); then
    err "Python ${PY_VER} found — need 3.10+."
    exit 1
fi
info "Python ${PY_VER} OK"

# pip (via python3 -m pip)
if ! python3 -m pip --version &>/dev/null; then
    err "pip not available (python3 -m pip failed). Install python3-pip and retry."
    exit 1
fi
info "pip OK"

# venv module
if ! python3 -c "import venv" &>/dev/null; then
    err "Python venv module not available. Install python3-venv and retry."
    exit 1
fi
info "venv module OK"

# systemd
if ! command -v systemctl &>/dev/null; then
    err "systemctl not found — systemd is required."
    exit 1
fi
info "systemd OK"

# zeroclaw binary
if [[ ! -x "${ZEROCLAW_BIN}" ]]; then
    err "zeroclaw binary not found or not executable at: ${ZEROCLAW_BIN}"
    err "Install ZeroClaw first, or pass --zeroclaw-bin /path/to/zeroclaw"
    exit 1
fi
info "zeroclaw binary OK (${ZEROCLAW_BIN})"

# repo files — bridge.py imports textUtils from custom-providers/ via a
# sys.path shim, and the `bridge` package via normal Python import. Both
# trees ship to BRIDGE_DIR alongside bridge.py.
for f in bridge.py bridge/requirements.txt custom-providers/textUtils.py bridge/__init__.py; do
    if [[ ! -e "${REPO_DIR}/${f}" ]]; then
        err "${f} not found at ${REPO_DIR}/${f} — run this script from the dotty-stackchan repo."
        exit 1
    fi
done
info "Repo files OK (${REPO_DIR})"

# ---------- create bridge directory ----------
step "Setting up bridge directory: ${BRIDGE_DIR}"

run mkdir -p "${BRIDGE_DIR}"

if $DRY_RUN; then
    info "[dry-run] cp ${REPO_DIR}/bridge.py -> ${BRIDGE_DIR}/bridge.py"
    info "[dry-run] cp -r ${REPO_DIR}/{custom-providers,bridge} -> ${BRIDGE_DIR}/"
else
    cp "${REPO_DIR}/bridge.py" "${BRIDGE_DIR}/bridge.py"
    info "Copied bridge.py"

    # custom-providers/ holds textUtils.py + LLM/TTS provider modules that
    # bridge.py imports via a sys.path shim. bridge/ holds metrics,
    # dashboard, perception, etc. — bridge.py reaches into them via
    # `from bridge.X import ...`. Both are required to avoid runtime
    # ModuleNotFoundError (issue #13).
    rm -rf "${BRIDGE_DIR}/custom-providers" "${BRIDGE_DIR}/bridge"
    cp -r "${REPO_DIR}/custom-providers" "${BRIDGE_DIR}/custom-providers"
    cp -r "${REPO_DIR}/bridge" "${BRIDGE_DIR}/bridge"
    # CRITICAL: drop bridge/__init__.py so bridge/ acts as a PEP 420
    # namespace package. Without this, `import bridge` resolves to the
    # package (empty __init__) and uvicorn `bridge:app` fails with
    # `module 'bridge' has no attribute 'app'`. With it removed, `import
    # bridge` resolves to bridge.py (the FastAPI app) while
    # `from bridge.metrics import ...` still works.
    rm -f "${BRIDGE_DIR}/bridge/__init__.py"
    # __pycache__ bloat from the source tree; the venv will regenerate.
    find "${BRIDGE_DIR}/custom-providers" "${BRIDGE_DIR}/bridge" \
        -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    info "Copied custom-providers/ and bridge/ (bridge/ as namespace pkg)"
fi

# ---------- create/update venv and install deps ----------
step "Setting up Python venv and dependencies"

VENV_DIR="${BRIDGE_DIR}/.venv"

if [[ -d "${VENV_DIR}" ]]; then
    info "Venv already exists at ${VENV_DIR} — upgrading deps"
else
    info "Creating venv at ${VENV_DIR}"
    run python3 -m venv "${VENV_DIR}"
fi

if $DRY_RUN; then
    info "[dry-run] ${VENV_DIR}/bin/pip install -r ${REPO_DIR}/bridge/requirements.txt"
else
    "${VENV_DIR}/bin/pip" install --upgrade pip --quiet
    "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/bridge/requirements.txt" --quiet
    info "Dependencies installed"
fi

# ---------- install systemd service ----------
step "Installing systemd service: ${SERVICE_NAME}"

SERVICE_CONTENT="[Unit]
Description=ZeroClaw HTTP Bridge for StackChan
After=network.target
Wants=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${BRIDGE_DIR}
# Vision/LLM API keys (issue #15) — leading '-' makes the file optional;
# without it the bridge logs a clear ERROR on photo intents instead of
# confabulating a description from an empty VLM response.
EnvironmentFile=-${BRIDGE_DIR}/.env
Environment=ZEROCLAW_BIN=${ZEROCLAW_BIN}
Environment=PORT=${PORT}
Environment=DOTTY_KID_MODE=true
ExecStart=${VENV_DIR}/bin/uvicorn bridge:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target"

# ---------- create stub .env if absent (issue #15) ----------
# Make the API-key drop-in obvious. systemd already EnvironmentFile=-'s it.
ENV_FILE="${BRIDGE_DIR}/.env"
ENV_STUB="# zeroclaw-bridge runtime env (issue #15). Loaded by systemd via
# EnvironmentFile= in /etc/systemd/system/${SERVICE_NAME}.service. Fill in
# at least OPENROUTER_API_KEY or photo (VLM) intents will fail with an
# explicit 'camera offline' message instead of confabulating descriptions.
# Mode 0600 — contains secrets. After editing:
#     sudo systemctl restart ${SERVICE_NAME}

OPENROUTER_API_KEY=
# Optional split if you use separate keys for the vision model:
#VISION_API_KEY=
#VLM_API_KEY=
"
if [[ -f "${ENV_FILE}" ]]; then
    info "Existing ${ENV_FILE} preserved (not overwriting)"
else
    if $DRY_RUN; then
        info "[dry-run] Would create stub ${ENV_FILE} (mode 0600) with OPENROUTER_API_KEY placeholder"
    else
        printf '%s' "${ENV_STUB}" > "${ENV_FILE}"
        chmod 600 "${ENV_FILE}"
        info "Created stub ${ENV_FILE} (mode 0600) — fill in OPENROUTER_API_KEY before photo intents will work"
    fi
fi

if $DRY_RUN; then
    info "[dry-run] Would write ${SERVICE_FILE}:"
    printf '%s\n' "${SERVICE_CONTENT}"
else
    printf '%s\n' "${SERVICE_CONTENT}" > "${SERVICE_FILE}"
    info "Wrote ${SERVICE_FILE}"
fi

# ---------- smoke test: bridge.py imports cleanly ----------
# Catches missing-module errors (like #13) here, with a readable traceback,
# instead of letting systemd crash-loop the service and burying the cause
# under restart noise. Skipped on dry-run since the venv won't exist.
if ! $DRY_RUN; then
    step "Import smoke test"
    if (cd "${BRIDGE_DIR}" && "${VENV_DIR}/bin/python" -c "import bridge" 2>&1); then
        info "bridge.py imports cleanly"
    else
        err "bridge.py failed to import — see traceback above."
        err "Fix the import error before retrying; the systemd service will"
        err "crash-loop with the same traceback if started in this state."
        exit 1
    fi
fi

# ---------- enable and start the service ----------
step "Enabling and starting ${SERVICE_NAME}"

run systemctl daemon-reload
run systemctl enable "${SERVICE_NAME}"

# Restart if already running, start if not — covers both fresh install and re-run.
run systemctl restart "${SERVICE_NAME}"

if ! $DRY_RUN; then
    # Give the service a moment to start
    sleep 2
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "${SERVICE_NAME} is running"
    else
        warn "${SERVICE_NAME} may not have started — check: journalctl -u ${SERVICE_NAME} -n 30"
    fi
fi

# ---------- health check ----------
step "Health check: http://localhost:${PORT}/health"

if $DRY_RUN; then
    info "[dry-run] curl -sf http://localhost:${PORT}/health"
else
    # Give uvicorn a few seconds to bind
    sleep 3
    if curl -sf "http://localhost:${PORT}/health" -o /dev/null; then
        info "Health check passed"
        curl -s "http://localhost:${PORT}/health" | python3 -m json.tool 2>/dev/null || true
    else
        warn "Health check failed — the service may still be starting."
        warn "Check logs: journalctl -u ${SERVICE_NAME} -f"
    fi
fi

# ---------- done ----------
step "Done"
info "Bridge installed at ${BRIDGE_DIR}"
info "Service: systemctl status ${SERVICE_NAME}"
info "Logs:    journalctl -u ${SERVICE_NAME} -f"
