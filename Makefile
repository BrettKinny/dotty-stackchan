SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Model URLs ───────────────────────────────────────────────────────
SENSEVOICE_REPO  := https://huggingface.co/FunAudioLLM/SenseVoiceSmall
PIPER_BASE       := https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/cori/medium
PIPER_ONNX       := en_GB-cori-medium.onnx
PIPER_JSON       := en_GB-cori-medium.onnx.json
WHISPER_REPO     := https://huggingface.co/Systran/faster-whisper-small.en
WHISPER_DIR      := models/whisper-small.en-ct2
WHISPER_FILES    := config.json model.bin tokenizer.json vocabulary.txt

# ── Colours ──────────────────────────────────────────────────────────
GREEN  := \033[0;32m
RED    := \033[0;31m
YELLOW := \033[0;33m
BOLD   := \033[1m
RESET  := \033[0m

# ── Robust download helper (issue #124) ──────────────────────────────
# Shell function injected at the head of each fetch block as `dl_file <url> <dest>`.
# `-f` turns any HTTP >=400 into a non-zero exit (and suppresses saving the error
# body), `--retry` rides out transient HF hiccups, the size floor rejects the
# 15-byte "Entry not found" stubs that the old bare `curl -o` saved silently, and
# every failure `rm`s the partial so the skip-if-exists guard can't "succeed" on it.
DL_FILE = dl_file() { if curl -fL --retry 3 --retry-delay 1 --progress-bar -o "$$2" "$$1"; then _sz=$$(wc -c < "$$2" 2>/dev/null || echo 0); if [ "$$_sz" -lt 100 ]; then echo -e "  $(RED)$$2: only $$_sz bytes — treating as a failed download$(RESET)"; rm -f "$$2"; return 1; fi; else echo -e "  $(RED)Failed to download $$1$(RESET)"; rm -f "$$2"; return 1; fi; }

# ── Targets ──────────────────────────────────────────────────────────
.PHONY: help setup fetch-models doctor audit up down logs status voice-list voice-install sbom verify-firmware test lint check _preflight-compose _preflight-rendered

# ─────────────────────────────────────────────────────────────────────
# _preflight-compose — fail fast if Docker Compose v2 plugin is missing
#
# Issue #6: on Ubuntu 24.04 with the distro `docker.io` package, only
# the legacy v1 `docker-compose` (Python, separate binary) is shipped.
# `docker compose <subcmd>` either errors with "is not a docker
# command" or routes args into a parser that rejects flags like `-d`
# with "unknown shorthand flag". Either way the user sees a cryptic
# failure inside whatever target they invoked. Catch it up front with
# install guidance instead.
# ─────────────────────────────────────────────────────────────────────
_preflight-rendered:
	@if [ ! -f docker-compose.yml ] || [ ! -f data/.config.yaml ]; then \
	  echo ""; \
	  echo -e "$(RED)Error: docker-compose.yml and/or data/.config.yaml not found.$(RESET)"; \
	  echo "These are rendered from *.template by 'make setup'."; \
	  echo "Run:  make setup"; \
	  echo ""; \
	  exit 1; \
	fi

_preflight-compose:
	@if ! docker compose version >/dev/null 2>&1; then \
	  echo ""; \
	  echo -e "$(RED)Error: Docker Compose v2 plugin is not available.$(RESET)"; \
	  echo ""; \
	  echo "This Makefile requires the v2 plugin (the 'docker compose'"; \
	  echo "subcommand, no hyphen). The legacy 'docker-compose' binary"; \
	  echo "is not supported."; \
	  echo ""; \
	  echo "Install on Debian/Ubuntu:"; \
	  echo "    sudo apt install docker-compose-plugin"; \
	  echo ""; \
	  echo "Other distros / manual install:"; \
	  echo "    https://docs.docker.com/compose/install/linux/"; \
	  echo ""; \
	  exit 1; \
	fi

help: ## Show this help
	@echo ""
	@echo -e "$(BOLD)Dotty$(RESET) — your self-hosted StackChan robot assistant"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(BOLD)%-15s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ─────────────────────────────────────────────────────────────────────
# Dev-loop targets — same commands CI runs, single source of truth.
# Assumes a venv with `pytest pytest-cov ruff` available on PATH. If
# you use a project venv, run e.g.  `source .venv/bin/activate && make check`.
# ─────────────────────────────────────────────────────────────────────
test: ## Run Python unit tests with coverage gate
	pytest tests/ custom-providers/pi_voice/tests/ \
		--cov --cov-report=term --cov-fail-under=56

lint: ## Run ruff lint over the repo
	ruff check .

check: lint test ## Run lint + tests (the CI gate)

# ─────────────────────────────────────────────────────────────────────
# setup — interactive first-run wizard
#
# Idempotent: reads previous answers from .wizard.env when present,
# offers them as defaults, and re-renders all live config files from
# the *.template sources. Re-running setup never modifies tracked files
# (the templates are tracked; rendered copies are in .gitignore).
#
# Also detects whether the NVIDIA Docker runtime is available and
# branches the rendered config — falls back to FunASR (CPU) + drops the
# `runtime: nvidia` block when CUDA isn't on the host.
# ─────────────────────────────────────────────────────────────────────
WIZARD_ENV := .wizard.env

setup: _preflight-compose ## Interactive first-run wizard (re-runnable; remembers previous answers)
	@echo ""
	@echo -e "$(BOLD)Dotty setup wizard$(RESET)"
	@echo "Renders config files from *.template sources. Re-runnable;"
	@echo "previous answers are loaded from $(WIZARD_ENV) and shown as defaults."
	@echo ""
	@# Source previous answers if present so we can offer them as defaults.
	@# Values are written via `printf '%q'` below so sourcing is safe even
	@# for inputs with spaces or shell metacharacters.
	@set -e; \
	 if [ -f $(WIZARD_ENV) ]; then \
	   echo -e "$(GREEN)Found $(WIZARD_ENV) — previous answers loaded as defaults.$(RESET)"; \
	   set -a; . ./$(WIZARD_ENV); set +a; \
	   echo ""; \
	 fi; \
	 prompt() { \
	   local var="$$1" label="$$2" example="$$3" cur="$$4" ans hint; \
	   hint="$$label"; \
	   if [ -n "$$cur" ]; then hint="$$hint [$$cur]"; \
	   elif [ -n "$$example" ]; then hint="$$hint (e.g. $$example)"; fi; \
	   read -rp "$$hint: " ans; \
	   if [ -z "$$ans" ]; then ans="$$cur"; fi; \
	   printf -v "$$var" '%s' "$$ans"; \
	 }; \
	 prompt XIAOZHI_HOST    "XIAOZHI_HOST     (LAN IP of Docker host)" "192.168.1.10"  "$$XIAOZHI_HOST"; \
	 prompt ROBOT_NAME      "ROBOT_NAME       (what the robot calls itself)" "Dotty"   "$${ROBOT_NAME:-Dotty}"; \
	 prompt YOUR_NAME       "YOUR_NAME        (your name / org)" "Brett"               "$$YOUR_NAME"; \
	 prompt TZ_VALUE        "TZ_VALUE         (IANA timezone)" "Australia/Brisbane"    "$$TZ_VALUE"; \
	 echo ""; \
	 if [ -z "$$XIAOZHI_HOST" ] || \
	    [ -z "$$ROBOT_NAME" ] || [ -z "$$YOUR_NAME" ] || [ -z "$$TZ_VALUE" ]; then \
	   echo -e "$(RED)Error: all fields are required.$(RESET)"; exit 1; \
	 fi; \
	 echo -e "$(BOLD)Detecting NVIDIA Docker runtime...$(RESET)"; \
	 if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi '"nvidia"'; then \
	   HAS_CUDA=1; \
	   echo -e "  $(GREEN)Found — using WhisperLocal on GPU (float16).$(RESET)"; \
	 else \
	   HAS_CUDA=0; \
	   echo -e "  $(YELLOW)Not found — using FunASR on CPU instead.$(RESET)"; \
	   echo "  (Install nvidia-container-toolkit and re-run setup to enable GPU ASR.)"; \
	 fi; \
	 echo ""; \
	 echo -e "$(BOLD)Saving answers to $(WIZARD_ENV)...$(RESET)"; \
	 { \
	   echo "# Generated by 'make setup' — defaults for the next run."; \
	   echo "# Values shell-quoted so sourcing tolerates spaces and metacharacters."; \
	   printf 'XIAOZHI_HOST=%q\n'     "$$XIAOZHI_HOST"; \
	   printf 'ROBOT_NAME=%q\n'       "$$ROBOT_NAME"; \
	   printf 'YOUR_NAME=%q\n'        "$$YOUR_NAME"; \
	   printf 'TZ_VALUE=%q\n'         "$$TZ_VALUE"; \
	   printf 'HAS_CUDA=%q\n'         "$$HAS_CUDA"; \
	 } > $(WIZARD_ENV); \
	 echo "  $(WIZARD_ENV) — done"; \
	 echo ""; \
	 echo -e "$(BOLD)Ensuring .env + admin-API token...$(RESET)"; \
	 if [ ! -f .env ]; then \
	   cp .env.example .env; \
	   echo "  .env created from .env.example"; \
	 fi; \
	 if grep -q '^DOTTY_ADMIN_TOKEN=' .env; then \
	   echo -e "  $(GREEN)DOTTY_ADMIN_TOKEN already present in .env — keeping it.$(RESET)"; \
	 else \
	   ADMIN_TOKEN=$$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'); \
	   printf '\nDOTTY_ADMIN_TOKEN=%s\n' "$$ADMIN_TOKEN" >> .env; \
	   echo "  generated DOTTY_ADMIN_TOKEN → .env (authenticates /xiaozhi/admin/*)"; \
	   echo -e "  $(YELLOW)NOTE:$(RESET) set the SAME value in the bridge / dotty-behaviour /"; \
	   echo "        dotty-pi deploy-dir .env files, or their admin calls will 401."; \
	   echo "        See .env.example ('Admin API auth') for details."; \
	 fi; \
	 echo ""; \
	 echo -e "$(BOLD)Rendering templates...$(RESET)"; \
	 mkdir -p data; \
	 if [ "$$HAS_CUDA" = "1" ]; then \
	   ASR_MODULE=WhisperLocal; ASR_DEVICE=cuda;  ASR_COMPUTE_TYPE=float16; \
	 else \
	   ASR_MODULE=FunASR;       ASR_DEVICE=cpu;   ASR_COMPUTE_TYPE=int8; \
	 fi; \
	 sed_escape() { printf '%s' "$$1" | sed -e 's/[\\&|]/\\&/g'; }; \
	 e_XIAOZHI_HOST=$$(sed_escape   "$$XIAOZHI_HOST"); \
	 e_ROBOT_NAME=$$(sed_escape     "$$ROBOT_NAME"); \
	 e_YOUR_NAME=$$(sed_escape      "$$YOUR_NAME"); \
	 e_TZ_VALUE=$$(sed_escape       "$$TZ_VALUE"); \
	 render() { \
	   local src="$$1" dst="$$2"; \
	   sed \
	     -e "s|<XIAOZHI_HOST>|$$e_XIAOZHI_HOST|g" \
	     -e "s|<ROBOT_NAME>|$$e_ROBOT_NAME|g" \
	     -e "s|You are Dotty,|You are $$e_ROBOT_NAME,|g" \
	     -e "s|<YOUR_NAME>|$$e_YOUR_NAME|g" \
	     -e "s|<TZ_VALUE>|$$e_TZ_VALUE|g" \
	     -e "s|<ASR_MODULE>|$$ASR_MODULE|g" \
	     -e "s|<ASR_DEVICE>|$$ASR_DEVICE|g" \
	     -e "s|<ASR_COMPUTE_TYPE>|$$ASR_COMPUTE_TYPE|g" \
	     "$$src" > "$$dst.tmp"; \
	   if [ "$$HAS_CUDA" != "1" ]; then \
	     sed -i \
	       -e '/# --- BEGIN CUDA BLOCK/,/# --- END CUDA BLOCK ---/d' \
	       -e '/# --- BEGIN CUDA ENV/,/# --- END CUDA ENV ---/d' \
	       "$$dst.tmp"; \
	   fi; \
	   mv "$$dst.tmp" "$$dst"; \
	   echo "  $$src → $$dst"; \
	 }; \
	 render .config.yaml.template            data/.config.yaml; \
	 render docker-compose.yml.template      docker-compose.yml; \
	 echo ""; \
	 $(MAKE) fetch-models; \
	 echo ""; \
	 echo -e "$(BOLD)Starting containers...$(RESET)"; \
	 docker compose up -d; \
	 echo ""; \
	 echo -e "$(GREEN)$(BOLD)Setup complete.$(RESET)"; \
	 echo ""; \
	 echo "Next steps:"; \
	 echo "  1. Flash the StackChan firmware (see SETUP.md or m5stack/StackChan repo)."; \
	 echo "  2. In the device's Advanced Options, set the OTA URL to:"; \
	 echo "       http://$$XIAOZHI_HOST:8003/xiaozhi/ota/"; \
	 echo "  3. Run 'make doctor' to verify everything is healthy."; \
	 echo ""

# ─────────────────────────────────────────────────────────────────────
# fetch-models — download ASR + TTS model files
# ─────────────────────────────────────────────────────────────────────
SENSEVOICE_FILES := model.pt config.yaml configuration.json am.mvn chn_jpn_yue_eng_ko_spectok.bpe.model
# Stale filenames shipped before the #124 fix: `tokens.json` and
# `chn_jpn_yue_eng_ko_spectral.fbank.conf.yaml` never existed in the HF repo and
# downloaded as 15-byte "Entry not found" stubs. Removed on existing installs below.
SENSEVOICE_STALE := tokens.json chn_jpn_yue_eng_ko_spectral.fbank.conf.yaml
SENSEVOICE_DIR   := models/SenseVoiceSmall
PIPER_DIR        := models/piper

fetch-models: ## Download SenseVoiceSmall + Piper voice models
	@echo ""
	@echo -e "$(BOLD)Fetching models...$(RESET)"
	@echo ""
	@# ── SenseVoiceSmall ──
	@mkdir -p $(SENSEVOICE_DIR)
	@echo -e "$(BOLD)[SenseVoiceSmall]$(RESET)"
	@# Purge pre-#124 stale stubs so the skip-if-exists guard re-fetches cleanly.
	@for f in $(SENSEVOICE_STALE); do rm -f "$(SENSEVOICE_DIR)/$$f"; done
	@$(DL_FILE); for f in $(SENSEVOICE_FILES); do \
	  if [ -f "$(SENSEVOICE_DIR)/$$f" ]; then \
	    echo -e "  $(GREEN)$$f — already exists, skipping$(RESET)"; \
	  else \
	    echo "  Downloading $$f ..."; \
	    dl_file "$(SENSEVOICE_REPO)/resolve/main/$$f" "$(SENSEVOICE_DIR)/$$f" || exit 1; \
	  fi; \
	done
	@echo ""
	@# ── Piper voice ──
	@mkdir -p $(PIPER_DIR)
	@echo -e "$(BOLD)[Piper TTS — $(PIPER_ONNX)]$(RESET)"
	@$(DL_FILE); for f in $(PIPER_ONNX) $(PIPER_JSON); do \
	  if [ -f "$(PIPER_DIR)/$$f" ]; then \
	    echo -e "  $(GREEN)$$f — already exists, skipping$(RESET)"; \
	  else \
	    echo "  Downloading $$f ..."; \
	    dl_file "$(PIPER_BASE)/$$f" "$(PIPER_DIR)/$$f" || exit 1; \
	  fi; \
	done
	@echo ""
	@# ── faster-whisper small.en (CTranslate2) ──
	@mkdir -p $(WHISPER_DIR)
	@echo -e "$(BOLD)[faster-whisper small.en]$(RESET)"
	@$(DL_FILE); for f in $(WHISPER_FILES); do \
	  if [ -f "$(WHISPER_DIR)/$$f" ]; then \
	    echo -e "  $(GREEN)$$f — already exists, skipping$(RESET)"; \
	  else \
	    echo "  Downloading $$f ..."; \
	    dl_file "$(WHISPER_REPO)/resolve/main/$$f" "$(WHISPER_DIR)/$$f" || exit 1; \
	  fi; \
	done
	@echo ""
	@echo -e "$(GREEN)All models ready.$(RESET)"

# ─────────────────────────────────────────────────────────────────────
# sbom — generate a component+license inventory (sbom.json at repo root)
# ─────────────────────────────────────────────────────────────────────
sbom: ## Generate Software Bill of Materials (sbom.json)
	@./scripts/generate-sbom.sh

# ─────────────────────────────────────────────────────────────────────
# doctor — health checks
#
# Looks at data/.config.yaml (the rendered output of `make setup`); falls
# back to the legacy root .config.yaml for pre-template checkouts.
# WIZARD_PLACEHOLDERS is the closed set of <TOKEN>s the wizard owns —
# the template also carries <OPENAI_COMPAT_URL>, etc.
# in alternate backend blocks; an unsubstituted token in an unselected
# backend isn't an error.
# ─────────────────────────────────────────────────────────────────────
WIZARD_PLACEHOLDERS := <XIAOZHI_HOST>|<ROBOT_NAME>|<YOUR_NAME>|<TZ_VALUE>|<ASR_MODULE>|<ASR_DEVICE>|<ASR_COMPUTE_TYPE>

doctor: ## Run health checks on config, models, and services
	@echo ""
	@echo -e "$(BOLD)Running health checks...$(RESET)"
	@echo ""
	@PASS=0; FAIL=0; \
	 check() { \
	   if eval "$$2" >/dev/null 2>&1; then \
	     echo -e "  $(GREEN)PASS$(RESET)  $$1"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo -e "  $(RED)FAIL$(RESET)  $$1"; \
	     FAIL=$$((FAIL+1)); \
	   fi; \
	 }; \
	 if [ -f data/.config.yaml ]; then CFG=data/.config.yaml; \
	 elif [ -f .config.yaml ]; then CFG=.config.yaml; \
	 else CFG=""; fi; \
	 if [ -n "$$CFG" ]; then \
	   echo -e "  $(GREEN)PASS$(RESET)  config exists ($$CFG)"; \
	   PASS=$$((PASS+1)); \
	 else \
	   echo -e "  $(RED)FAIL$(RESET)  config exists (run 'make setup')"; \
	   FAIL=$$((FAIL+1)); \
	 fi; \
	 if [ -z "$$CFG" ]; then \
	   echo -e "  $(YELLOW)SKIP$(RESET)  no config — placeholder check skipped"; \
	 elif grep -qE '$(WIZARD_PLACEHOLDERS)' "$$CFG" 2>/dev/null; then \
	   echo -e "  $(RED)FAIL$(RESET)  $$CFG has unsubstituted wizard placeholders"; \
	   FAIL=$$((FAIL+1)); \
	 else \
	   echo -e "  $(GREEN)PASS$(RESET)  $$CFG has no unsubstituted wizard placeholders"; \
	   PASS=$$((PASS+1)); \
	 fi; \
	 check "SenseVoiceSmall model.pt present (>200MB)" "[ $$(wc -c < $(SENSEVOICE_DIR)/model.pt 2>/dev/null || echo 0) -gt 209715200 ]"; \
	 check "SenseVoiceSmall tokenizer (chn_jpn_yue_eng_ko_spectok.bpe.model) present" "[ -s $(SENSEVOICE_DIR)/chn_jpn_yue_eng_ko_spectok.bpe.model ]"; \
	 check "models/piper/*.onnx exists" "ls $(PIPER_DIR)/*.onnx >/dev/null 2>&1"; \
	 check "docker compose config validates" "docker compose config --quiet"; \
	 XIAOZHI_HOST=$$(grep -oP 'ws://\K[0-9.]+' "$$CFG" 2>/dev/null | head -1); \
	 if [ -n "$$XIAOZHI_HOST" ]; then \
	   if curl -sf --max-time 3 "http://$$XIAOZHI_HOST:8003/xiaozhi/ota/" >/dev/null 2>&1; then \
	     echo -e "  $(GREEN)PASS$(RESET)  OTA endpoint reachable ($$XIAOZHI_HOST:8003)"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo -e "  $(RED)FAIL$(RESET)  OTA endpoint reachable ($$XIAOZHI_HOST:8003)"; \
	     FAIL=$$((FAIL+1)); \
	   fi; \
	   if curl -sf --max-time 3 "http://$$XIAOZHI_HOST:8081/health" >/dev/null 2>&1; then \
	     echo -e "  $(GREEN)PASS$(RESET)  Dashboard /health reachable ($$XIAOZHI_HOST:8081)"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo -e "  $(RED)FAIL$(RESET)  Dashboard /health reachable ($$XIAOZHI_HOST:8081)"; \
	     FAIL=$$((FAIL+1)); \
	   fi; \
	   if curl -sf --max-time 3 "http://$$XIAOZHI_HOST:8090/health" >/dev/null 2>&1; then \
	     echo -e "  $(GREEN)PASS$(RESET)  dotty-behaviour /health reachable ($$XIAOZHI_HOST:8090)"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo -e "  $(RED)FAIL$(RESET)  dotty-behaviour /health reachable ($$XIAOZHI_HOST:8090)"; \
	     FAIL=$$((FAIL+1)); \
	   fi; \
	 else \
	   echo -e "  $(YELLOW)SKIP$(RESET)  OTA / dashboard / dotty-behaviour (could not extract XIAOZHI_HOST from config)"; \
	 fi; \
	 echo ""; \
	 echo -e "$(BOLD)Results: $$PASS passed, $$FAIL failed.$(RESET)"; \
	 echo ""; \
	 if [ $$FAIL -gt 0 ]; then exit 1; fi

# ─────────────────────────────────────────────────────────────────────
# audit — verify "local except LLM" network claim
# ─────────────────────────────────────────────────────────────────────
audit: ## Audit outbound network connections (verify local-except-LLM claim)
	@echo ""
	@echo -e "$(BOLD)Network audit — verifying 'local except LLM' claim$(RESET)"
	@echo ""
	@if [ -f data/.config.yaml ]; then CFG=data/.config.yaml; \
	 elif [ -f .config.yaml ]; then CFG=.config.yaml; \
	 else CFG=""; fi; \
	 XIAOZHI_HOST=$$(grep -oP 'ws://\K[0-9.]+' "$$CFG" 2>/dev/null | head -1); \
	 PASS=0; FAIL=0; WARN=0; \
	 echo -e "$(BOLD)Server host (Docker):$(RESET)"; \
	 if [ -n "$$XIAOZHI_HOST" ]; then \
	   echo "  Checking outbound connections on $$XIAOZHI_HOST..."; \
	   CONNS=$$(ssh -o ConnectTimeout=5 root@$$XIAOZHI_HOST \
	     'ss -tnp | grep -v "127.0.0.1\|::1" | grep "ESTAB"' 2>/dev/null); \
	   if [ -z "$$CONNS" ]; then \
	     echo -e "  $(GREEN)PASS$(RESET)  No outbound connections (fully local)"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo "$$CONNS" | while read line; do echo "  $$line"; done; \
	     LLM=$$(echo "$$CONNS" | grep -cE "openrouter|cloudflare|anthropic" || true); \
	     OTHER=$$(echo "$$CONNS" | grep -cvE "openrouter|cloudflare|anthropic|tailscale|100\." || true); \
	     if [ "$$OTHER" -gt 0 ]; then \
	       echo -e "  $(RED)FAIL$(RESET)  Unexpected external connections detected"; \
	       FAIL=$$((FAIL+1)); \
	     else \
	       echo -e "  $(GREEN)PASS$(RESET)  Only LLM/Tailscale connections (expected)"; \
	       PASS=$$((PASS+1)); \
	     fi; \
	   fi; \
	 else \
	   echo -e "  $(YELLOW)SKIP$(RESET)  Could not extract server IP from config"; \
	   WARN=$$((WARN+1)); \
	 fi; \
	 echo ""; \
	 echo -e "$(BOLD)Docker container (no external mounts):$(RESET)"; \
	 MOUNTS=$$(docker compose exec -T xiaozhi-esp32-server mount 2>/dev/null | \
	   grep -v "overlay\|proc\|sys\|dev\|tmpfs\|cgroup\|mqueue\|shm" || true); \
	 if [ -z "$$MOUNTS" ]; then \
	   echo -e "  $(GREEN)PASS$(RESET)  No unexpected filesystem mounts"; \
	   PASS=$$((PASS+1)); \
	 else \
	   echo "$$MOUNTS" | while read line; do echo "  $$line"; done; \
	   echo -e "  $(YELLOW)WARN$(RESET)  Review mounts above"; \
	   WARN=$$((WARN+1)); \
	 fi; \
	 echo ""; \
	 echo -e "$(BOLD)Results: $$PASS passed, $$FAIL failed, $$WARN warnings.$(RESET)"; \
	 echo ""; \
	 if [ $$FAIL -gt 0 ]; then exit 1; fi

# ─────────────────────────────────────────────────────────────────────
# Docker shortcuts
# ─────────────────────────────────────────────────────────────────────
up: _preflight-compose _preflight-rendered ## Start containers (docker compose up -d)
	docker compose up -d

down: _preflight-compose _preflight-rendered ## Stop containers (docker compose down)
	docker compose down

logs: _preflight-compose _preflight-rendered ## Tail container logs (docker compose logs -f)
	docker compose logs -f

voice-list: ## List curated Piper voices (see docs/voice-catalog.md)
	@./scripts/voice-install.sh --list

voice-install: ## Install a curated Piper voice (VOICE=<key> [APPLY=1])
	@if [ -z "$(VOICE)" ]; then \
	  echo -e "$(RED)Error: VOICE is required.$(RESET)  Example: make voice-install VOICE=en_US-kristin-medium"; \
	  echo "Run 'make voice-list' to see the catalog."; \
	  exit 2; \
	fi
	@if [ -n "$(APPLY)" ]; then \
	  ./scripts/voice-install.sh "$(VOICE)" --apply; \
	else \
	  ./scripts/voice-install.sh "$(VOICE)"; \
	fi

# ─────────────────────────────────────────────────────────────────────
# verify-firmware — build + checksum, optionally diff against published
# ─────────────────────────────────────────────────────────────────────
verify-firmware: ## Build firmware in IDF container and compute SHA256 checksums
	@echo ""
	@echo -e "$(BOLD)Firmware reproducibility check$(RESET)"
	@echo ""
	@if ! command -v docker >/dev/null 2>&1; then \
	  echo -e "$(RED)Error: docker is required.$(RESET)"; exit 1; \
	fi
	@if [ ! -f firmware/firmware/CMakeLists.txt ]; then \
	  echo -e "$(RED)Error: firmware submodule not initialised.$(RESET)"; \
	  echo "Run: git submodule update --init --recursive"; \
	  exit 1; \
	fi
	@echo -e "$(BOLD)Fetching firmware deps...$(RESET)"
	docker run --rm -v "$(PWD)/firmware/firmware:/project" -w /project \
	  espressif/idf:v5.5.4 \
	  bash -lc 'git config --global --add safe.directory "*" && python fetch_repos.py'
	@echo -e "$(BOLD)Building firmware...$(RESET)"
	docker run --rm -v "$(PWD)/firmware/firmware:/project" -w /project \
	  espressif/idf:v5.5.4 \
	  bash -lc 'git config --global --add safe.directory "*" && idf.py build'
	@echo -e "$(BOLD)Computing checksums...$(RESET)"
	@sha256sum \
	  firmware/firmware/build/stack-chan.bin \
	  firmware/firmware/build/ota_data_initial.bin \
	  firmware/firmware/build/generated_assets.bin \
	  | tee firmware/firmware/build/SHA256SUMS.txt
	@echo ""
	@if [ -f firmware/firmware/build/SHA256SUMS.published ]; then \
	  echo -e "$(BOLD)Comparing against published checksums...$(RESET)"; \
	  if diff -q firmware/firmware/build/SHA256SUMS.published \
	             firmware/firmware/build/SHA256SUMS.txt >/dev/null 2>&1; then \
	    echo -e "$(GREEN)PASS$(RESET)  Build is reproducible."; \
	  else \
	    echo -e "$(RED)FAIL$(RESET)  Checksums differ:"; \
	    diff firmware/firmware/build/SHA256SUMS.published \
	         firmware/firmware/build/SHA256SUMS.txt; \
	    exit 1; \
	  fi; \
	else \
	  echo -e "$(YELLOW)NOTE$(RESET)  No published SHA256SUMS.published to compare against."; \
	  echo "  To verify a release, download SHA256SUMS.txt from GitHub Releases,"; \
	  echo "  save it as firmware/firmware/build/SHA256SUMS.published, and re-run."; \
	fi
	@echo ""

status: _preflight-compose _preflight-rendered ## Show container status + bridge / dotty-behaviour health
	@docker compose ps
	@echo ""
	@if [ -f data/.config.yaml ]; then CFG=data/.config.yaml; \
	 elif [ -f .config.yaml ]; then CFG=.config.yaml; \
	 else CFG=""; fi; \
	 XIAOZHI_HOST=$$(grep -oP 'ws://\K[0-9.]+' "$$CFG" 2>/dev/null | head -1); \
	 if [ -n "$$XIAOZHI_HOST" ]; then \
	   echo -n "Dashboard health ($$XIAOZHI_HOST:8081): "; \
	   curl -sf --max-time 3 "http://$$XIAOZHI_HOST:8081/health" && echo "" || \
	     echo -e "$(YELLOW)unreachable$(RESET)"; \
	   echo -n "dotty-behaviour health ($$XIAOZHI_HOST:8090): "; \
	   curl -sf --max-time 3 "http://$$XIAOZHI_HOST:8090/health" && echo "" || \
	     echo -e "$(YELLOW)unreachable$(RESET)"; \
	 else \
	   echo -e "Service health: $(YELLOW)could not extract XIAOZHI_HOST from config$(RESET)"; \
	 fi
