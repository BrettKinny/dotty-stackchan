# Dotty

## What This Is

Your self-hosted StackChan robot assistant. A fully self-hosted voice stack for the M5Stack **StackChan** desktop robot. The default persona is "Dotty" (customizable via `make setup`). Voice I/O routes through a self-hosted xiaozhi-esp32-server; brain is ZeroClaw on the RPi. No cloud AI services — fully self-hosted except for the LLM call (replaceable with local Ollama).

## Architecture

```
StackChan hardware → configured persona
  │
  │  ESP32-S3, xiaozhi firmware (built from m5stack/StackChan source)
  │  WiFi / WebSocket (Xiaozhi protocol)
  ▼
xiaozhi-esp32-server (Docker on a Linux host)
  ├─ ASR: FunASR SenseVoiceSmall (local, no cloud)
  ├─ TTS: LocalPiper (en_US-kristin-medium); EdgeTTS / StreamingEdgeTTS available as alternates
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
- **Docker host**: runs xiaozhi-esp32-server. Any Linux box with Docker works. Reachable on the LAN (and optionally Tailscale).
- **RPi (DietPi)**: Runs ZeroClaw + the HTTP bridge. Reachable via Tailscale and LAN.
- **StackChan**: On LAN WiFi only (not on Tailnet). Needs LAN IPs for OTA and WebSocket.

SSH access is via Tailscale hostnames. Discover actual Tailscale hostnames at runtime with `tailscale status`.

This repo uses placeholders (`<XIAOZHI_HOST>`, `<RPI_IP>`, `<RPI_USER>`, `<XIAOZHI_PATH>`, etc.) everywhere real values would normally appear — see the "Configuring for your environment" section of `README.md` for the full list.

## Key Paths

- **xiaozhi-server install dir** (on the Docker host): `<XIAOZHI_PATH>` (e.g. `/opt/xiaozhi-server/`)
- **Custom LLM provider** (on the Docker host): mounted into container at `/opt/xiaozhi-server/core/providers/llm/zeroclaw/`
- **RPi ZeroClaw bridge**: `<RPI_BRIDGE_PATH>` (e.g. `~/zeroclaw-bridge/`)
- **This project dir**: wherever you cloned `dotty-stackchan`

## Ports

| Service | Host | Port | Protocol |
|---------|------|------|----------|
| xiaozhi WebSocket | Docker host LAN IP | 8000 | ws:// |
| xiaozhi OTA/HTTP | Docker host LAN IP | 8003 | http:// |
| ZeroClaw bridge | RPi LAN IP | 8080 | http:// |
| ZeroClaw gateway (ws) | RPi localhost | 18789 | ws:// |
| ZeroClaw gateway (web UI) | RPi localhost | 42617 | http:// |

## Config Files to Know

- `.config.yaml` (repo root; deployed to the Docker host as `data/.config.yaml`) — the xiaozhi-server override config. Never overwrite wholesale on upgrades; merge keys.
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

- **Change TTS voice**: Edit `data/.config.yaml` on the Docker host. For the default `LocalPiper`, swap the `voice` + `model_path` + `config_path` keys (download a new `.onnx` / `.onnx.json` pair into `models/piper/`). For `EdgeTTS` / `StreamingEdgeTTS` alternates, change `TTS.EdgeTTS.voice` / `TTS.StreamingEdgeTTS.voice` and switch `selected_module.TTS`. Restart container.
- **Change system prompt**: Edit `data/.config.yaml` on the Docker host, top-level `prompt:` block. Restart container.
- **Check logs**: `ssh <XIAOZHI_USER>@<XIAOZHI_HOST> 'docker logs -f xiaozhi-esp32-server'`
- **Restart pipeline**: `ssh <XIAOZHI_USER>@<XIAOZHI_HOST> 'cd <XIAOZHI_PATH> && docker compose restart'`
- **Test bridge**: `curl http://<RPI_IP>:8080/health`
- **Test full round-trip**: `curl -X POST http://<RPI_IP>:8080/api/message -H 'Content-Type: application/json' -d '{"content":"hello"}'`

## Firmware iteration

Build + flash the StackChan firmware locally with the cached IDF container — no GHA round-trip needed for dev cycles.

```bash
cd firmware/firmware

# Build (≈5 min cold, faster incremental). fetch_repos.py clones
# upstream xiaozhi-esp32 v2.2.4 and applies patches/xiaozhi-esp32.patch.
docker run --rm -v "$PWD:/project" -w /project \
  espressif/idf:v5.5.4 bash -lc \
  'git config --global --add safe.directory "*" && python fetch_repos.py && idf.py build'

# USB-C flash (device shows up as /dev/ttyACM0).
docker run --rm -v "$PWD:/project" -w /project \
  --device=/dev/ttyACM0 espressif/idf:v5.5.4 \
  bash -lc 'idf.py -p /dev/ttyACM0 -b 921600 flash'
```

Gotchas hit in real sessions:

- **CMake GLOB cache**: when adding a new `.cpp/.h` under `main/stackchan/`, `idf.py build` will *silently* not compile it and you'll get a linker error like `undefined reference to '...'`. Force a reconfigure with `touch main/CMakeLists.txt` then rebuild — or run `idf.py reconfigure` once.
- **`%lld` printf**: ESP-IDF newlib's printf doesn't reliably honour `%lld` in this build. Use `%.0f` with a `double` cast for >32-bit integers, or manually split the value.
- **Upstream xiaozhi-esp32 changes** go through `firmware/firmware/patches/xiaozhi-esp32.patch`, not directly into the working tree (which `fetch_repos.py` re-fetches). After editing the upstream tree locally for a build, regenerate with `git -C firmware/firmware/xiaozhi-esp32 diff HEAD > firmware/firmware/patches/xiaozhi-esp32.patch`. Verify the patch applies cleanly to a fresh `v2.2.4` checkout before committing.
- **`/dev/ttyACM0` disappears** after a hard reset / power cycle; if `docker run` complains "no such file", either re-plug the USB-C cable or wait for the device to finish booting back into the JTAG-Serial endpoint.

## Ambient perception layer (Phase 1)

Forward-looking modes (face-detected greeting, sound-direction head-turn, future curiosity / boredom mode) all subscribe to a single perception event bus on the bridge. Producers are firmware-resident and emit JSON `event` frames over the WS:

```json
{"type":"event","name":"face_detected","data":{}}
{"type":"event","name":"face_lost","data":{}}
{"type":"event","name":"sound_event","data":{"direction":"left","balance":0.997,"energy":1807933247}}
```

Plumbing:

- **Firmware emit**: `Application::SendEvent(name, data_json)` in upstream `application.cc` (lazy-opens the WS via `OpenAudioChannel()` because xiaozhi WS is otherwise session-scoped — without lazy-open, perception events from idle silently drop).
- **xiaozhi-server relay**: custom override at `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py` adds an `EventTextMessageHandler` that POSTs each event frame to the bridge's `/api/perception/event`.
- **Bridge bus**: `_perception_listeners` pub/sub + `_perception_state[device_id]` per-device state in `bridge.py`, mirrored on the existing `_dashboard_event_listeners` pattern.
- **Consumers** (also bridge-side): `_perception_face_greeter` (Hi! greeting via `/xiaozhi/admin/inject-text`), `_perception_sound_turner` (head-turn via `/xiaozhi/admin/set-head-angles`), `_perception_face_lost_aborter` (TTS abort when audience walks away).

WS lifecycle is the structural fact most easily forgotten: **xiaozhi only opens the WS during a conversation**, not persistently. Anything that needs to fire a server-bound event from idle has to either (a) trigger `OpenAudioChannel()` first or (b) accept that events are session-only. Producer A and B both assume (a) — done in `SendEvent`.

For the design spec see [`docs/modes.md`](./docs/modes.md) "Designed but not yet implemented".

## Deeper reference

For hardware specs, protocol details, model internals, latent capabilities, and the behavioural mode + LED contract, see [`docs/README.md`](./docs/README.md) and its linked files (`architecture.md`, `hardware.md`, `voice-pipeline.md`, `brain.md`, `protocols.md`, `modes.md`, `latent-capabilities.md`, `references.md`).

## Tech Stack Refs

- xiaozhi-esp32-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- xiaozhi-esp32 firmware (upstream): https://github.com/78/xiaozhi-esp32
- ZeroClaw: https://github.com/zeroclaw-labs/zeroclaw
- StackChan (hardware + firmware patches): https://github.com/m5stack/StackChan
- Emotion protocol: https://xiaozhi.dev/en/docs/development/emotion/
