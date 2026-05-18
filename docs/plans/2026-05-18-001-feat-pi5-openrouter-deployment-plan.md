---
title: "feat: Deploy Dotty StackChan all-in-one on Raspberry Pi 5 with OpenRouter cloud LLM"
status: active
depth: standard
created: 2026-05-18
type: feat
---

# feat: Deploy Dotty StackChan all-in-one on Raspberry Pi 5 with OpenRouter cloud LLM

## Summary

Stand up the Dotty StackChan voice stack on a dedicated Raspberry Pi 5 (8/16GB, SD-card V1) running Pi OS Lite 64-bit. The all-in-one Docker compose runs xiaozhi-server + the FastAPI bridge; voice LLM goes directly to OpenRouter via the `openai_compat` custom provider (`openrouter/free` model) — no ZeroClaw runtime, no local model weights. Bridge stays running for the dashboard, perception consumers, and `brain.db` memory, but its ZeroClaw paths are neutralized via a stub binary plus `DOTTY_VOICE_PROVIDER=tier1slim`. Dashboard is exposed at `https://dotty.pcar.me` through a native `cloudflared` systemd service gated by a Cloudflare Access policy. The StackChan hardware (M5Stack CoreS3 + servo kit) is on hand but not flashed — firmware is built from the active dotty fork (`BrettKinny/StackChan @ dotty`) using the Espressif IDF container, then flashed over USB-C, WiFi-onboarded, and pointed at the Pi's OTA endpoint.

---

## Problem Frame

The user wants Dotty working end-to-end with the project's custom firmware and tooling, deployed on a dedicated, isolated host. The Synology DS723+ that owns the repo is unsuitable — DSM's userspace makes any native binary install painful, and the existing 30+ container infrastructure (DockFlare, frontend/backend networks, Authentik) is over-engineered for what should be a focused robot brain. A clean Raspberry Pi 5 dedicated to the voice stack delivers true isolation, comfortably exceeds the upstream xiaozhi-esp32-server hardware minimum (2c/4GB for FunASR), and matches the project's own envisioned shape (the repo's README explicitly suggests a Pi as a low-power always-on host for the bridge half of the multi-host architecture).

The user is comfortable with cloud LLM (one outbound call leaving the LAN), which collapses the deployment substantially: no ZeroClaw native binary functionally needed, no local model weights, no GPU. The remaining work is OS provisioning, container bring-up, firmware build/flash, and tunnel/auth wiring.

---

## Requirements

**R1.** Voice round-trip works end-to-end: StackChan → xiaozhi-server (ASR) → OpenRouter (LLM) → xiaozhi-server (TTS) → StackChan, with audio output and the emoji-prefix face animation firing correctly.

**R2.** LLM provider is `OpenAICompat` pointed at `https://openrouter.ai/api/v1`, model `openrouter/free`. The user's OpenRouter API key is read from a host environment file, not committed.

**R3.** Bridge dashboard reachable at `https://dotty.pcar.me/ui` via Cloudflare Tunnel, gated by a Cloudflare Access policy (no DockFlare, no Authentik forward-auth).

**R4.** Perception consumers in bridge.py work: face-detected → "Hi!" greeting, sound-direction → head turn, face-lost → TTS abort.

**R5.** StackChan firmware is built from the active dotty fork (`BrettKinny/StackChan @ dotty` branch) and flashed onto the device, NOT the version pinned in `firmware/firmware/` submodule — the user needs Phase 4 (StateManager + LEDs), Phase 5 (sleep), and Phase 6 (security) behaviour that the submodule pin lacks.

**R6.** Kid mode is on by default (project default).

**R7.** The deployment survives a Pi reboot — all containers, cloudflared, and any systemd units come back automatically.

**R8.** Pre-flight checks documented in `SETUP.md` pass before flashing the StackChan: OTA endpoint responds at `http://<PI_LAN_IP>:8003/xiaozhi/ota/`, bridge health responds at `http://<PI_LAN_IP>:8080/health`.

---

## Scope Boundaries

### In scope

- Pi 5 OS provisioning (Pi OS Lite 64-bit, headless, SSH access)
- Docker Engine install
- Native `cloudflared` install + systemd service on the Pi (NOT containerized)
- Repo clone + hand-edited `data/.config.yaml` (skipping `make setup` wizard — its prompts target the multi-host Tier1Slim default)
- Compose adapted for OpenRouter via an override file (`compose.openrouter.override.yml`): adds `DOTTY_VOICE_PROVIDER=tier1slim` on the bridge service and `OPENROUTER_API_KEY=${OPENROUTER_API_KEY}` on xiaozhi-server. `compose.all-in-one.yml` itself stays unmodified.
- Zeroclaw stub script (no-op shell script at `/root/.cargo/bin/zeroclaw` on the Pi) to satisfy the existing compose bind-mounts without installing the real binary
- Cloudflare Tunnel + Access policy setup (tunnel creation via `cloudflared tunnel`, Access policy via Cloudflare Zero Trust dashboard)
- StackChan firmware build via Espressif IDF Docker container against the active fork
- USB-C flash + first-boot WiFi onboarding + OTA URL config
- End-to-end smoke test via `scripts/dotty_doctor.py` + a real voice turn

### Deferred to Follow-Up Work

- **NVMe HAT storage upgrade.** SD-card V1 is acceptable short-term (model files are read-mostly after initial download, but `brain.db` writes on every voice turn and xiaozhi audio chunks write to `./tmp/`). Plan an NVMe HAT (~$25-$50) before daily-driving the deployment for months.
- **Voice tools** (`memory_lookup`, `take_photo`, `think_hard`, `play_song`). These require either Tier1Slim+llama-swap (local GPU) or ZeroClaw (native binary + agent config). The current OpenAICompat provider doesn't define tools in the OpenAI Chat Completions request, so the LLM can never emit tool calls; the tool handlers in `bridge.py` are dormant. Adding them later means either bolting tool definitions onto `openai_compat.py` or migrating to Tier1Slim with a remote llama-swap.
- **Smart mode flip.** Smart mode switches the live LLM provider between a default and a "smart" model. Under `DOTTY_VOICE_PROVIDER=tier1slim` the flip is in-process via `/xiaozhi/admin/set-tier1slim-model` — but that path only works when Tier1Slim is the selected LLM. With OpenAICompat, smart-mode requests would be no-ops. Acceptable for V1; revisit if the user wants the smart-mode toggle to do something.
- **Upgrade to a paid model.** OpenRouter free-router has 200 req/day + 20 req/min rate limits and variable emoji-prefix adherence. If quality or rate-limit pressure hits, swap `model: openrouter/free` → `anthropic/claude-haiku-4.5` in `data/.config.yaml` (one line, restart the container, no rebuild).
- **Pre-flight Phase 4 hardware verification** ([#38](https://github.com/BrettKinny/dotty-stackchan/issues/38), [#39](https://github.com/BrettKinny/dotty-stackchan/issues/39), [#40](https://github.com/BrettKinny/dotty-stackchan/issues/40)). The fork commits ship Phase 4-6 firmware but bench checks are tracked in those issues. Out of this deployment's scope — flashing the firmware is enough; visual / interactive verification of every LED state and behaviour belongs in firmware QA.

### Out of scope

- Multi-host split (xiaozhi-server on Pi, bridge on a different machine)
- Any local LLM (Ollama, llama-swap, llama.cpp)
- DockFlare label-driven tunnel/auth (the user explicitly opted out)
- Authentik forward-auth middleware in the request path
- Synology-conventions adaptation (frontend/backend networks, port-range allocation, DockFlare labels)
- Custom persona authoring (default `personas/default.md` is fine for V1)
- Voice catalog work (default `en_US-kristin-medium` Piper voice)
- Dance/song MIDI tooling, singing-piper renderers (separate post-V1 work)
- Backup automation for `brain.db` and `.wizard.env`

---

## Key Technical Decisions

### KD1. Skip the `make setup` wizard; hand-render `data/.config.yaml`

The wizard's prompts target the multi-host Tier1Slim default (it asks for `XIAOZHI_HOST`, `ZEROCLAW_HOST`, `ZEROCLAW_USER`, `LLAMA_SWAP_URL`, `LLAMA_SWAP_KEY`, `LLAMA_SWAP_MODEL`) and renders `docker-compose.yml.template` (multi-host) and `zeroclaw-bridge.service.template` (systemd-on-other-host) — neither of which we use. The `OpenAICompat` config block placeholders (`<OPENAI_COMPAT_URL>`, `<OPENAI_COMPAT_API_KEY>`, `<OPENAI_COMPAT_MODEL>`) are NOT in the wizard's substitution set anyway.

Plan: copy `.config.yaml.template` to `data/.config.yaml` and hand-edit the `OpenAICompat` block + `selected_module.LLM: OpenAICompat` directly. Reference: the cookbook pattern at `docs/cookbook/run-fully-local.md` shows the same shape for Ollama.

### KD2. Use the zeroclaw-stub workaround instead of installing ZeroClaw natively

`compose.all-in-one.yml` lines 117-124 bind-mount `/root/.cargo/bin/zeroclaw` (binary) and `/root/.zeroclaw/` (config) from host to container as required read-only mounts. Docker fails to start the container if these don't exist on the host.

Three options were considered:
1. **Install zeroclaw via `cargo install` on the Pi.** ~15 min cargo build on arm64, adds Rust toolchain (~500MB), gives full ZeroClaw functionality the deployment doesn't use.
2. **Modify compose to remove the bind-mounts.** Edits a tracked file; cleaner but more invasive.
3. **Stub the binary with a no-op shell script + set `DOTTY_VOICE_PROVIDER=tier1slim` on the bridge.** One file on the host (`/root/.cargo/bin/zeroclaw` = `#!/bin/bash\nexec sleep infinity`), one env var, no tracked-file edits.

Chose option 3. Bridge.py has graceful error handling for ZeroClaw failures (line 4845, `FileNotFoundError → log.exception → friendly fallback`), and `DOTTY_VOICE_PROVIDER=tier1slim` (documented in changelog entry for commit `e2930ce`) keeps the bridge off the ZeroClaw code paths for model swaps and tool routing. With `selected_module.LLM: OpenAICompat`, xiaozhi-server bypasses the bridge entirely for the LLM path, so ACP is never invoked.

### KD3. Native `cloudflared` systemd service on the Pi, not containerized

The all-in-one compose doesn't own the tunnel lifecycle — the tunnel needs to be up before the bridge is reachable and should survive container restarts. Native systemd (`cloudflared service install <TUNNEL_TOKEN>`) is the canonical Cloudflare-supported install on Debian/arm64. Tunnel config (`~/.cloudflared/config.yml`) routes `dotty.pcar.me` → `http://localhost:8080`.

The Cloudflare Access policy on the hostname is configured separately in the Zero Trust dashboard, not via tunnel config — it gates inbound requests at Cloudflare's edge before they hit the tunnel.

### KD4. OpenRouter free-router as V1 default; Claude Haiku 4.5 as documented upgrade

`openrouter/free` selects free models at random with smart feature-filtering. $0 cost, 200k context, but ~200 req/day + ~20 req/min rate limits and variable emoji-prefix adherence across the free model pool.

The bridge's `_ensure_emoji_prefix` fallback covers missing-emoji cases (face defaults to 😐), and `custom-providers/openai_compat/openai_compat.py` does the same enforcement in-stream — so a non-conforming free model still yields a working voice turn, just with less expressive face animation.

Documented upgrade path: change one line in `data/.config.yaml` (`model: anthropic/claude-haiku-4.5`), `docker compose restart xiaozhi-server`. No rebuild.

### KD5. Firmware build from the active dotty fork (not the submodule)

The repo's `firmware/firmware/` submodule pin deliberately lags the active `BrettKinny/StackChan @ dotty` branch — it exists for reproducible release-tagged firmware, not for development bring-up. The active fork has Phase 4 StateManager / LEDs (commit `d78118b`, 2026-04-27), Phase 5 sleep behaviour, and Phase 6 security behaviour shipped; the submodule pin has none of those.

Plan: clone the active fork separately on the Pi (or a workstation with USB-C access to the StackChan), run the IDF container build per the recipe in repo-level `CLAUDE.md`, and flash. Do NOT bump the submodule pin as part of this deployment.

---

## Output Structure

What lands on the Pi at `~/dotty-stackchan/` after deployment, in addition to the repo checkout:

```text
~/dotty-stackchan/                       # repo clone
├── compose.all-in-one.yml               # unmodified upstream; zeroclaw bind-mounts satisfied by host-side stub binary
├── compose.openrouter.override.yml      # new file (Pi-only, not committed); overlays env vars for OpenRouter + DOTTY_VOICE_PROVIDER
├── data/
│   └── .config.yaml                     # hand-rendered from .config.yaml.template (OpenAICompat block filled)
├── models/
│   ├── SenseVoiceSmall/                 # ~1.5GB, downloaded by `make fetch-models`
│   └── piper/                           # ~63MB per voice
├── tmp/                                 # xiaozhi-server audio buffer (SD card wear concern)
└── .env                                 # OPENROUTER_API_KEY, not committed

/root/.cargo/bin/zeroclaw                # no-op stub (KD2)
/root/.zeroclaw/                         # empty dir or minimal config.toml stub

/etc/systemd/system/cloudflared.service  # installed by `cloudflared service install <TOKEN>`
~/.cloudflared/                          # tunnel credentials + config.yml
```

---

## Implementation Units

### U1. Provision Pi 5 + base OS

**Goal:** Get the Pi 5 to a state where the dotty-stackchan repo can be cloned and `docker compose up` is one command away.

**Requirements:** R7 (boot survival)

**Dependencies:** None

**Files:** None in this repo. Operator runs commands on the Pi directly.

**Approach:**
- Image Pi OS Lite 64-bit (Bookworm or current stable) onto SD card via Raspberry Pi Imager. Pre-configure: hostname `dotty`, SSH enabled, WiFi credentials, user `dotty` with SSH pubkey, locale + timezone.
- First-boot SSH in, run `sudo apt update && sudo apt full-upgrade -y`.
- Install Docker Engine via the official convenience script (`curl -fsSL https://get.docker.com | sudo sh`), then add `dotty` user to the `docker` group and re-login.
- Install build essentials: `git`, `make`, `curl`, `jq`. Pi OS includes Python 3 by default.
- Set a high-endurance SD card recommendation in the operator notes — SanDisk High Endurance or Samsung PRO Endurance for `brain.db` write longevity. NVMe HAT upgrade tracked as deferred.
- Verify Docker works: `docker run --rm hello-world`.

**Patterns to follow:** Standard Pi OS Lite + Docker install. No project-specific patterns apply at this layer.

**Test scenarios:**
- `ssh dotty@<PI_IP>` succeeds with pubkey, no password prompt.
- `docker run --rm hello-world` exits 0.
- `timedatectl` shows the configured timezone.
- `Test expectation: none -- system provisioning, verified by smoke commands above`

**Verification:** Operator can `ssh dotty@<PI_IP>`, run `docker version`, and see Docker Engine reporting `linux/arm64` platform.

---

### U2. Clone repo + zeroclaw stub + render `data/.config.yaml`

**Goal:** Repo is present on the Pi, the zeroclaw mount points exist (stubbed), and `data/.config.yaml` is configured for OpenRouter.

**Requirements:** R2, R6

**Dependencies:** U1

**Files (on Pi, not committed):**
- `~/dotty-stackchan/` — clone of this repo, branch `main`
- `~/dotty-stackchan/data/.config.yaml` — copied from `.config.yaml.template` and hand-edited
- `~/dotty-stackchan/.env` — contains `OPENROUTER_API_KEY=<user's key>`; `.gitignore`'d
- `/root/.cargo/bin/zeroclaw` — no-op shell script:
  ```bash
  #!/bin/bash
  # Stub: real ZeroClaw is not installed. Bridge bypasses ACP via DOTTY_VOICE_PROVIDER=tier1slim.
  exec sleep infinity
  ```
- `/root/.zeroclaw/` — empty directory (satisfies the second compose bind-mount)

**Approach:**
- `git clone https://github.com/BrettKinny/dotty-stackchan ~/dotty-stackchan`
- Create the stub binary: `sudo mkdir -p /root/.cargo/bin /root/.zeroclaw && sudo tee /root/.cargo/bin/zeroclaw <<'EOF' ... && sudo chmod +x /root/.cargo/bin/zeroclaw`
- `cp .config.yaml.template data/.config.yaml`
- Hand-edit `data/.config.yaml`:
  - Replace `<XIAOZHI_HOST>` (line 5, the `websocket: ws://<XIAOZHI_HOST>:8000/xiaozhi/v1/` line) with the Pi's LAN IP — this URL is served back to the StackChan via the OTA response and is the device's WebSocket destination. Leaving the literal placeholder breaks R1 at the WS handshake.
  - Change `selected_module.LLM: Tier1Slim` → `selected_module.LLM: OpenAICompat` (line 17 region)
  - In the `LLM.OpenAICompat:` block (lines 133+):
    - `url: https://openrouter.ai/api/v1`
    - `api_key: ${OPENROUTER_API_KEY}` (placeholder; the container reads from env — verified at U3 pre-flight)
    - `model: openrouter/free`
    - Leave `persona_file: personas/default.md`, `max_tokens: 256`, `temperature: 0.7`, `timeout: 60` as defaults
  - In the top-level `prompt:` block (line 27 region), replace `<ROBOT_NAME>` placeholder with `Dotty` if not already substituted
  - The CUDA-block markers live in `docker-compose.yml.template` (which we don't use — we use `compose.all-in-one.yml`), not in `.config.yaml.template`. Nothing to strip from `data/.config.yaml`.
- Create `~/dotty-stackchan/.env`:
  ```text
  OPENROUTER_API_KEY=sk-or-v1-<user's-actual-key>
  ```
- `make fetch-models` — downloads SenseVoiceSmall (~1.5GB) and Piper voice files into `models/`

**Patterns to follow:**
- The OpenAICompat config-block shape is documented in `docs/cookbook/run-fully-local.md` (just replace Ollama URL with OpenRouter)
- Template substitution pattern in the Makefile setup target (`Makefile` lines 95-200) — we do the substitutions by hand rather than running the wizard

**Test scenarios:**
- `yq '.selected_module.LLM' data/.config.yaml` outputs `OpenAICompat`
- `yq '.LLM.OpenAICompat.url' data/.config.yaml` outputs `https://openrouter.ai/api/v1`
- `yq '.LLM.OpenAICompat.model' data/.config.yaml` outputs `openrouter/free`
- `/root/.cargo/bin/zeroclaw &` runs and stays running indefinitely; `kill %1` cleans up
- `ls /root/.zeroclaw/` succeeds (directory exists)
- `ls models/SenseVoiceSmall/model.pt models/piper/*.onnx` shows both downloaded
- `Test expectation: none -- config file content is verified by yq checks above; no behavioural code change in this unit`

**Verification:** The five files/dirs above exist with correct content; `cat ~/dotty-stackchan/.env | grep OPENROUTER_API_KEY` succeeds and the key matches the operator's OpenRouter dashboard.

---

### U3. Adapt `compose.all-in-one.yml` for OpenRouter + bring up the stack

**Goal:** Both `xiaozhi-server` and `zeroclaw-bridge` containers are running on the Pi. Voice path works (xiaozhi → OpenRouter → xiaozhi); bridge serves the dashboard at `localhost:8080/ui`.

**Requirements:** R1, R2, R4

**Dependencies:** U2

**Files:**
- `compose.openrouter.override.yml` (new file on the Pi, not committed) — layers on top of `compose.all-in-one.yml`. Adds env vars **and** the volume mounts the all-in-one compose omits but the canonical multi-host `docker-compose.yml.template` has (the all-in-one is missing the `openai_compat` LLM provider mount + the four `xiaozhi-patches` overrides that deliver the `/xiaozhi/admin/*` admin routes and the perception-event relay).
- `compose.all-in-one.yml` is left unmodified — keeps the upstream-merge surface clean. The existing zeroclaw bind-mounts are satisfied by the stub binary created in U2.

**Approach:**
- Write `compose.openrouter.override.yml` (mount paths sourced from the multi-host `docker-compose.yml.template` lines 41, 55-59):
  ```yaml
  services:
    xiaozhi-server:
      environment:
        - OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
        # Bridge endpoint that xiaozhi-server's EventTextMessageHandler POSTs perception
        # events to (face_detected, face_lost, sound_event, state_changed). Multi-host
        # template sets these to the bridge host's LAN IP; in the all-in-one, use the
        # docker compose service DNS name.
        - BRIDGE_URL=http://bridge:8080
        - VISION_BRIDGE_URL=http://bridge:8080
      volumes:
        # OpenAICompat LLM provider — the base all-in-one only mounts the zeroclaw provider.
        - ./custom-providers/openai_compat:/opt/xiaozhi-esp32-server/core/providers/llm/openai_compat
        # Project-custom textUtils.py — openai_compat imports ALLOWED_EMOJIS / FALLBACK_EMOJI /
        # build_turn_suffix from here. Not in the upstream image's core/utils/textUtils.py.
        - ./custom-providers/textUtils.py:/opt/xiaozhi-esp32-server/core/utils/textUtils.py:ro
        # Persona directory — OpenAICompat's persona_file: personas/default.md loads from here.
        - ./personas:/opt/xiaozhi-esp32-server/personas:ro
        # xiaozhi-server patches — `/xiaozhi/admin/*` admin routes (toggle, kid-mode, smart-mode,
        # play-asset, songs, inject-text, set-head-angles) and the EventTextMessageHandler that
        # POSTs perception events to bridge `/api/perception/event`.
        - ./custom-providers/xiaozhi-patches/portal_bridge.py:/opt/xiaozhi-esp32-server/core/portal_bridge.py:ro
        - ./custom-providers/xiaozhi-patches/websocket_server.py:/opt/xiaozhi-esp32-server/core/websocket_server.py:ro
        - ./custom-providers/xiaozhi-patches/http_server.py:/opt/xiaozhi-esp32-server/core/http_server.py:ro
        - ./custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:/opt/xiaozhi-esp32-server/core/handle/textMessageHandlerRegistry.py:ro
    bridge:
      environment:
        - DOTTY_VOICE_PROVIDER=tier1slim
        - DOTTY_KID_MODE=true
        # xiaozhi-server endpoint that bridge POSTs to for `/xiaozhi/admin/inject-text`
        # (face-detected greeting) and `/xiaozhi/admin/set-head-angles` (sound-direction turn).
        # Multi-host: bridge.py reads this from systemd Environment=; all-in-one: docker DNS.
        - XIAOZHI_HOST=xiaozhi-server
  ```
- Bring up: `docker compose --env-file .env -f compose.all-in-one.yml -f compose.openrouter.override.yml up -d --build`
- Watch logs: `docker compose logs -f` and verify:
  - `xiaozhi-server` reaches "Started service" without ASR/TTS init errors
  - `zeroclaw-bridge` starts the FastAPI app on :8080; ACP child process spawns (stub sleep), no crash

**Patterns to follow:**
- Docker compose override pattern is standard; the project uses it for `compose.local.override.yml` (Ollama) — same shape.
- The openai_compat provider expects `api_key` to be the literal string. xiaozhi-server's config loader supports `${VAR}` interpolation from container env — confirm this at execution time; if it doesn't, fall back to embedding the literal key in `data/.config.yaml` (still `.gitignore`d).

**Test scenarios:**
- `docker compose ps` shows both `xiaozhi-server` and `zeroclaw-bridge` in `running` state, no `restarting` flapping after 60s.
- `curl http://localhost:8080/health` returns 200 with JSON `{"status":"ok","service":"zeroclaw-bridge","acp_running":<bool>}`. `acp_running` may be `true` (stub responds to subprocess spawn) or `false` (bridge marks ACP unhealthy after timeout) — either is acceptable; the field is informational.
- `curl http://localhost:8003/xiaozhi/ota/` returns 200 with the OTA banner string.
- `curl http://localhost:8080/ui` returns 200 with the dashboard HTML.
- `docker compose logs xiaozhi-server | grep -i 'openai'` shows the provider initialized with the OpenRouter URL.
- Manual smoke: from the Pi or LAN, `curl -X POST http://localhost:8080/api/message -H 'content-type: application/json' -d '{"content":"hello"}'` returns either a real LLM response (if bridge HTTP API routes to LLM directly — not the path xiaozhi uses) or the friendly fallback (`"😐 My brain is offline"`). Either is fine; this isn't the voice path.
- `Test expectation: none -- behaviour validated via curl probes above; no unit-test surface in this unit`

**Verification:** Both containers `running` and `healthy` (per compose healthcheck if defined, else manual). Operator can hit `http://<PI_LAN_IP>:8080/ui` from another LAN device and see the dashboard.

---

### U4. Install + configure `cloudflared` as a native systemd service on the Pi

**Goal:** Tunnel is up; `dotty.pcar.me` (configured in the Cloudflare dashboard as a hostname on the tunnel) routes to `http://localhost:8080` on the Pi. Tunnel restarts on Pi reboot.

**Requirements:** R3, R7

**Dependencies:** U3

**Files (on Pi):**
- `/etc/apt/sources.list.d/cloudflared.list` (added by Cloudflare's apt repo install)
- `/usr/local/bin/cloudflared` (installed binary)
- `~/.cloudflared/config.yml` — tunnel config:
  ```yaml
  tunnel: <tunnel-uuid>
  credentials-file: /home/dotty/.cloudflared/<tunnel-uuid>.json
  ingress:
    - hostname: dotty.pcar.me
      service: http://localhost:8080
    - service: http_status:404
  ```
- `~/.cloudflared/<tunnel-uuid>.json` — tunnel credentials (downloaded during `cloudflared tunnel create`)
- `/etc/systemd/system/cloudflared.service` — installed by `cloudflared service install`

**Approach:**
- Install cloudflared from Cloudflare's apt repo for arm64:
  ```bash
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
  sudo apt update && sudo apt install -y cloudflared
  ```
- `cloudflared tunnel login` (browser flow on operator's workstation — the Pi may not have a browser, so this can be done by SSH-forwarding the auth URL)
- `cloudflared tunnel create dotty` — creates the tunnel, writes credentials JSON to `~/.cloudflared/`
- Write `~/.cloudflared/config.yml` (shape above) with the tunnel UUID
- Add the DNS record (CNAME `dotty` → `<tunnel-uuid>.cfargotunnel.com`) — `cloudflared tunnel route dns dotty dotty.pcar.me`
- Install as systemd service: `sudo cloudflared service install` (uses the config at `~/.cloudflared/config.yml`)
- Start + enable: `sudo systemctl start cloudflared && sudo systemctl enable cloudflared`

**Patterns to follow:**
- Cloudflare's documented Linux install path for arm64. No project-specific patterns.

**Test scenarios:**
- `sudo systemctl status cloudflared` shows `active (running)`, no restart loop.
- `cloudflared tunnel info dotty` shows the tunnel up with at least one connection.
- From an external network (LAN won't route through the tunnel) or via `curl --resolve dotty.pcar.me:443:104.16.x.x https://dotty.pcar.me/ui`, the request reaches the bridge and is gated by Cloudflare Access (returns 302 to the Access login page until U5 is configured — that's expected pre-U5).
- `Test expectation: none -- systemd service health validated via systemctl + tunnel-info above`

**Verification:** Operator sees the tunnel as "Healthy" in the Cloudflare Zero Trust > Networks > Tunnels dashboard, and `dig dotty.pcar.me` resolves to a Cloudflare edge IP.

---

### U5. Configure Cloudflare Access policy on `dotty.pcar.me`

**Goal:** Unauthenticated requests to `https://dotty.pcar.me/*` are blocked at Cloudflare's edge; authenticated requests reach the bridge dashboard.

**Requirements:** R3

**Dependencies:** U4

**Files:** None in the repo. Configuration lives in the Cloudflare Zero Trust dashboard.

**Approach:**
- In Cloudflare Zero Trust dashboard: Access → Applications → Add an Application → Self-hosted
- Application name: `Dotty Dashboard`
- Application domain: `dotty.pcar.me` (full hostname)
- Identity providers: One-Time PIN to the user's email is the simplest; Google / GitHub OIDC are richer if the user has them set up
- Policies: one policy, "Allow `pcarm92@gmail.com`" (or the user's preferred email)
- Session duration: 24h (default — change per preference)
- No special CORS / OIDC claims needed; the dashboard doesn't read Access headers (no per-user behaviour)

**Patterns to follow:**
- This mirrors the `dockflare.access.policy=authenticate` shape that DockFlare wraps elsewhere in the user's infrastructure, but done directly via the Zero Trust dashboard rather than via DockFlare labels.

**Test scenarios:**
- Unauthenticated curl to `https://dotty.pcar.me/ui` returns 302 to a `*.cloudflareaccess.com` URL.
- Authenticated request (in a browser, after PIN/SSO) reaches the dashboard.
- A different email attempting to authenticate is rejected at the policy stage.
- `Test expectation: none -- policy behaviour verified manually in browser`

**Verification:** Operator can open `https://dotty.pcar.me/ui` in a browser, authenticate via one-time PIN, and see the bridge dashboard. Logging out and revisiting the URL re-prompts for auth.

---

### U6. Configure + build StackChan firmware from the active dotty fork

**Goal:** A built `firmware.bin` (and partition + bootloader) ready to flash, with the OTA URL and (optionally) WiFi credentials baked in at build time so the device can self-provision after flash.

**Requirements:** R5, R8

**Dependencies:** U3 (Pi must be running so its LAN IP — used as the OTA host — is final and reachable before the firmware is built). Can otherwise be done on any workstation with Docker and USB-C access to the StackChan.

**Files:**
- `~/work/StackChan-dotty/` (or similar) on a workstation — clone of `https://github.com/BrettKinny/StackChan`, checkout branch `dotty`
- `~/work/StackChan-dotty/firmware/sdkconfig.defaults` — **edited pre-build** to set the OTA URL and (optionally) WiFi credentials
- Build outputs: `firmware/build/firmware.bin`, `firmware/build/bootloader.bin`, `firmware/build/partition_table/partition-table.bin`

**Approach:**
- Clone the active fork on a workstation that has Docker and USB-C access to the StackChan:
  ```bash
  git clone -b dotty https://github.com/BrettKinny/StackChan ~/work/StackChan-dotty
  cd ~/work/StackChan-dotty/firmware
  ```
- **Edit `firmware/sdkconfig.defaults` before building.** Per `SETUP.md` §2a, this is the required pre-build step that points the firmware at the Pi's OTA endpoint. Add (or modify) the line:
  ```
  CONFIG_OTA_URL="http://<PI_LAN_IP>:8003/xiaozhi/ota/"
  ```
  Trailing slash matters — that's the path the server exposes. Use the Pi's actual LAN IP from U1, not a placeholder.
- **Optionally, also bake in WiFi credentials at build time** (per `SETUP.md` §3 — simplest path for a static home setup):
  ```
  CONFIG_WIFI_SSID="your-2.4ghz-ssid"
  CONFIG_WIFI_PASSWORD="your-wifi-password"
  ```
  Caveat: credentials are stored in the firmware binary. If you'd rather rely on the upstream xiaozhi-esp32 build's fallback provisioning (BLE or an upstream-version-dependent fallback flow), leave these unset and consult the xiaozhi-esp32 README for what your pinned commit supports. Note the StackChan's WiFi is 2.4 GHz only (ESP32-S3 doesn't do 5 GHz).
- Run the IDF container build:
  ```bash
  docker run --rm -v "$PWD:/project" -w /project \
    espressif/idf:v5.5.4 bash -lc \
    'git config --global --add safe.directory "*" && python fetch_repos.py && idf.py build'
  ```
- `fetch_repos.py` clones upstream `78/xiaozhi-esp32 v2.2.4` and applies `patches/xiaozhi-esp32.patch`. ~5 min cold build, faster incremental.

**Patterns to follow:**
- `SETUP.md` §2 ("Build and flash open firmware") is the authoritative recipe for the pre-build sdkconfig edits and the IDF container build.
- The repo-level `CLAUDE.md` `## Firmware iteration` section covers the development-loop gotchas (CMake GLOB cache, `%lld` printf, upstream patch regen).

**Test scenarios:**
- `grep CONFIG_OTA_URL firmware/sdkconfig.defaults` shows the Pi's LAN IP, not a placeholder.
- If WiFi compile-time creds were set: `grep CONFIG_WIFI_SSID firmware/sdkconfig.defaults` shows the chosen SSID.
- `ls firmware/build/firmware.bin firmware/build/bootloader.bin firmware/build/partition_table/partition-table.bin` shows all three artifacts.
- `file firmware/build/firmware.bin` reports `data` (ESP32 firmware blob); size is in the few-MB range.
- `Test expectation: none -- build produces artifacts; behavioural verification happens in U8 end-to-end`

**Verification:** Three binaries exist at the paths above; the build log shows no warnings about missing symbols or `undefined reference`. `CONFIG_OTA_URL` baked into the firmware matches the Pi's reachable LAN IP. Operator notes the firmware commit hash for the deployment changelog.

---

### U7. Flash the StackChan and watch first-boot handshake

**Goal:** StackChan boots the new firmware, joins LAN WiFi (from compile-time creds or upstream fallback), and successfully POSTs to the Pi's OTA endpoint — establishing a working WebSocket session.

**Requirements:** R8

**Dependencies:** U3 (Pi's xiaozhi-server must be running and serving OTA), U6 (firmware built with the correct OTA URL baked in)

**Files:** None in this repo. Operator runs commands on the workstation; the StackChan is the target.

**Approach:**
- USB-C the StackChan to the workstation (`/dev/ttyACM0` appears on Linux; `/dev/cu.usbmodem*` on macOS — adapt the `--device` flag accordingly).
- Flash from the same IDF container as U6:
  ```bash
  docker run --rm --device=/dev/ttyACM0 \
    -v "$PWD:/project" -w /project \
    espressif/idf:v5.5.4 \
    bash -lc 'idf.py -p /dev/ttyACM0 -b 921600 flash'
  ```
  If the build reported a non-standard flash command (e.g. a merged-bin flow), use that instead — `SETUP.md` §2c covers the variants.
- **No SoftAP / captive-portal / QR-code onboarding flow.** The open-source firmware doesn't have one — `SETUP.md` is explicit on this (§1, lines 47-48). After flash, the device:
  - Boots directly (no pairing-code screen)
  - Loads WiFi credentials compiled into the firmware (if U6 set `CONFIG_WIFI_SSID` / `CONFIG_WIFI_PASSWORD`), or uses whatever fallback provisioning the upstream `78/xiaozhi-esp32` build at the pinned commit exposes (BLE provisioning is the typical fallback — check the upstream README for the current default)
  - POSTs to `http://<PI_LAN_IP>:8003/xiaozhi/ota/` automatically (URL was baked in at build time via `CONFIG_OTA_URL`)
  - Receives the WebSocket endpoint in the OTA response and connects
- Watch the handshake by tailing xiaozhi-server logs on the Pi while the device boots:
  ```bash
  ssh dotty@<PI_LAN_IP> 'docker logs -f xiaozhi-esp32-server'
  ```
  Within ~30s of reboot, in order: a `POST /xiaozhi/ota/` line; a WebSocket connect line with the device's MAC; a `vad` / `asr` init line when the device first starts listening.

**Patterns to follow:**
- `SETUP.md` §2d ("First boot after flash") and §4 ("Watch the handshake") are the authoritative post-flash flow.
- The `/dev/ttyACM0` "disappears after a hard reset" gotcha from repo-level `CLAUDE.md` `## Firmware iteration` applies: re-plug USB-C if the device flash command can't find the port.
- If the device doesn't appear in xiaozhi logs within 60s, `SETUP.md` §4 lists the triage steps (check DHCP for an ESP32 MAC; curl the OTA URL from another LAN device; power-cycle by holding power 3s + unplug-replug).

**Test scenarios:**
- After flash + reboot, the StackChan's display shows the avatar idle state (eyes open, neutral expression, "Dotty" or the configured robot name on-screen).
- `docker logs xiaozhi-esp32-server --tail 50` on the Pi shows a WebSocket connection from the StackChan's MAC address.
- The device's status menu (via long-press the touch panel, if accessible) reports a healthy WS connection.
- `Test expectation: none -- end-to-end behavioural validation happens in U8`

**Verification:** StackChan is on the LAN, WS-connected to the Pi, idle and waiting for input.

---

### U8. End-to-end smoke test + tuning

**Goal:** A real voice turn round-trips successfully: speak to Dotty, hear a response, see the face animation, observe the perception consumers fire when expected.

**Requirements:** R1, R4, R6

**Dependencies:** U3 (Pi stack running), U5 (Cloudflare Access configured), U7 (StackChan onboarded)

**Files:**
- `scripts/dotty_doctor.py` — repo's existing health-check tool; run on the Pi
- No new files

**Approach:**
- On the Pi: `python scripts/dotty_doctor.py` (or `make doctor`) — read its output for any failed checks.
- Walk through the `SETUP.md` first-conversation flow:
  - Wake the device (firmware uses wake-word + face-detected as wake triggers in `talk` state; if device is in `idle`, look at it to trigger face-detected → `Hi!` greeting)
  - Say a sample utterance ("Hello, what's your name?")
  - Confirm:
    - Mic LED (right ring index 11) lights red while listening
    - ASR transcribes (visible in xiaozhi-server logs)
    - LLM responds via OpenRouter (visible in xiaozhi-server logs — look for `openai_compat` + the OpenRouter URL)
    - TTS plays back through the device speaker
    - Face emoji renders correctly (the LLM's emoji prefix should be parsed and animated)
- Walk through perception:
  - Step out of the device's view (face_lost) and back in (face_detected) — verify the bridge fires a greeting via `/xiaozhi/admin/inject-text`
  - Make a sharp sound to one side — verify the bridge fires a head-turn via `/xiaozhi/admin/set-head-angles`
- Walk through kid mode:
  - Test that kid_mode is on by default (the `_TURN_SUFFIX` from `custom-providers/openai_compat/openai_compat.py` appends safety language to user turns)
  - Verify the kid_mode pip (right ring index 8, salmon pink) is lit
- Tune as needed:
  - If voice turns feel sluggish: check OpenRouter free-router's currently-routed model in the response headers (`x-or-model`), consider swapping to `anthropic/claude-haiku-4.5`
  - If emoji prefixes are missing: bridge fallback handles it, but adjust the system prompt in `data/.config.yaml` `prompt:` block to emphasize the emoji-first rule
  - If perception events don't fire: check `docker logs zeroclaw-bridge` for `EventTextMessageHandler` POST receipts at `/api/perception/event`

**Patterns to follow:**
- `SETUP.md` pre-flight curls (already verified at U3) — same shape applies to in-context smoke checking.
- The dashboard's live event log at `https://dotty.pcar.me/ui` shows turns + perception events + errors in real-time; use it as the primary observability surface during tuning.

**Test scenarios:**
- A complete voice turn ("Hello, what's your name?" → spoken response containing "Dotty" with a face animation) completes within 3-5 seconds.
- The LLM response starts with a valid emoji from the project's allowed set; the face animation matches (e.g., 😊 → smile).
- `face_detected` perception event followed by a `Hi!`-class greeting (via TTS) when stepping into view.
- `kid_mode` is enabled by default; `curl http://localhost:8080/api/state/kid-mode` (or whatever the bridge exposes — check the dashboard) confirms.
- Container restart survival: `sudo reboot` the Pi; after boot, all checks above still pass without manual intervention.
- `Test expectation: none -- end-to-end smoke is the verification`

**Verification:** A 5-minute conversation works without crashes, error fallbacks, or rate-limit hits. The dashboard's event log shows clean turns. Operator marks deployment complete.

---

## System-Wide Impact

**Files in this repo:** None require commits as part of this deployment. Everything is local Pi configuration + Cloudflare dashboard config + firmware build (in a separate fork). The plan deliberately avoids editing tracked files (`compose.all-in-one.yml`, `.config.yaml.template`) in favour of a host-local override file (`compose.openrouter.override.yml`) and `data/.config.yaml` (already `.gitignore`'d).

**Existing functionality:** None affected. Other deployment shapes (multi-host, Tier1Slim+llama-swap, ZeroClaw, the Synology-based Synology-conventions deployment) all remain valid and untouched.

**Future maintenance:** Upgrading from upstream requires merging changes from `compose.all-in-one.yml` + `.config.yaml.template` into the Pi's local override + rendered config. The hand-edit pattern means an upstream wizard-feature change (e.g., adding a new template placeholder) might not flow through automatically. Mitigation: when fetching upstream, diff `.config.yaml.template` against the Pi's `data/.config.yaml` and propagate any new keys.

**External dependencies introduced:** OpenRouter free-tier API (rate limits documented above), Cloudflare Tunnel + Access, the cloudflared apt repository.

---

## Verification Strategy

V1 is verified hands-on, not via automated tests in this repo. The plan's verification surface is:

1. **Container health** — `docker compose ps` showing both services running, `curl http://localhost:8080/health` returning 200 (U3).
2. **OTA + WS reachability** — `curl http://<PI_IP>:8003/xiaozhi/ota/` returns 200 with banner (U3, U7).
3. **Cloudflare Tunnel health** — Zero Trust dashboard shows tunnel as healthy with connections; external HTTPS access through Cloudflare Access works (U4, U5).
4. **Firmware on device** — StackChan boots, joins WiFi, connects to xiaozhi-server WS (U7).
5. **End-to-end voice turn** — A real spoken utterance produces a voice response with face animation (U8).
6. **Perception consumers** — face-detected greeting fires, sound-direction turns fire (U8).
7. **Reboot survival** — `sudo reboot` the Pi, everything comes back up (U7, U8).

The repo's `scripts/dotty_doctor.py` (or `make doctor`) runs a subset of these checks programmatically and is the canonical "is this thing healthy?" entry point.

---

## Risks

**RI1. `openrouter/free` rate limits hit during active play.** 200 req/day + 20 req/min limits are easy to exceed in a 1-hour kid-play session, especially with perception events adding chat-less LLM calls. **Mitigation:** documented one-line swap to `anthropic/claude-haiku-4.5` (or any other OpenRouter model) in `data/.config.yaml`; operator notes the rate-limit symptom (HTTP 429 in xiaozhi-server logs → `Sorry, I'm thinking too slowly right now` fallback message).

**RI2. SD card wear from `brain.db` writes.** Every voice turn writes to `brain.db`; perception events write less frequently. SanDisk High Endurance / Samsung PRO Endurance mitigate but don't eliminate. **Mitigation:** NVMe HAT upgrade is deferred follow-up; operator monitors `df` and SMART data quarterly. Worst-case (SD failure): restore from a periodic `brain.db` snapshot to USB drive.

**RI3. Zeroclaw stub causes unexpected bridge behaviour.** The stub `exec sleep infinity` keeps the ACP subprocess "alive" but unresponsive. Bridge code paths that wait on ACP output will hit `asyncio.TimeoutError` per line 4840 of `bridge.py` and return a friendly fallback. **Mitigation:** the OpenAICompat LLM path doesn't touch ACP at all, so the only triggers for ACP calls are direct hits to `/api/message` (not used by xiaozhi-server in this config). Verify in U3's container-startup logs that the bridge doesn't crash on ACP timeout — if it does, the fallback plan is to set `DOTTY_VOICE_PROVIDER=tier1slim` env var (already in the override, but its behaviour there is the failsafe) and confirm bridge code paths around line 311 of `bridge.py` skip ACP spawning entirely.

**RI4. Cloudflare Access PIN flow not viable in the user's setup.** If the user wants the dashboard accessible from a household-shared device without PIN-per-session, a session-duration extension or a more permissive policy is needed. **Mitigation:** Cloudflare Access supports 24h+ session durations and device-posture allow-lists; revisit at U5.

**RI5. Firmware build fails on the workstation.** The IDF container build is well-trodden (the repo's `CLAUDE.md` documents it) but `fetch_repos.py` reaching out to GitHub can fail transiently. **Mitigation:** retry; if persistent, check the patches/xiaozhi-esp32.patch applies cleanly to a fresh `v2.2.4` checkout.

**RI6. StackChan WiFi onboarding doesn't surface a clear AP.** The CoreS3's 2.4GHz-only WiFi must be on a 2.4GHz SSID; if the LAN is 5GHz-only or the SSID is split-band with the 2.4 hidden, the device can't see it. **Mitigation:** confirm a 2.4GHz SSID is broadcast before unboxing the device.

---

## Deferred Questions

These are noted but do not block V1:

- **Backup strategy for `brain.db`.** Not in V1 scope. Once the deployment is daily-driven, a `rsync` cron to a USB drive or the Synology is the obvious move. Schema-aware backup (Postgres-style logical dump) isn't applicable — SQLite snapshots are fine.
- **Persona customization.** Default `personas/default.md` works; the user may want a "household-aware" persona later (referencing kid names, household routines). Out of V1.
- **Voice swap to a non-default Piper voice.** `make voice-list` + `make voice-install VOICE=<key> APPLY=1` is the existing path; out of V1 (default is fine).
- **Monitoring + alerting** beyond the dashboard. The bridge exposes Prometheus metrics if `_METRICS_AVAILABLE` is true; `monitoring/` directory has scrape config templates. Out of V1.
- **Multi-device household.** All bridge state is per-device-id keyed (`_perception_state[device_id]`); supporting two StackChans on the same Pi works out of the box from the bridge's perspective. Out of V1.

---

## Operational Notes

- **Restarts:** `docker compose --env-file .env -f compose.all-in-one.yml -f compose.openrouter.override.yml restart` for the stack; `sudo systemctl restart cloudflared` for the tunnel.
- **Log access:** `docker compose logs -f` (both services interleaved); `sudo journalctl -u cloudflared -f` for tunnel.
- **Upgrade path:** `git pull` in the repo, diff `.config.yaml.template` against `data/.config.yaml` to surface new keys, `docker compose pull && docker compose up -d` to refresh the xiaozhi-server image. Bridge image rebuilds from the inline Dockerfile in compose.
- **Disaster recovery:** SD card image taken after U8 lands gives a fully working restore baseline. `dd if=/dev/disk2 of=dotty-v1-baseline.img bs=1m` on a Mac or `pishrink` for a smaller image.
