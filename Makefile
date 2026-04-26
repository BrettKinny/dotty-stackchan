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

# ── Targets ──────────────────────────────────────────────────────────
.PHONY: help setup fetch-models doctor audit up down logs status voice-list voice-install sbom verify-firmware

help: ## Show this help
	@echo ""
	@echo -e "$(BOLD)Dotty$(RESET) — your self-hosted StackChan robot assistant"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(BOLD)%-15s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ─────────────────────────────────────────────────────────────────────
# setup — interactive first-run wizard
# ─────────────────────────────────────────────────────────────────────
setup: ## Interactive first-run wizard (prompts for IPs, names, timezone)
	@echo ""
	@echo -e "$(BOLD)Dotty setup wizard$(RESET)"
	@echo "This will substitute placeholders in config files and start the stack."
	@echo ""
	@read -rp "UNRAID_IP  (LAN IP of Docker host, e.g. 192.168.1.10): " UNRAID_IP && \
	 read -rp "RPI_IP     (LAN IP of Raspberry Pi,  e.g. 192.168.1.20): " RPI_IP && \
	 read -rp "RPI_USER   (SSH user on the Pi,       e.g. dietpi):       " RPI_USER && \
	 read -rp "ROBOT_NAME (name the robot calls itself) [Dotty]:          " ROBOT_NAME && \
	 ROBOT_NAME=$${ROBOT_NAME:-Dotty} && \
	 read -rp "YOUR_NAME  (your name / org,           e.g. Brett):       " YOUR_NAME && \
	 read -rp "Timezone   (TZ identifier,             e.g. Australia/Brisbane): " TZ_VALUE && \
	 echo "" && \
	 if [ -z "$$UNRAID_IP" ] || [ -z "$$RPI_IP" ] || [ -z "$$RPI_USER" ] || \
	    [ -z "$$ROBOT_NAME" ] || [ -z "$$YOUR_NAME" ] || [ -z "$$TZ_VALUE" ]; then \
	   echo -e "$(RED)Error: all fields are required.$(RESET)"; exit 1; \
	 fi && \
	 echo -e "$(BOLD)Substituting placeholders...$(RESET)" && \
	 for f in .config.yaml docker-compose.yml zeroclaw-bridge.service; do \
	   if [ -f "$$f" ]; then \
	     sed -i "s|<UNRAID_IP>|$$UNRAID_IP|g"   "$$f"; \
	     sed -i "s|<RPI_IP>|$$RPI_IP|g"         "$$f"; \
	     sed -i "s|<RPI_USER>|$$RPI_USER|g"     "$$f"; \
	     sed -i "s|<ROBOT_NAME>|$$ROBOT_NAME|g; s|You are Dotty,|You are $$ROBOT_NAME,|g" "$$f"; \
	     sed -i "s|<YOUR_NAME>|$$YOUR_NAME|g"   "$$f"; \
	     echo "  $$f — done"; \
	   else \
	     echo -e "  $(YELLOW)$$f — not found, skipping$(RESET)"; \
	   fi; \
	 done && \
	 if [ -f docker-compose.yml ]; then \
	   sed -i "s|TZ=.*|TZ=$$TZ_VALUE|g" docker-compose.yml; \
	   echo "  docker-compose.yml — timezone set to $$TZ_VALUE"; \
	 fi && \
	 echo "" && \
	 $(MAKE) fetch-models && \
	 echo "" && \
	 echo -e "$(BOLD)Starting containers...$(RESET)" && \
	 docker compose up -d && \
	 echo "" && \
	 echo -e "$(GREEN)$(BOLD)Setup complete.$(RESET)" && \
	 echo "" && \
	 echo "Next steps:" && \
	 echo "  1. Flash the StackChan firmware (see SETUP.md or m5stack/StackChan repo)." && \
	 echo "  2. In the device's Advanced Options, set the OTA URL to:" && \
	 echo "       http://$$UNRAID_IP:8003/xiaozhi/ota/" && \
	 echo "  3. Deploy zeroclaw-bridge.service to the RPi and start it." && \
	 echo "  4. Run 'make doctor' to verify everything is healthy." && \
	 echo ""

# ─────────────────────────────────────────────────────────────────────
# fetch-models — download ASR + TTS model files
# ─────────────────────────────────────────────────────────────────────
SENSEVOICE_FILES := model.pt config.yaml tokens.json configuration.json am.mvn chn_jpn_yue_eng_ko_spectral.fbank.conf.yaml
SENSEVOICE_DIR   := models/SenseVoiceSmall
PIPER_DIR        := models/piper

fetch-models: ## Download SenseVoiceSmall + Piper voice models
	@echo ""
	@echo -e "$(BOLD)Fetching models...$(RESET)"
	@echo ""
	@# ── SenseVoiceSmall ──
	@mkdir -p $(SENSEVOICE_DIR)
	@echo -e "$(BOLD)[SenseVoiceSmall]$(RESET)"
	@for f in $(SENSEVOICE_FILES); do \
	  if [ -f "$(SENSEVOICE_DIR)/$$f" ]; then \
	    echo -e "  $(GREEN)$$f — already exists, skipping$(RESET)"; \
	  else \
	    echo "  Downloading $$f ..."; \
	    curl -# -L -o "$(SENSEVOICE_DIR)/$$f" \
	      "$(SENSEVOICE_REPO)/resolve/main/$$f" || \
	      { echo -e "  $(RED)Failed to download $$f$(RESET)"; exit 1; }; \
	  fi; \
	done
	@echo ""
	@# ── Piper voice ──
	@mkdir -p $(PIPER_DIR)
	@echo -e "$(BOLD)[Piper TTS — $(PIPER_ONNX)]$(RESET)"
	@if [ -f "$(PIPER_DIR)/$(PIPER_ONNX)" ]; then \
	  echo -e "  $(GREEN)$(PIPER_ONNX) — already exists, skipping$(RESET)"; \
	else \
	  echo "  Downloading $(PIPER_ONNX) (this is ~75 MB)..."; \
	  curl -# -L -o "$(PIPER_DIR)/$(PIPER_ONNX)" \
	    "$(PIPER_BASE)/$(PIPER_ONNX)" || \
	    { echo -e "  $(RED)Failed to download $(PIPER_ONNX)$(RESET)"; exit 1; }; \
	fi
	@if [ -f "$(PIPER_DIR)/$(PIPER_JSON)" ]; then \
	  echo -e "  $(GREEN)$(PIPER_JSON) — already exists, skipping$(RESET)"; \
	else \
	  echo "  Downloading $(PIPER_JSON)..."; \
	  curl -# -L -o "$(PIPER_DIR)/$(PIPER_JSON)" \
	    "$(PIPER_BASE)/$(PIPER_JSON)" || \
	    { echo -e "  $(RED)Failed to download $(PIPER_JSON)$(RESET)"; exit 1; }; \
	fi
	@echo ""
	@# ── faster-whisper small.en (CTranslate2) ──
	@mkdir -p $(WHISPER_DIR)
	@echo -e "$(BOLD)[faster-whisper small.en]$(RESET)"
	@for f in $(WHISPER_FILES); do \
	  if [ -f "$(WHISPER_DIR)/$$f" ]; then \
	    echo -e "  $(GREEN)$$f — already exists, skipping$(RESET)"; \
	  else \
	    echo "  Downloading $$f ..."; \
	    curl -# -L -o "$(WHISPER_DIR)/$$f" \
	      "$(WHISPER_REPO)/resolve/main/$$f" || \
	      { echo -e "  $(RED)Failed to download $$f$(RESET)"; exit 1; }; \
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
# ─────────────────────────────────────────────────────────────────────
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
	 check ".config.yaml exists" "test -f .config.yaml"; \
	 if grep -qE '<[A-Z_]+>' .config.yaml 2>/dev/null; then \
	   echo -e "  $(RED)FAIL$(RESET)  .config.yaml has no unsubstituted placeholders"; \
	   FAIL=$$((FAIL+1)); \
	 else \
	   echo -e "  $(GREEN)PASS$(RESET)  .config.yaml has no unsubstituted placeholders"; \
	   PASS=$$((PASS+1)); \
	 fi; \
	 check "models/SenseVoiceSmall/ has files" "ls $(SENSEVOICE_DIR)/*.pt >/dev/null 2>&1 || ls $(SENSEVOICE_DIR)/*.yaml >/dev/null 2>&1"; \
	 check "models/piper/*.onnx exists" "ls $(PIPER_DIR)/*.onnx >/dev/null 2>&1"; \
	 check "docker compose config validates" "docker compose config --quiet"; \
	 UNRAID_IP=$$(grep -oP 'ws://\K[0-9.]+' .config.yaml 2>/dev/null | head -1); \
	 RPI_IP=$$(grep -oP 'url: http://\K[0-9.]+' .config.yaml 2>/dev/null | head -1); \
	 if [ -n "$$UNRAID_IP" ]; then \
	   if curl -sf --max-time 3 "http://$$UNRAID_IP:8003/xiaozhi/ota/" >/dev/null 2>&1; then \
	     echo -e "  $(GREEN)PASS$(RESET)  OTA endpoint reachable ($$UNRAID_IP:8003)"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo -e "  $(RED)FAIL$(RESET)  OTA endpoint reachable ($$UNRAID_IP:8003)"; \
	     FAIL=$$((FAIL+1)); \
	   fi; \
	 else \
	   echo -e "  $(YELLOW)SKIP$(RESET)  OTA endpoint (could not extract UNRAID_IP from config)"; \
	 fi; \
	 if [ -n "$$RPI_IP" ]; then \
	   if curl -sf --max-time 3 "http://$$RPI_IP:8080/health" >/dev/null 2>&1; then \
	     echo -e "  $(GREEN)PASS$(RESET)  Bridge /health reachable ($$RPI_IP:8080)"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo -e "  $(RED)FAIL$(RESET)  Bridge /health reachable ($$RPI_IP:8080)"; \
	     FAIL=$$((FAIL+1)); \
	   fi; \
	 else \
	   echo -e "  $(YELLOW)SKIP$(RESET)  Bridge /health (could not extract RPI_IP from config)"; \
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
	@UNRAID_IP=$$(grep -oP 'ws://\K[0-9.]+' .config.yaml 2>/dev/null | head -1); \
	 RPI_IP=$$(grep -oP 'url: http://\K[0-9.]+' .config.yaml 2>/dev/null | head -1); \
	 PASS=0; FAIL=0; WARN=0; \
	 echo -e "$(BOLD)Server host (Docker):$(RESET)"; \
	 if [ -n "$$UNRAID_IP" ]; then \
	   echo "  Checking outbound connections on $$UNRAID_IP..."; \
	   CONNS=$$(ssh -o ConnectTimeout=5 root@$$UNRAID_IP \
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
	 echo -e "$(BOLD)Bridge host (RPi):$(RESET)"; \
	 if [ -n "$$RPI_IP" ]; then \
	   echo "  Checking outbound connections on $$RPI_IP..."; \
	   CONNS=$$(ssh -o ConnectTimeout=5 dietpi@$$RPI_IP \
	     'ss -tnp | grep -v "127.0.0.1\|::1" | grep "ESTAB"' 2>/dev/null); \
	   if [ -z "$$CONNS" ]; then \
	     echo -e "  $(GREEN)PASS$(RESET)  No outbound connections"; \
	     PASS=$$((PASS+1)); \
	   else \
	     echo "$$CONNS" | while read line; do echo "  $$line"; done; \
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
	   echo -e "  $(YELLOW)SKIP$(RESET)  Could not extract bridge IP from config"; \
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
up: ## Start containers (docker compose up -d)
	docker compose up -d

down: ## Stop containers (docker compose down)
	docker compose down

logs: ## Tail container logs (docker compose logs -f)
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

status: ## Show container status + bridge health
	@docker compose ps
	@echo ""
	@RPI_IP=$$(grep -oP 'url: http://\K[0-9.]+' .config.yaml 2>/dev/null | head -1); \
	 if [ -n "$$RPI_IP" ]; then \
	   echo -n "Bridge health ($$RPI_IP:8080): "; \
	   curl -sf --max-time 3 "http://$$RPI_IP:8080/health" && echo "" || \
	     echo -e "$(YELLOW)unreachable$(RESET)"; \
	 else \
	   echo -e "Bridge health: $(YELLOW)could not extract RPI_IP from config$(RESET)"; \
	 fi
