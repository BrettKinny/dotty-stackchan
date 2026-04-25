# StackChan Infrastructure

## What This Is

A self-hosted voice stack for the M5Stack **StackChan** desktop robot. Hardware is the StackChan body; the agent persona (what ZeroClaw runs) is configurable — this repo ships a default that uses the hardware name, swap in anything you want via the `<ROBOT_NAME>` placeholder. Voice I/O routes through a self-hosted xiaozhi-esp32-server; brain is ZeroClaw on the RPi. No cloud AI services — fully self-hosted except for EdgeTTS (replaceable with local Piper).

## Architecture

```
StackChan hardware → configured persona
  │
  │  ESP32-S3, xiaozhi firmware (built from m5stack/StackChan source)
  │  WiFi / WebSocket (Xiaozhi protocol)
  ▼
xiaozhi-esp32-server (Docker on Unraid)
  ├─ ASR: FunASR SenseVoiceSmall (local, no cloud)
  ├─ TTS: EdgeTTS en-AU-WilliamNeural
  ├─ LLM: Custom ZeroClawLLM provider (proxies to RPi)
  └─ Emotion: Parsed from emoji in LLM response text
       │  HTTP POST /api/message
       ▼
zeroclaw-bridge (FastAPI on RPi, runs as root)
  │  JSON-RPC 2.0 over stdio to a long-running `zeroclaw acp` child
  ▼
ZeroClaw (the brain, on RPi)
```

See `README.md` for the full visual architecture and message-flow diagrams.

## Network

- **Admin workstation** (this machine): Development/admin workstation. Runs Claude Code sessions.
- **Unraid**: Docker host for xiaozhi-esp32-server. Reachable via Tailscale and LAN.
- **RPi (DietPi)**: Runs ZeroClaw + the HTTP bridge. Reachable via Tailscale and LAN.
- **StackChan**: On LAN WiFi only (not on Tailnet). Needs LAN IPs for OTA and WebSocket.

SSH access is via Tailscale hostnames. Discover actual Tailscale hostnames at runtime with `tailscale status`.

This repo uses placeholders (`<UNRAID_IP>`, `<RPI_IP>`, `<RPI_USER>`, `<UNRAID_XIAOZHI_PATH>`, etc.) everywhere real values would normally appear — see the "Configuring for your environment" section of `README.md` for the full list.

## Key Paths

- **Unraid xiaozhi-server**: `<UNRAID_XIAOZHI_PATH>` (e.g. `/mnt/user/appdata/xiaozhi-server/`)
- **Unraid custom LLM provider**: mounted into container at `/opt/xiaozhi-server/core/providers/llm/zeroclaw/`
- **RPi ZeroClaw bridge**: `<RPI_BRIDGE_PATH>` (e.g. `~/zeroclaw-bridge/`)
- **This project dir**: wherever you cloned `dotty-stackchan`

## Ports

| Service | Host | Port | Protocol |
|---------|------|------|----------|
| xiaozhi WebSocket | Unraid LAN IP | 8000 | ws:// |
| xiaozhi OTA/HTTP | Unraid LAN IP | 8003 | http:// |
| ZeroClaw bridge | RPi LAN IP | 8080 | http:// |
| ZeroClaw gateway (ws) | RPi localhost | 18789 | ws:// |
| ZeroClaw gateway (web UI) | RPi localhost | 42617 | http:// |

## Config Files to Know

- `.config.yaml` (repo root; deployed to Unraid) — the xiaozhi-server override config. Never overwrite wholesale on upgrades; merge keys.
- `custom-providers/zeroclaw/zeroclaw.py` — custom LLM provider. Mounted into the container via docker-compose volume.
- `custom-providers/edge_stream/edge_stream.py` — custom streaming TTS provider. Mounted similarly.
- `custom-providers/openai_compat/openai_compat.py` — OpenAI-compatible LLM provider (alternative to ZeroClaw).
- `custom-providers/piper_local/piper_local.py` — local Piper TTS provider (offline alternative to EdgeTTS).
- `custom-providers/asr/fun_local.py` — patched FunASR provider. Adds a `language` config key (upstream hardcodes `"auto"`, which mis-detects Korean/Japanese on unclear English). Mounted as a file-level override over the upstream provider.
- `bridge.py` on RPi — the HTTP↔ZeroClaw translator (ACP-over-stdio client).
- `personas/default.md` — default robot persona prompt (swappable).
- `session-prompt.md` — Claude Code session prompt for infrastructure setup.

## Emotion/Expression Protocol

The LLM response MUST start with an emoji. The xiaozhi firmware parses it into a face animation:
😊=smile 😆=laugh 😢=sad 😮=surprise 🤔=thinking 😠=angry 😐=neutral 😍=love 😴=sleepy

Three layers enforce this:
1. **ZeroClaw's own agent prompt** (the configured persona) — primary source
2. **xiaozhi-server top-level `prompt:`** in `data/.config.yaml` — gets injected as system message
3. **Bridge fallback** (`_ensure_emoji_prefix` in `bridge.py`) — if the first non-whitespace char isn't a non-ASCII symbol, prepends 😐 before returning.

## Key Directories

- `custom-providers/` — all custom ASR/LLM/TTS providers (mounted into the xiaozhi container)
- `bridge/` — bridge Python dependencies (`requirements.txt`)
- `firmware/` — StackChan firmware patches, remote config, and server-side OTA assets
- `personas/` — swappable robot persona prompts
- `docs/` — deep technical reference (architecture, hardware, protocols, brain, latent capabilities)

## Make Targets

Run `make help` for the full list. Key targets:

- `make setup` — interactive first-run wizard (substitutes placeholders, fetches models, starts containers)
- `make doctor` — health checks on config, models, and services
- `make fetch-models` — download SenseVoiceSmall + Piper voice models
- `make up` / `make down` / `make logs` / `make status` — docker compose shortcuts

## Common Maintenance Tasks

- **Change TTS voice**: Edit `data/.config.yaml` on Unraid, `TTS.EdgeTTS.voice` / `TTS.StreamingEdgeTTS.voice`. Restart container.
- **Change system prompt**: Edit `data/.config.yaml` on Unraid, top-level `prompt:` block. Restart container.
- **Check logs**: `ssh <UNRAID_USER>@<UNRAID_IP> 'docker logs -f xiaozhi-esp32-server'`
- **Restart pipeline**: `ssh <UNRAID_USER>@<UNRAID_IP> 'cd <UNRAID_XIAOZHI_PATH> && docker compose restart'`
- **Test bridge**: `curl http://<RPI_IP>:8080/health`
- **Test full round-trip**: `curl -X POST http://<RPI_IP>:8080/api/message -H 'Content-Type: application/json' -d '{"content":"hello"}'`

## Deeper reference

For hardware specs, protocol details, model internals, and latent capabilities, see [`docs/README.md`](./docs/README.md) and its linked files (`architecture.md`, `hardware.md`, `voice-pipeline.md`, `brain.md`, `protocols.md`, `latent-capabilities.md`, `references.md`).

## Tech Stack Refs

- xiaozhi-esp32-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- xiaozhi-esp32 firmware (upstream): https://github.com/78/xiaozhi-esp32
- ZeroClaw: https://github.com/zeroclaw-labs/zeroclaw
- StackChan (hardware + firmware patches): https://github.com/m5stack/StackChan
- Emotion protocol: https://xiaozhi.dev/en/docs/development/emotion/
