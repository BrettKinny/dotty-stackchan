# Claude Code Session Prompt — StackChan Infrastructure Setup

> Historical bootstrap prompt. Describes how this infra was originally stood
> up. Updated after the 2026-05-19 cutover (#36) which retired the separate
> ZeroClaw/RPi host and consolidated all services onto a single Docker host.
> The firmware-provisioning steps still require the build-from-source flow in
> `SETUP.md`.

Paste this into your terminal:

```
claude --prompt-file ./session-prompt.md
```

---

## Prompt content (save as `session-prompt.md`):

I need you to set up infrastructure on a single Linux Docker host for an M5Stack StackChan robot. You'll be SSHing from this workstation to the target via Tailscale. Read the CLAUDE.md in this directory first for full architecture context.

## What you're building

A self-hosted voice pipeline with four Docker containers on one host:

1. **xiaozhi-esp32-server** — ASR (FunASR SenseVoiceSmall) + TTS (Piper/EdgeTTS) + WebSocket voice gateway for StackChan.
2. **dotty-pi** — the pi coding agent: the voice-tool brain. Runs `qwen3.5:4b` on a local llama-swap instance; invoked via `docker exec -i` by the `PiVoiceLLM` provider. See `dotty-pi/README.md`.
3. **dotty-behaviour** — perception event bus, ambient consumers, greeter, calendar. FastAPI on `:8090`. See `dotty-behaviour/README.md` and `scripts/deploy-behaviour.sh`.
4. **bridge.py** (admin dashboard) — HTMX admin UI on `:8080`. Runs as a container on the same host.

All four containers run on the same machine (the Docker host). There is no separate brain host or RPi.

## Discovery steps (do these first)

1. Run `tailscale status` (if you use Tailscale) to find the hostname and IP for the Docker host.
2. SSH into the Docker host. Find its LAN IP (not Tailscale IP) — StackChan will need this because it's on WiFi, not Tailnet. Check `ip addr` or `hostname -I`. Also confirm Docker is available and pick a directory for the install (e.g. `/opt/xiaozhi-server/` or `/srv/xiaozhi-server/`).
3. Check whether a local model backend is already running — llama-swap at `:8080/health` (or Ollama at `:11434/api/tags`). If not, set up Ollama as the simpler single-binary option (see `cookbook/run-fully-local.md`) or llama-swap if you need concurrent voice+coding model sets.

## Docker host setup (xiaozhi-esp32-server)

On the Docker host:

1. Create the directory structure at your chosen install path (e.g. `/opt/xiaozhi-server/`) with subdirs: `data/`, `models/SenseVoiceSmall/`, `tmp/`.
2. Clone this repo (`dotty-stackchan`) into the install path or copy the relevant files. The `make setup` wizard handles most substitution.
3. Download the SenseVoiceSmall ASR model (`model.pt`, ~250 MB) into `models/SenseVoiceSmall/`. Try ModelScope first: `https://www.modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt`. If that's slow, use HuggingFace: `https://huggingface.co/FunAudioLLM/SenseVoiceSmall/resolve/main/model.pt`. Verify the file is >200 MB after download.
4. Create `data/.config.yaml` with:
   - `selected_module.ASR: FunASRLocal`
   - `selected_module.LLM: PiVoiceLLM`
   - `selected_module.TTS: LocalPiper` (or `EdgeTTS` if you don't want offline TTS)
   - `selected_module.VAD: SileroVAD`
   - PiVoiceLLM container name: `dotty-pi` (the `docker exec` target)
   - A system prompt that identifies as a desktop robot assistant. Enforce emoji-first responses. Keep TTS-friendly (short sentences).
   - VAD silence duration 700 ms (so it doesn't cut off slow speakers)
   - Use the actual LAN IP for `<XIAOZHI_HOST>`, not a placeholder.
5. Bring up the xiaozhi-esp32-server container via `docker compose up -d` (from `docker-compose.yml.template` after `make setup` substitutes your placeholders).
6. Check the container's internal directory structure before writing volume mounts. Run `docker run --rm ghcr.io/xinnan-tech/xiaozhi-esp32-server:server_latest ls /opt/xiaozhi-server/` (or wherever the app lives) to confirm internal paths. The mount targets must match where the app actually loads providers from.
7. Tail the logs and confirm you see the WebSocket and OTA addresses in the output.

## Bring up dotty-pi (brain container)

Follow `dotty-pi/README.md` for build and run instructions. Key points:

- The container idles via `sleep infinity`; voice turns invoke pi on demand via `docker exec -i`.
- Model target for the outer agent loop: `qwen3.5:4b` (in the llama-swap `voice` matrix set). Do **not** use `qwen3.6:27b` here — it evicts the voice model set and causes cold-reload latency on every `think_hard` escalation.
- Mount `persona/`, `memory/brain.db`, and the `dotty-pi-ext` extension directory as documented in `dotty-pi/README.md`.

## Bring up dotty-behaviour (perception + dashboard backend)

Follow `dotty-behaviour/README.md` for build and run instructions. The `scripts/deploy-behaviour.sh` helper handles the deploy step. Key points:

- Runs in `network_mode: host` on port `:8090`.
- xiaozhi-server talks to it on `http://<XIAOZHI_HOST>:8090` (the LAN IP, not loopback — xiaozhi-server is on bridge networking so its loopback resolves to itself).
- Set `VISION_BRIDGE_URL` env var in the xiaozhi-server compose to `http://<XIAOZHI_HOST>:8090`.

## Bring up the admin dashboard (bridge.py)

The bridge.py dashboard container starts as part of the main compose stack. It serves the HTMX admin UI on `:8080/ui` and a health endpoint at `:8080/health`. No separate deployment step beyond `docker compose up -d`.

## Testing

After all containers are up:

1. Check the OTA endpoint from a LAN machine:
   ```bash
   curl -s http://<XIAOZHI_HOST>:8003/xiaozhi/ota/
   # Expect: OTA接口运行正常...
   ```
2. Check the dashboard health:
   ```bash
   curl -s http://<XIAOZHI_HOST>:8080/health
   # Expect: {"status":"ok", ...}
   ```
3. Check dotty-behaviour health:
   ```bash
   curl -s http://<XIAOZHI_HOST>:8090/health
   # Expect: {"status":"ok", ...}
   ```
4. Tail the xiaozhi-server logs to confirm the WebSocket endpoint is listening.

## Final output

When everything is confirmed working, print a clear summary:

```
=== STACKCHAN SETUP COMPLETE ===

OTA URL (enter this in StackChan's Advanced Settings):
  http://X.X.X.X:8003/xiaozhi/ota/

WebSocket endpoint:
  ws://X.X.X.X:8000/xiaozhi/v1/

Admin dashboard:
  http://X.X.X.X:8080/ui

dotty-behaviour (perception bus):
  http://X.X.X.X:8090/health

When StackChan arrives:
  1. Flash open firmware built from https://github.com/m5stack/StackChan
     (see SETUP.md for the current build/flash flow — stock firmware
     ships with BLE+cloud provisioning, not the SoftAP flow that some
     older guides describe).
  2. First boot: device POSTs to the OTA URL, gets redirected to the
     WebSocket endpoint, and connects automatically.
```

## Important constraints

- Use `micro` if you need to interactively edit files (not nano, not vim).
- Don't install anything on the local workstation — everything happens via SSH to the remote machine.
- All IPs in config files must be real LAN IPs discovered at runtime, not Tailscale IPs (StackChan isn't on the Tailnet).
- If any step fails, diagnose from the logs before retrying. Don't just re-run blindly.
- The xiaozhi-esp32-server Docker image's internal directory structure may differ from the repo layout. Inspect the container before writing volume mounts.
- Be conservative about commands not documented in the component READMEs — link to `dotty-pi/README.md` and `dotty-behaviour/README.md` rather than inventing invocations.
