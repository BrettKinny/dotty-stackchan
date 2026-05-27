#!/usr/bin/env bash
# scripts/bench-video.sh — Close the video loop in bench testing.
#
# Tails all Dotty container logs while you perform a physical bench test,
# then hands the recorded YouTube Shorts URL + captured logs to the Gemini
# CLI for visual analysis, and prints a combined report.
#
# Usage:
#   bash scripts/bench-video.sh [test description]
#
# Environment:
#   DOTTY_HOST    SSH target (user@host) for the Docker host — required.
#                 Same host that runs xiaozhi-esp32-server, dotty-pi, etc.
#                 Read from .env automatically if that file exists.
#   GEMINI_MODEL  Gemini model to use (default: gemini-2.5-pro).
#   LOG_LINES     Max log lines per container fed to Gemini (default: 200).
#
# Requires:
#   • SSH key-based auth to DOTTY_HOST (no passphrase prompt).
#   • `gemini` CLI on PATH (npm install -g @google/gemini-cli).

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# ── Colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
die()  { echo -e "${RED}ERROR: $*${RESET}" >&2; exit 1; }
info() { echo -e "${CYAN}$*${RESET}"; }
ok()   { echo -e "${GREEN}✓ $*${RESET}"; }
warn() { echo -e "${YELLOW}⚠  $*${RESET}"; }

# ── Load .env if present ──────────────────────────────────────────────
if [[ -f .env ]]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | grep -v '^$' | xargs) 2>/dev/null || true
fi

# ── Config ────────────────────────────────────────────────────────────
DOTTY_HOST="${DOTTY_HOST:-}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-pro}"
LOG_LINES="${LOG_LINES:-200}"

# Containers to tail (order: most signal-rich first)
CONTAINERS=(xiaozhi-esp32-server dotty-pi dotty-behaviour dotty-bridge)

# ── Preflight ─────────────────────────────────────────────────────────
[[ -z "$DOTTY_HOST" ]] && die \
  "DOTTY_HOST not set. Export it or add DOTTY_HOST=user@host to .env\n  e.g. DOTTY_HOST=root@192.168.1.10 bash scripts/bench-video.sh"

command -v gemini &>/dev/null || die \
  "'gemini' not found on PATH. Install with: npm install -g @google/gemini-cli"

info "SSH preflight → $DOTTY_HOST …"
ssh -o BatchMode=yes -o ConnectTimeout=8 "$DOTTY_HOST" true \
  || die "SSH preflight failed for $DOTTY_HOST — check key auth"
ok "SSH OK"

# ── Test description ──────────────────────────────────────────────────
if [[ -n "${1:-}" ]]; then
  TEST_DESC="$*"
else
  echo ""
  echo -e "${BOLD}What bench test are you running?${RESET}"
  echo "  (describe the feature/scenario to exercise)"
  read -rp "  > " TEST_DESC
fi
[[ -z "$TEST_DESC" ]] && die "Test description is required."

# ── Discover running containers ────────────────────────────────────────
info "Discovering running containers on $DOTTY_HOST …"
RUNNING_CONTAINERS=()
for c in "${CONTAINERS[@]}"; do
  if ssh "$DOTTY_HOST" "docker inspect --format='{{.State.Running}}' $c 2>/dev/null" 2>/dev/null | grep -q true; then
    RUNNING_CONTAINERS+=("$c")
    ok "  $c — running"
  else
    warn "  $c — not found / not running (skipping)"
  fi
done

[[ ${#RUNNING_CONTAINERS[@]} -eq 0 ]] && die "No Dotty containers are running on $DOTTY_HOST"

# ── Start log capture ─────────────────────────────────────────────────
TMPDIR_RUN=$(mktemp -d)
LOG_FILE="$TMPDIR_RUN/bench-logs.txt"
trap 'kill "$SSH_PID" 2>/dev/null; wait "$SSH_PID" 2>/dev/null; rm -rf "$TMPDIR_RUN"' EXIT

echo ""
info "Starting log capture (--tail 0 = new events only) …"

# Build the remote command: tail all running containers concurrently,
# prefix each line with [container-name] and an ISO timestamp.
REMOTE_CMD=""
for c in "${RUNNING_CONTAINERS[@]}"; do
  REMOTE_CMD+="docker logs --tail 0 -f --timestamps $c 2>&1 | sed 's/^/[$c] /' & "
done
REMOTE_CMD+="wait"

ssh "$DOTTY_HOST" "$REMOTE_CMD" >> "$LOG_FILE" 2>&1 &
SSH_PID=$!

ok "Log capture started (PID $SSH_PID) — writing to $LOG_FILE"

# ── Instruct the user ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}══════════════════════════════════════════${RESET}"
echo -e "${BOLD} TEST: ${TEST_DESC}${RESET}"
echo -e "${BOLD}══════════════════════════════════════════${RESET}"
echo ""
echo -e "  1. ${BOLD}Grab your phone${RESET} and open the camera."
echo -e "  2. Frame Dotty so you can see her face + LEDs clearly."
echo -e "  3. ${BOLD}Perform the test${RESET} while recording."
echo -e "  4. When done: ${BOLD}upload to YouTube Shorts${RESET} (unlisted is fine)."
echo ""

# ── Wait for YouTube URL ──────────────────────────────────────────────
VIDEO_URL=""
while [[ -z "$VIDEO_URL" ]]; do
  read -rp "  Paste the YouTube Shorts URL when ready (or 'q' to quit): " VIDEO_URL
  if [[ "$VIDEO_URL" == "q" ]]; then
    warn "Aborted by user."
    exit 0
  fi
  if ! echo "$VIDEO_URL" | grep -qE '^https?://(www\.)?(youtube\.com|youtu\.be)/'; then
    warn "That doesn't look like a YouTube URL — try again."
    VIDEO_URL=""
  fi
done
echo ""

# ── Stop log capture ──────────────────────────────────────────────────
info "Stopping log capture …"
kill "$SSH_PID" 2>/dev/null || true
wait "$SSH_PID" 2>/dev/null || true
SSH_PID=0   # disarm trap

TOTAL_LINES=$(wc -l < "$LOG_FILE" || echo 0)
ok "Captured $TOTAL_LINES log lines"

# ── Build per-container log excerpts ──────────────────────────────────
build_log_excerpt() {
  local container="$1"
  local raw
  raw=$(grep -F "[$container]" "$LOG_FILE" || true)
  local n
  n=$(echo "$raw" | grep -c . || echo 0)
  echo "=== $container ($n lines) ==="
  if [[ $n -eq 0 ]]; then
    echo "(no output)"
  elif [[ $n -gt $LOG_LINES ]]; then
    echo "[... $((n - LOG_LINES)) earlier lines omitted ...]"
    echo "$raw" | tail -n "$LOG_LINES"
  else
    echo "$raw"
  fi
}

LOG_EXCERPT=""
for c in "${RUNNING_CONTAINERS[@]}"; do
  LOG_EXCERPT+="$(build_log_excerpt "$c")"$'\n\n'
done

# ── Build Gemini prompt ───────────────────────────────────────────────
PROMPT="You are analysing a physical bench test of the \"Dotty\" StackChan robot assistant — a M5Stack desktop robot with voice I/O, LED rings, and face animations.

## Test being performed
${TEST_DESC}

## Video
Watch the full YouTube video at this URL: ${VIDEO_URL}

## System logs captured during the test
(Prefixed with [container-name]. Timestamps are UTC.)

${LOG_EXCERPT}

## Your analysis (be specific and technical — this is for the developer iterating on the firmware and voice pipeline)

**1. Visual observation**
Describe in detail what you see in the video:
- What voice interactions happened (what was said, any latency)?
- LED ring states — left ring (state arc 0-5) and right ring (face/kid/smart/listening on pixels 6/8/9/11)?
- Face animation (emoji mapping: 😊smile 😆laugh 😢sad 😮surprise 🤔thinking 😠angry 😐neutral 😍love 😴sleepy)?
- Head movements, physical behaviour?

**2. Log correlation**
For each key visual event, find the matching log lines. Note timing gaps or surprises.

**3. Pass / Fail verdict**
Did the test achieve its stated goal? Cite specific visual + log evidence.

**4. Issues & anomalies**
List anything unexpected: wrong LED, missing emoji, audio glitch, error log, timing problem.

**5. Next steps**
What to fix, tweak, or test next? Keep it actionable."

# ── Invoke Gemini ─────────────────────────────────────────────────────
echo ""
info "Invoking gemini (model: $GEMINI_MODEL) …"
echo -e "${BOLD}══ Gemini Analysis ══════════════════════════════════════════════${RESET}"
echo ""

# Write prompt to temp file to avoid argument-length limits.
PROMPT_FILE="$TMPDIR_RUN/prompt.txt"
printf '%s\n' "$PROMPT" > "$PROMPT_FILE"

if ! gemini --model "$GEMINI_MODEL" < "$PROMPT_FILE"; then
  warn "gemini exited non-zero — check above output for errors."
fi

echo ""
echo -e "${BOLD}══ Raw logs saved to: $LOG_FILE ══${RESET}"
echo -e "(File survives until this shell exits — copy it if you need it.)"

# ── Keep log file alive until user exits ──────────────────────────────
echo ""
read -rp "Press Enter to clean up temp files and exit … " _
