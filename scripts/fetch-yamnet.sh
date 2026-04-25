#!/usr/bin/env bash
# fetch-yamnet.sh — Download the YAMNet TFLite audio scene classifier.
#
# Usage:
#   scripts/fetch-yamnet.sh [--help]
#
# Behaviour:
#   * Idempotent — skips download if models/yamnet/yamnet.tflite already
#     exists. Re-run with the file removed to force re-download.
#   * Pulls the float YAMNet model from Google's audioset bucket. INT8
#     quantisation is a follow-up step — see "Quantising to INT8" below.
#   * Writes to models/yamnet/ at the repo root, the same path that
#     bridge/audio_scene.py uses by default (env override:
#     YAMNET_MODEL_PATH).
#
# Quantising to INT8:
#   The float .tflite from googleapis storage is the official Google AI
#   release and runs fine on x86_64 (Unraid CPU container). For tighter
#   latency on the RPi, generate an INT8 version with TensorFlow's
#   post-training quantisation (`tf.lite.TFLiteConverter` with
#   `optimizations=[tf.lite.Optimize.DEFAULT]` and a representative
#   dataset of audioset clips). Drop the resulting file at
#   models/yamnet/yamnet_int8.tflite and point YAMNET_MODEL_PATH at it.
#
#   Alternatively, several community mirrors host pre-quantised YAMNet
#   variants on TF Hub / Hugging Face — check there first before
#   spending an evening on conversion.
#
# After fetching, restart the bridge and tail the log:
#   systemctl restart zeroclaw-bridge   # RPi
#   docker compose restart bridge       # if running in compose

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
YAMNET_DIR="${REPO_DIR}/models/yamnet"
YAMNET_FILE="${YAMNET_DIR}/yamnet.tflite"
YAMNET_URL="https://storage.googleapis.com/audioset/yamnet.tflite"
YAMNET_CLASSMAP_URL="https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"
YAMNET_CLASSMAP_FILE="${YAMNET_DIR}/yamnet_class_map.csv"

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

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
fi

if ! command -v curl >/dev/null 2>&1; then
    err "curl is required but not installed."
    exit 1
fi

mkdir -p "${YAMNET_DIR}"

if [[ -f "${YAMNET_FILE}" ]]; then
    info "${YAMNET_FILE} already exists — skipping (delete to force re-download)"
else
    info "Downloading YAMNet float .tflite (~17 MB) to ${YAMNET_FILE}"
    if ! curl -# -L --fail -o "${YAMNET_FILE}.tmp" "${YAMNET_URL}"; then
        err "Failed to download ${YAMNET_URL}"
        rm -f "${YAMNET_FILE}.tmp"
        exit 1
    fi
    mv "${YAMNET_FILE}.tmp" "${YAMNET_FILE}"
    info "Saved ${YAMNET_FILE}"
fi

if [[ -f "${YAMNET_CLASSMAP_FILE}" ]]; then
    info "${YAMNET_CLASSMAP_FILE} already exists — skipping"
else
    info "Downloading official 521-class CSV map to ${YAMNET_CLASSMAP_FILE}"
    if ! curl -# -L --fail -o "${YAMNET_CLASSMAP_FILE}.tmp" "${YAMNET_CLASSMAP_URL}"; then
        warn "Failed to download class map (non-fatal — bridge ships a curated subset)."
        rm -f "${YAMNET_CLASSMAP_FILE}.tmp"
    else
        mv "${YAMNET_CLASSMAP_FILE}.tmp" "${YAMNET_CLASSMAP_FILE}"
        info "Saved ${YAMNET_CLASSMAP_FILE}"
    fi
fi

printf "\n${BOLD}YAMNet ready.${NC}\n"
printf "  Model    : %s\n" "${YAMNET_FILE}"
printf "  Classmap : %s (or curated subset in bridge/yamnet_classmap.py)\n" "${YAMNET_CLASSMAP_FILE}"
printf "  Override : export YAMNET_MODEL_PATH=/path/to/your/quantised.tflite\n"
printf "\n"
printf "Next: install tflite-runtime on the inference host and restart the bridge.\n"
printf "  pip install 'tflite-runtime>=2.13'\n"
printf "  systemctl restart zeroclaw-bridge   # or docker compose restart bridge\n"
