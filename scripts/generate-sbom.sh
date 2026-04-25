#!/usr/bin/env bash
# generate-sbom.sh — Generate a component+license inventory for Dotty.
#
# Usage:
#   scripts/generate-sbom.sh [--help]
#
# Behaviour:
#   * Idempotent — overwrites sbom.json + sbom-server.json each run, so
#     the file at HEAD always reflects the current dependency tree.
#   * Three sections:
#       - server     — Python deps from bridge/requirements.txt, generated
#                      with pip-licenses (must be installed on the host).
#       - containers — image: lines from docker-compose.yml, including any
#                      pinned @sha256:... digests for supply-chain pinning.
#       - firmware   — every dir under firmware/firmware/managed_components/
#                      with its idf_component.yml version. Requires the
#                      firmware submodule to be checked out:
#                        git submodule update --init --recursive
#   * Output is a CycloneDX-ish sbom.json (loose conformance — full
#     CycloneDX 1.5 conformance is a follow-up).
#   * Prints a summary table to stdout (rows: name, version, license).
#
# Installing pip-licenses:
#   pip-licenses inspects whatever Python environment it runs in. To
#   inventory the bridge deps specifically, install both pip-licenses
#   and the bridge requirements into the same venv:
#     python -m venv .venv-sbom
#     source .venv-sbom/bin/activate
#     pip install -r bridge/requirements.txt pip-licenses
#     ./scripts/generate-sbom.sh
#
# Re-running with no Python venv active: server section is recorded as
# unavailable; containers + firmware sections still populate.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SBOM_OUT="${REPO_DIR}/sbom.json"
SBOM_SERVER="${REPO_DIR}/sbom-server.json"
COMPOSE_FILE="${REPO_DIR}/docker-compose.yml"
FW_COMPONENTS_DIR="${REPO_DIR}/firmware/firmware/managed_components"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

info() { printf "${GREEN}[INFO]${NC}  %s\n" "$*" >&2; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*" >&2; }
err()  { printf "${RED}[ERR]${NC}   %s\n" "$*" >&2; }

usage() {
    sed -n '2,/^$/{ s/^# \{0,1\}//; p }' "$0"
    exit 0
}

if [[ "${1-}" == "--help" || "${1-}" == "-h" ]]; then
    usage
fi

# ─────────────────────────────────────────────────────────────────────
# Server (Python) section — pip-licenses
# ─────────────────────────────────────────────────────────────────────
generate_server_section() {
    if ! command -v pip-licenses >/dev/null 2>&1; then
        warn "pip-licenses not found — server section will be empty."
        warn "Install with: pip install pip-licenses (in a venv with bridge/requirements.txt)"
        printf '{"available": false, "reason": "pip-licenses not installed"}'
        return 0
    fi

    info "Running pip-licenses → ${SBOM_SERVER}"
    if ! pip-licenses --format=json --with-license-file --no-license-path \
            --output-file="${SBOM_SERVER}" >/dev/null 2>&1; then
        warn "pip-licenses run failed — emitting empty server section."
        printf '{"available": false, "reason": "pip-licenses run failed"}'
        return 0
    fi

    # Forward the per-package list verbatim.
    cat "${SBOM_SERVER}"
}

# ─────────────────────────────────────────────────────────────────────
# Containers section — parse image: lines from docker-compose.yml
# ─────────────────────────────────────────────────────────────────────
generate_containers_section() {
    if [[ ! -f "${COMPOSE_FILE}" ]]; then
        warn "docker-compose.yml not found at ${COMPOSE_FILE}"
        printf '[]'
        return 0
    fi

    # Match `image: …` lines (any indent). Strip leading whitespace + key.
    # Each entry: { "image": "name:tag", "digest": "sha256:..." | null }
    local entries=""
    while IFS= read -r raw; do
        local line
        line="$(echo "$raw" | sed -E 's/^[[:space:]]*image:[[:space:]]*//; s/[[:space:]]*$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
        [[ -z "$line" ]] && continue

        local digest="null"
        if [[ "$line" == *"@sha256:"* ]]; then
            digest="\"${line#*@}\""
            line="${line%@*}"
        fi
        local entry
        entry="{\"image\": \"${line}\", \"digest\": ${digest}}"
        if [[ -z "$entries" ]]; then
            entries="$entry"
        else
            entries="${entries},${entry}"
        fi
    done < <(grep -E '^[[:space:]]*image:[[:space:]]' "${COMPOSE_FILE}" || true)

    printf '[%s]' "$entries"
}

# ─────────────────────────────────────────────────────────────────────
# Firmware section — walk managed_components/*
# ─────────────────────────────────────────────────────────────────────
generate_firmware_section() {
    if [[ ! -d "${FW_COMPONENTS_DIR}" ]]; then
        warn "firmware/firmware/managed_components/ not found."
        warn "If the firmware submodule is uninitialised, run:"
        warn "  git submodule update --init --recursive"
        printf '{"available": false, "reason": "managed_components dir missing — submodule not checked out?", "components": []}'
        return 0
    fi

    local entries=""
    local count=0
    for comp_dir in "${FW_COMPONENTS_DIR}"/*/; do
        [[ -d "$comp_dir" ]] || continue
        local name
        name="$(basename "$comp_dir")"
        local yml="${comp_dir}idf_component.yml"

        local version="unknown"
        local repo="unknown"
        if [[ -f "$yml" ]]; then
            local v
            v="$(grep -E '^version:[[:space:]]*' "$yml" | head -1 | sed -E "s/^version:[[:space:]]*['\"]?//; s/['\"]?[[:space:]]*$//")"
            [[ -n "$v" ]] && version="$v"
            local r
            r="$(grep -E '^repository:[[:space:]]*' "$yml" | head -1 | sed -E "s/^repository:[[:space:]]*['\"]?//; s/['\"]?[[:space:]]*$//")"
            [[ -n "$r" ]] && repo="$r"
        fi

        local entry
        entry="{\"name\": \"${name}\", \"version\": \"${version}\", \"repository\": \"${repo}\"}"
        if [[ -z "$entries" ]]; then
            entries="$entry"
        else
            entries="${entries},${entry}"
        fi
        count=$((count+1))
    done

    printf '{"available": true, "count": %d, "components": [%s]}' "$count" "$entries"
}

# ─────────────────────────────────────────────────────────────────────
# Compose the combined sbom.json
# ─────────────────────────────────────────────────────────────────────
info "Generating SBOM at ${SBOM_OUT}"

SERVER_JSON="$(generate_server_section)"
CONTAINERS_JSON="$(generate_containers_section)"
FIRMWARE_JSON="$(generate_firmware_section)"

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

cat > "${SBOM_OUT}" <<EOF
{
  "bomFormat": "CycloneDX-ish",
  "specVersion": "1.5-loose",
  "metadata": {
    "timestamp": "${TIMESTAMP}",
    "tool": "scripts/generate-sbom.sh"
  },
  "server": ${SERVER_JSON},
  "containers": ${CONTAINERS_JSON},
  "firmware": ${FIRMWARE_JSON}
}
EOF

info "Wrote ${SBOM_OUT}"

# ─────────────────────────────────────────────────────────────────────
# Summary table
# ─────────────────────────────────────────────────────────────────────
printf "\n${BOLD}SBOM summary${NC}\n"
printf "%-40s %-20s %-30s\n" "name" "version" "license / source"
printf "%-40s %-20s %-30s\n" "----" "-------" "----------------"

# Server: try to summarise from sbom-server.json if it was produced.
if [[ -f "${SBOM_SERVER}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        python3 - "${SBOM_SERVER}" <<'PY' || warn "Could not pretty-print server section."
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    if isinstance(data, list):
        for pkg in data:
            name = pkg.get("Name", "?")[:40]
            ver = str(pkg.get("Version", "?"))[:20]
            lic = pkg.get("License", "?")[:30]
            print(f"{name:<40} {ver:<20} {lic:<30}")
except Exception as e:
    print(f"(server summary skipped: {e})")
PY
    fi
else
    echo "(server section unavailable — install pip-licenses)"
fi

# Containers
echo ""
printf "${BOLD}Containers:${NC}\n"
if command -v python3 >/dev/null 2>&1; then
    python3 - "${SBOM_OUT}" <<'PY' || true
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for c in data.get("containers", []):
    img = c.get("image", "?")[:40]
    digest = c.get("digest") or "(no digest pin)"
    print(f"{img:<40} {'':<20} {digest:<30}")
PY
fi

# Firmware
echo ""
printf "${BOLD}Firmware components:${NC}\n"
if command -v python3 >/dev/null 2>&1; then
    python3 - "${SBOM_OUT}" <<'PY' || true
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
fw = data.get("firmware", {})
if not fw.get("available"):
    print(f"(firmware section unavailable: {fw.get('reason', '?')})")
else:
    for comp in fw.get("components", [])[:10]:
        name = comp.get("name", "?")[:40]
        ver = comp.get("version", "?")[:20]
        repo = comp.get("repository", "?")[:30]
        print(f"{name:<40} {ver:<20} {repo:<30}")
    total = fw.get("count", 0)
    if total > 10:
        print(f"... and {total - 10} more components.")
    print(f"\nTotal firmware components: {total}")
PY
fi

echo ""
info "Done. See ${SBOM_OUT} for full output."
