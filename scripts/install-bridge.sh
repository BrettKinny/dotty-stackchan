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

# repo files
if [[ ! -f "${REPO_DIR}/bridge.py" ]]; then
    err "bridge.py not found at ${REPO_DIR}/bridge.py — run this script from the repo."
    exit 1
fi
if [[ ! -f "${REPO_DIR}/bridge/requirements.txt" ]]; then
    err "bridge/requirements.txt not found at ${REPO_DIR}/bridge/requirements.txt"
    exit 1
fi
info "Repo files OK (${REPO_DIR})"

# ---------- create bridge directory ----------
step "Setting up bridge directory: ${BRIDGE_DIR}"

run mkdir -p "${BRIDGE_DIR}"

if $DRY_RUN; then
    info "[dry-run] cp ${REPO_DIR}/bridge.py -> ${BRIDGE_DIR}/bridge.py"
else
    cp "${REPO_DIR}/bridge.py" "${BRIDGE_DIR}/bridge.py"
    info "Copied bridge.py"
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

if $DRY_RUN; then
    info "[dry-run] Would write ${SERVICE_FILE}:"
    printf '%s\n' "${SERVICE_CONTENT}"
else
    printf '%s\n' "${SERVICE_CONTENT}" > "${SERVICE_FILE}"
    info "Wrote ${SERVICE_FILE}"
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
