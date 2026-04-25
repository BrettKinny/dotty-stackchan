#!/usr/bin/env bash
# voice-install.sh — Download a curated Piper voice into models/piper/.
#
# Usage:
#   scripts/voice-install.sh <voice-key> [--apply]
#   scripts/voice-install.sh --list
#   scripts/voice-install.sh --help
#
# Arguments:
#   <voice-key>   A key from the catalog, e.g. en_US-kristin-medium.
#                 Run --list to see the full catalog.
#
# Options:
#   --apply       Edit .config.yaml so TTS.LocalPiper points at the new
#                 voice. Without this flag, files are downloaded only.
#   --list        Print the curated voice catalog and exit.
#   --help        Show this help and exit.
#
# Files come from https://huggingface.co/rhasspy/piper-voices (MIT-licensed
# public mirror). Each voice is a .onnx model + .onnx.json config; both
# land in models/piper/.
#
# After install, run `make doctor` to verify, then restart the server:
#   docker compose restart xiaozhi-server

set -euo pipefail

# ---------- resolve repo paths ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CATALOG_FILE="${SCRIPT_DIR}/voice-catalog.txt"
PIPER_DIR="${REPO_DIR}/models/piper"
CONFIG_FILE="${REPO_DIR}/.config.yaml"
HF_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"

# ---------- colors ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

info() { printf "${GREEN}[INFO]${NC}  %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
err()  { printf "${RED}[ERR]${NC}   %s\n" "$*" >&2; }

usage() {
    sed -n '2,/^$/{ s/^# \{0,1\}//; p }' "$0"
    exit 0
}

# ---------- catalog helpers ----------
catalog_lookup() {
    # Print the lang_dir for a given voice key, or empty string if missing.
    local key="$1"
    awk -v k="$key" '
        /^#/ || NF == 0 { next }
        $1 == k { print $2; found=1; exit }
    ' "$CATALOG_FILE"
}

catalog_list() {
    printf "${BOLD}Curated Piper voices${NC} (from scripts/voice-catalog.txt):\n\n"
    awk '
        /^#/ || NF == 0 { next }
        { printf "  %-40s %s\n", $1, $2 }
    ' "$CATALOG_FILE"
    printf "\nFull catalog with character notes: docs/voice-catalog.md\n"
}

# ---------- arg parse ----------
VOICE_KEY=""
APPLY=false

if [[ $# -eq 0 ]]; then
    usage
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            ;;
        --list)
            catalog_list
            exit 0
            ;;
        --apply)
            APPLY=true
            shift
            ;;
        --*)
            err "Unknown option: $1"
            exit 2
            ;;
        *)
            if [[ -n "$VOICE_KEY" ]]; then
                err "Unexpected extra argument: $1"
                exit 2
            fi
            VOICE_KEY="$1"
            shift
            ;;
    esac
done

if [[ -z "$VOICE_KEY" ]]; then
    err "No voice key given. Run --list to see the catalog, or --help."
    exit 2
fi

if [[ ! -f "$CATALOG_FILE" ]]; then
    err "Catalog file not found: $CATALOG_FILE"
    exit 1
fi

LANG_DIR="$(catalog_lookup "$VOICE_KEY")"
if [[ -z "$LANG_DIR" ]]; then
    err "Voice key '$VOICE_KEY' not in catalog."
    err "Run '$0 --list' to see available voices."
    exit 2
fi

# ---------- download ----------
mkdir -p "$PIPER_DIR"

ONNX_NAME="${VOICE_KEY}.onnx"
JSON_NAME="${VOICE_KEY}.onnx.json"
ONNX_URL="${HF_BASE}/${LANG_DIR}/${ONNX_NAME}"
JSON_URL="${HF_BASE}/${LANG_DIR}/${JSON_NAME}"
ONNX_PATH="${PIPER_DIR}/${ONNX_NAME}"
JSON_PATH="${PIPER_DIR}/${JSON_NAME}"

download() {
    local url="$1" dest="$2" name
    name="$(basename "$dest")"
    if [[ -f "$dest" ]]; then
        info "${name} — already present, skipping"
        return 0
    fi
    info "Downloading ${name} ..."
    if ! curl -# -fL -o "${dest}.part" "$url"; then
        err "Download failed: $url"
        rm -f "${dest}.part"
        exit 1
    fi
    mv -f "${dest}.part" "$dest"
}

printf "${BOLD}Installing Piper voice:${NC} %s\n" "$VOICE_KEY"
printf "  source: %s\n" "${HF_BASE}/${LANG_DIR}/"
printf "  dest:   %s\n\n" "$PIPER_DIR"

download "$ONNX_URL" "$ONNX_PATH"
download "$JSON_URL" "$JSON_PATH"

# ---------- optional .config.yaml update ----------
apply_config() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        warn ".config.yaml not found at $CONFIG_FILE — skipping --apply."
        return 0
    fi

    # Use the in-container path the live config expects.
    local container_dir="/opt/xiaozhi-esp32-server/models/piper"
    local new_voice="$VOICE_KEY"
    local new_model="${container_dir}/${ONNX_NAME}"
    local new_cfg="${container_dir}/${JSON_NAME}"

    # Backup once per run.
    cp -f "$CONFIG_FILE" "${CONFIG_FILE}.bak"

    # Edit only inside the LocalPiper block. We use a Python helper so we
    # don't have to wrestle YAML with sed — Python 3 ships on every host
    # this repo targets.
    python3 - "$CONFIG_FILE" "$new_voice" "$new_model" "$new_cfg" <<'PY'
import re, sys, pathlib
path, voice, model, cfg = sys.argv[1:5]
text = pathlib.Path(path).read_text()

# Find the LocalPiper: block (2-space indent under TTS:) and rewrite the
# three keys we care about. Leaves comments and other keys untouched.
def patch(block: str) -> str:
    block = re.sub(r'(?m)^(\s+voice:\s*).*$',         lambda m: m.group(1) + voice, block, count=1)
    block = re.sub(r'(?m)^(\s+model_path:\s*).*$',    lambda m: m.group(1) + model, block, count=1)
    block = re.sub(r'(?m)^(\s+config_path:\s*).*$',   lambda m: m.group(1) + cfg,   block, count=1)
    return block

# Match "  LocalPiper:\n" up to the next 2-space-indented sibling key or EOF.
pattern = re.compile(r'(?ms)^(  LocalPiper:\s*\n)((?:    .*\n|\s*\n)+)')
m = pattern.search(text)
if not m:
    sys.stderr.write("LocalPiper: block not found in .config.yaml\n")
    sys.exit(1)
new_text = text[:m.start()] + m.group(1) + patch(m.group(2)) + text[m.end():]
pathlib.Path(path).write_text(new_text)
PY

    info "Updated TTS.LocalPiper in .config.yaml (backup: .config.yaml.bak)"
}

if $APPLY; then
    apply_config
    info "Now restart the server:  docker compose restart xiaozhi-server"
else
    cat <<EOF

${VOICE_KEY} downloaded to ${PIPER_DIR}/.

Next steps:
  1. Edit .config.yaml so TTS.LocalPiper.voice / model_path / config_path
     point at the new files (or re-run with --apply to do it for you).
  2. Run: make doctor
  3. Restart: docker compose restart xiaozhi-server

EOF
fi
