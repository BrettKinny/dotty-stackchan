# Dotty

## AI transparency (binding on agents)

This is a human-focused project, made by humans, for humans, that is **openly
AI-assisted**. If you are an AI agent working here, the project's AI policy
([`AI_TRANSPARENCY.md`](./AI_TRANSPARENCY.md)) is binding on you, not just
descriptive. The core rule: **anything you author is acknowledged as such.** In
practice:

- **Keep the `Co-Authored-By:` trailer** naming your model on every commit you
  help write (the global commit/PR conventions already require this — honour
  them, never strip them).
- **Note AI assistance** in PR bodies you draft, and mark substantial AI-drafted
  docs as such.
- **Never present agent work as unaided human work**, and never remove existing
  attribution. Leave the human-accountability chain intact: you propose, a human
  reviews and is accountable for what lands. Don't merge to `main` unattended.

## What This Is

Your self-hosted StackChan robot assistant. A fully self-hosted voice stack for the M5Stack **StackChan** desktop robot. The default persona is "Dotty" (customizable via `make setup`). Voice I/O routes through a self-hosted xiaozhi-esp32-server; the brain is a **pi** coding agent running in the `dotty-pi` container. No cloud AI services — fully self-hosted except for the LLM call (replaceable with local Ollama).

## Architecture

The voice path runs through a single LLM provider — `PiVoiceLLM`, selected via `selected_module.LLM` in `data/.config.yaml`. One alternate provider ships as a fallback (`OpenAICompat`). (The former `Tier1Slim` two-tier provider was removed in the 2026-05-29 alignment pass — its tool escalation depended on the retired ZeroClaw bridge.)

```
                 StackChan hardware → configured persona
                   │  ESP32-S3, xiaozhi firmware (built from m5stack/StackChan source)
                   │  WiFi / WebSocket (Xiaozhi protocol)
                   ▼
                 xiaozhi-esp32-server (Docker)
                   ├─ ASR: FunASR SenseVoiceSmall / WhisperLocal (local)
                   ├─ TTS: LocalPiper; EdgeTTS / StreamingEdgeTTS alternates
                   └─ LLM: PiVoiceLLM
                        │  PiClient → `docker exec -i dotty-pi pi --mode rpc …`  (JSONL over stdio)
                        ▼
                 dotty-pi container — the pi coding agent (the brain)
                   ├─ outer loop: qwen3.5:4b on llama-swap
                   └─ dotty-pi-ext extension → 7 voice tools:
                        memory_lookup · remember · recall_person · remember_person · think_hard (→ qwen3.6:27b-think) · take_photo · play_song
                   only TTS-bound text streams back to xiaozhi-server

  Perception + ambient behaviour:  firmware `event` frames → xiaozhi relay → dotty-behaviour (FastAPI, :8090)
  Admin dashboard:                 bridge.py (FastAPI, :8081, served at /ui)
```

All four server-side services — xiaozhi-server, `dotty-pi`, `dotty-behaviour`, and the `bridge.py` dashboard — run as Docker containers on a single Docker host.

Smart-mode currently flips behaviour but **not** the backend model — the model-swap path was dropped in the #36 cutover and is v2 scope (see `docs/cutover-behaviour.md`).

> **Cutover note:** until the #36 cutover (executed 2026-05-19) the brain was **ZeroClaw**, a Rust AI-agent fronted by a FastAPI bridge on a separate Raspberry Pi. That path — ZeroClaw, the ACP protocol, the `ZeroClawLLM` provider, and the RPi host — has been retired. `bridge.py` survived as the dashboard service; its voice and perception roles moved to `dotty-pi` and `dotty-behaviour`. Historical record: `docs/cutover-behaviour.md`.

See `README.md` for the full visual architecture and message-flow diagrams.

## Network

- **Admin workstation** (this machine): Development/admin workstation. Runs Claude Code sessions.
- **Docker host**: runs xiaozhi-esp32-server, `dotty-pi`, `dotty-behaviour`, and the `bridge.py` dashboard — all as containers. Any Linux box with Docker works. Reachable on the LAN (and optionally Tailscale).
- **StackChan**: On LAN WiFi only (not on Tailnet). Needs LAN IPs for OTA and WebSocket.

SSH access is via Tailscale hostnames. Discover actual Tailscale hostnames at runtime with `tailscale status`.

This repo uses placeholders (`<XIAOZHI_HOST>`, `<XIAOZHI_USER>`, `<XIAOZHI_PATH>`, etc.) everywhere real values would normally appear — see the "Configuring for your environment" section of `README.md` for the full list.

## Key Paths

- **xiaozhi-server install dir** (on the Docker host): `<XIAOZHI_PATH>` (e.g. `/opt/xiaozhi-server/`)
- **Custom LLM provider** (on the Docker host): mounted into the xiaozhi container at `/opt/xiaozhi-server/core/providers/llm/pi_voice/`
- **dotty-pi / dotty-behaviour / bridge.py**: each deployed as its own container on the Docker host (see their respective `README.md` files; deploy via `scripts/deploy-behaviour.sh` and `scripts/deploy-bridge-unraid.sh`)
- **This project dir**: wherever you cloned `dotty-stackchan`

## Ports

| Service | Host | Port | Protocol |
|---------|------|------|----------|
| xiaozhi WebSocket | Docker host LAN IP | 8000 | ws:// |
| xiaozhi OTA/HTTP | Docker host LAN IP | 8003 | http:// |
| dashboard service (`bridge.py`) | Docker host LAN IP | 8081 | http:// (`/ui`) |
| dotty-behaviour (perception, vision, greeter) | Docker host LAN IP | 8090 | http:// |

## Config Files to Know

- `.config.yaml` (repo root; deployed to the Docker host as `data/.config.yaml`) — the xiaozhi-server override config. Never overwrite wholesale on upgrades; merge keys.
- `custom-providers/pi_voice/` — the **`PiVoiceLLM` provider** + `PiClient`, the default voice path. xiaozhi-server's LLM call is translated into a pi RPC request and run inside the `dotty-pi` container via `docker exec -i dotty-pi pi --mode rpc …`; pi owns the agent loop and tools, and only TTS-bound text streams back. Selected when `selected_module.LLM = PiVoiceLLM`. Requires the host docker socket bind-mounted into the xiaozhi container — see `custom-providers/pi_voice/README.md`.
- `custom-providers/edge_stream/edge_stream.py` — custom streaming TTS provider. Mounted similarly.
- `custom-providers/openai_compat/openai_compat.py` — OpenAI-compatible LLM provider; the alternate voice backend to `PiVoiceLLM` (point it at a local llama-swap endpoint or any OpenAI-compatible API). Selected when `selected_module.LLM = OpenAICompat`.
- `custom-providers/piper_local/piper_local.py` — local Piper TTS provider (offline alternative to EdgeTTS).
- `custom-providers/asr/fun_local.py` — patched FunASR provider. Adds a `language` config key (upstream hardcodes `"auto"`, which mis-detects Korean/Japanese on unclear English). Mounted as a file-level override over the upstream provider.
- `custom-providers/xiaozhi-patches/{http_server,websocket_server,portal_bridge}.py` — drop-in overrides against upstream xiaozhi-server. Add the `/xiaozhi/admin/*` admin routes (inject-text, abort, set-state, set-toggle, set-head-angles, take-photo, play-asset, songs catalogue, say) and the `active_connections` registry that lets admin routes reach a live device WS. (The `set-tier1slim-model` route and `shared_llm` singleton were removed with Tier1Slim in the 2026-05-29 alignment pass.)
- `bridge.py` — the **admin dashboard** service (FastAPI, port 8081, served at `/ui`); runs as a container on the Docker host (build via `bridge/Dockerfile`, deploy via `scripts/deploy-bridge-unraid.sh`). Its former voice and perception-bus roles were retired in #36; the dashboard now pulls its perception/vision/audio cards from `dotty-behaviour` (#115 series). Supporting modules live under `bridge/`.
- `dotty-pi/` — Docker image + compose for the pi agent container (the brain). See `dotty-pi/README.md`.
- `dotty-pi-ext/` — pi extension providing the **seven** voice tools (`memory_lookup`, `remember`, `recall_person`, `remember_person`, `think_hard`, `take_photo`, `play_song`), loaded into the `dotty-pi` agent. (`recall_person`/`remember_person` were added in #53.)
- `dotty-behaviour/` — FastAPI service (port 8090): the perception event bus, ambient consumers, vision/audio explain endpoints, the proactive greeter, and calendar context. Successor to the bridge's perception role. See `dotty-behaviour/README.md`.
- `personas/default.md` — default robot persona prompt (swappable).
- `session-prompt.md` — Claude Code session prompt for infrastructure setup.

## Emotion/Expression Protocol

The LLM response MUST start with an emoji. The xiaozhi firmware parses it into a face animation:
😊=smile 😆=laugh 😢=sad 😮=surprise 🤔=thinking 😠=angry 😐=neutral 😍=love 😴=sleepy

Two layers enforce this on the live `PiVoiceLLM` path:
1. **The pi agent's persona prompt** (the configured persona) — primary source.
2. **xiaozhi-server top-level `prompt:`** in `data/.config.yaml` — injected as a system message.

The old third layer — a `_ensure_emoji_prefix` fallback in `bridge.py` — only ran on the retired ZeroClaw voice path; `PiVoiceLLM` has no equivalent, so the persona prompts are load-bearing.

## Key Directories

- `custom-providers/` — all custom ASR/LLM/TTS providers (mounted into the xiaozhi container)
- `bridge/` — supporting modules for the `bridge.py` dashboard service (dashboard UI, templates, static assets, CSRF, metrics)
- `dotty-pi/`, `dotty-pi-ext/`, `dotty-behaviour/` — the pi agent container, its voice-tool extension, and the perception/greeter service (see Config Files above)
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
- **Test the dashboard service**: `curl http://<XIAOZHI_HOST>:8081/health`
- **Test dotty-behaviour**: `curl http://<XIAOZHI_HOST>:8090/health`

## Firmware iteration

The `firmware/` directory at the root of this repo is a **git submodule** that pins a release of the StackChan firmware fork (`BrettKinny/StackChan` @ `dotty`). It exists so the public repo has a reproducible firmware-version pointer; updating it is a *release* action, not a development action. The build commands below operate inside that submodule and are appropriate for users who only have this repo cloned.

If you maintain a separate firmware checkout for active development (recommended for non-trivial firmware work — keeps the submodule clean and avoids accidental commits into a release pin), point the same `docker run` invocations at that checkout instead and bump the submodule pointer here only when cutting a release.

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

Forward-looking modes (face-detected greeting, sound-direction head-turn, future curiosity / boredom mode) all subscribe to a single perception event bus in `dotty-behaviour`. Producers are firmware-resident and emit JSON `event` frames over the WS:

```json
{"type":"event","name":"face_detected","data":{}}
{"type":"event","name":"face_lost","data":{}}
{"type":"event","name":"sound_event","data":{"direction":"left","balance":0.997,"energy":1807933247}}
```

Plumbing:

- **Firmware emit**: `Application::SendEvent(name, data_json)` in upstream `application.cc` (lazy-opens the WS via `OpenAudioChannel()` because xiaozhi WS is otherwise session-scoped — without lazy-open, perception events from idle silently drop).
- **xiaozhi-server relay**: custom override at `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py` adds an `EventTextMessageHandler` that POSTs each event frame to `dotty-behaviour`'s `/api/perception/event`.
- **dotty-behaviour bus**: the perception event bus + per-device state live in `dotty-behaviour` (`perception/state.py`, `perception/snapshot.py`).
- **Consumers** (`dotty-behaviour/consumers/`): `face_greeter` (Hi! greeting via `/xiaozhi/admin/inject-text`), `sound_turner` (head-turn via `/xiaozhi/admin/set-head-angles`), `face_lost_aborter` (TTS abort when audience walks away), and six more — see `dotty-behaviour/README.md`.

WS lifecycle is the structural fact most easily forgotten: **xiaozhi only opens the WS during a conversation**, not persistently. Anything that needs to fire a server-bound event from idle has to either (a) trigger `OpenAudioChannel()` first or (b) accept that events are session-only. Producer A and B both assume (a) — done in `SendEvent`.

The Phase 4 firmware **StateManager** (`firmware/main/stackchan/modes/state_manager.{h,cpp}`) is a producer too — it emits `state_changed` on every mutex-state transition (`idle / talk / story_time / security / sleep / dance`) so `dotty-behaviour` consumers can gate behaviour on state. `dotty-behaviour` tracks per-device `current_state` from those events.

## States, toggles & LEDs

`docs/modes.md` is the **authoritative source** for the six-state mutex (`idle / talk / story_time / security / sleep / dance`), the orthogonal toggles (`kid_mode`, `smart_mode`), the LED contract (state arc on left ring 0-5; face-state / kid / smart / listening indicators on right ring 6 / 8 / 9 / 11 with reserved pixels at 7 / 10 — all six right-ring pixels owned by StateManager and re-asserted at 5 Hz), the voice-phrase triggers, and the per-state backing-architecture (which states use the pi agent vs direct OpenRouter). When adding behaviour that responds to or changes Dotty's mode, read modes.md first — don't reinvent.

## Deeper reference

For hardware specs, protocol details, model internals, latent capabilities, and the behavioural mode + LED contract, see [`docs/README.md`](./docs/README.md) and its linked files (`architecture.md`, `hardware.md`, `voice-pipeline.md`, `brain.md`, `protocols.md`, `modes.md`, `latent-capabilities.md`, `references.md`).

## Tech Stack Refs

- xiaozhi-esp32-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- xiaozhi-esp32 firmware (upstream): https://github.com/78/xiaozhi-esp32
- StackChan (hardware + firmware patches): https://github.com/m5stack/StackChan
- Emotion protocol: https://xiaozhi.dev/en/docs/development/emotion/

## Agent skills

### Issue tracker

Issues live as GitHub issues on `BrettKinny/dotty-stackchan` (the `origin` remote), managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles use their default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), orthogonal to the existing `status:*` / `area:*` labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
