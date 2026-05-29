---
title: Architecture
description: Single-host architecture and message flow for the self-hosted voice stack (post-#36 cutover).
---

# Architecture

## TL;DR

- Two hosts: **robot** (StackChan on your desk) and a **single Docker host** (`<XIAOZHI_HOST>`) that runs all four server-side services.
- Audio goes robot → xiaozhi-server → (text) → dotty-pi → (response text) → xiaozhi-server → (audio) → robot. The Docker host never sends audio to the robot — xiaozhi-server handles that.
- The default voice provider is **`PiVoiceLLM`**, selected via `selected_module.LLM` in `.config.yaml`. One documented alternate exists (`OpenAICompat`) — see [llm-backends.md](./llm-backends.md).
- Everything is LAN-local **except** cloud-routed LLM calls (smart-mode, VLM, audio caption). EdgeTTS is cloud when selected; Piper is fully local.
- The robot speaks the **Xiaozhi WebSocket protocol** (see [protocols.md](./protocols.md)). It has no knowledge of the services running on the Docker host.

> **Cutover note (2026-05-19, issue #36):** The stack previously ran on three hosts — a separate ZeroClaw host (Raspberry Pi) ran the ZeroClaw Rust agent + a FastAPI bridge under systemd. That host has been retired. The brain is now the `dotty-pi` container; the voice provider is `PiVoiceLLM`. See [cutover-behaviour.md](./cutover-behaviour.md) for the historical runbook.

## Topology

```mermaid
flowchart LR
    subgraph Desk["Desk (LAN WiFi)"]
        SC["M5Stack StackChan<br/>ESP32-S3"]
    end

    subgraph DockerHost["Docker host — &lt;XIAOZHI_HOST&gt;"]
        XZ["xiaozhi-esp32-server<br/>:8000 WS + :8003 HTTP"]
        subgraph XZMods["voice pipeline"]
            VAD["SileroVAD"]
            ASR["FunASR SenseVoiceSmall<br/>/ WhisperLocal"]
            PV["PiVoiceLLM<br/>(default LLM provider)"]
            TTS["TTS<br/>(LocalPiper default,<br/>EdgeTTS available)"]
        end
        PI["dotty-pi<br/>(pi agent container)"]
        BH["dotty-behaviour<br/>FastAPI :8090"]
        BR["bridge.py<br/>FastAPI :8081 (/ui dashboard)"]
    end

    subgraph Llama["llama-swap (same host or LAN GPU host)"]
        SLIM["qwen3.5:4b<br/>(pi outer loop)"]
        THINK["qwen3.6:27b-think<br/>(think_hard target)"]
    end

    Cloud["OpenRouter<br/>(smart_mode, VLM,<br/>audio caption)"]

    SC -->|"WebSocket<br/>Xiaozhi protocol"| XZ
    XZ --- VAD & ASR & PV & TTS
    PV -->|"docker exec -i dotty-pi<br/>pi --mode rpc (JSONL stdio)"| PI
    PI -->|"outer loop"| SLIM
    PI -->|"think_hard escalation"| THINK
    PI -.->|"smart_mode ON"| Cloud
    XZ -->|"perception event<br/>POST /api/perception/event"| BH
    BH -.->|"VLM / audio caption"| Cloud
    TTS -->|"audio stream"| XZ
    XZ -->|"WebSocket"| SC
```

Solid arrows are per-turn data flow; dotted arrows are cloud / conditional. All four server-side services share one Docker host.

## Actors

| Actor | Host | Role | Process |
|---|---|---|---|
| **StackChan** | Desk | Captures audio, plays audio, renders face, runs MCP tools for head/LED/camera | ESP32-S3 firmware built from `m5stack/StackChan` |
| **xiaozhi-esp32-server** | Docker host | VAD → ASR → LLM (proxy) → TTS pipeline, emotion dispatch, OTA, admin surface | Docker container |
| **PiVoiceLLM custom provider** | Docker host (inside xiaozhi container) | Default LLM provider — translates each voice turn into a pi RPC request, streams TTS-bound text back | Python, mounted via volume |
| **dotty-pi** | Docker host | The voice-tool brain — pi coding agent with the `dotty-pi-ext` extension; owns the agent loop and tool dispatch | Docker container (`dotty-pi`) |
| **dotty-behaviour** | Docker host | Perception event bus, 11 consumer classes (the running set is config-gated), vision/audio explain endpoints, proactive greeter, calendar context | FastAPI container, port 8090 |
| **bridge.py** | Docker host | Admin dashboard service (`/ui`, port 8081). Voice and perception roles were retired in #36; dashboard port to dotty-behaviour is pending. | FastAPI container, port 8081 |
| **llama-swap** | Same host or LAN GPU host | Routes OpenAI-compatible requests to per-model llama-server children; co-loads `qwen3.5:4b` (pi outer loop) and `qwen3.6:27b-think` (`think_hard` target) | Docker container (`ghcr.io/mostlygeek/llama-swap:cuda`) |
| **OpenRouter** | Cloud | Routes cloud LLM calls (smart_mode `claude-sonnet-4-6`, VLM `gemini-2.0-flash`, audio caption `gemini-2.5-flash`) | External |

## Data flow (single utterance, PiVoiceLLM — normal turn)

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant SC as StackChan
    participant XZ as xiaozhi-server
    participant PV as PiVoiceLLM / PiClient
    participant PI as dotty-pi<br/>(pi agent)
    participant LS as llama-swap<br/>qwen3.5:4b

    User->>SC: speaks
    SC->>XZ: Opus audio frames (WebSocket)
    XZ->>XZ: SileroVAD → speech end
    XZ->>XZ: FunASR / Whisper → text
    XZ->>PV: generate() call
    PV->>PI: docker exec -i dotty-pi pi --mode rpc (JSONL over stdio)
    PI->>LS: chat/completions
    LS-->>PI: "😊 The sky is blue!"
    PI-->>PV: JSONL text chunks (TTS-bound only)
    PV-->>XZ: streamed text
    XZ->>XZ: strip leading emoji → emotion frame
    XZ->>XZ: TTS (Piper / EdgeTTS) → Opus frames
    XZ-->>SC: audio + emotion
    SC-->>User: speaks + face animation
```

## Data flow (PiVoiceLLM — tool call inside the agent)

Tool dispatch happens entirely inside the `dotty-pi` container. The pi agent with the `dotty-pi-ext` extension drives the tool loop; xiaozhi-server and PiVoiceLLM see only the final streamed text.

```mermaid
sequenceDiagram
    autonumber
    participant PI as dotty-pi<br/>(pi agent + dotty-pi-ext)
    participant LS4 as llama-swap<br/>qwen3.5:4b
    participant THK as llama-swap<br/>qwen3.6:27b-think
    participant BH as dotty-behaviour<br/>:8090

    PI->>LS4: chat/completions
    LS4-->>PI: tool_call: think_hard(question)
    PI->>THK: direct POST /v1/chat/completions<br/>(enable_thinking=false, 200-token cap)
    THK-->>PI: reasoned answer
    PI->>LS4: chat/completions (tool result in context)
    LS4-->>PI: streamed final answer

    Note over PI,BH: take_photo tool variant
    PI->>BH: GET /api/voice/take_photo
    BH-->>PI: latest cached vision description
```

The five voice tools in `dotty-pi-ext`: `memory_lookup`, `remember`, `think_hard`, `take_photo`, `play_song`. See [brain.md](./brain.md) for the full tool catalogue.

## Why this shape

- **Audio lives with xiaozhi-server** because the StackChan firmware already speaks the Xiaozhi WS protocol. xiaozhi-esp32-server is the matching server; any alternative would require reimplementing that protocol.
- **The brain is a container on the same host** because co-locating dotty-pi with xiaozhi-server eliminates network latency on the `docker exec` stdio pipe and avoids a second host to manage.
- **The seam is a custom xiaozhi LLM provider** (`pi_voice.py`, mounted into the container). xiaozhi-server thinks it's calling a local Python LLM class; the class runs pi via `docker exec`. That means the brain can be swapped for anything without touching xiaozhi.
- **dotty-behaviour is a peer container** rather than code inside xiaozhi-server because perception consumers fire 200-token narrative LLM calls; blocking the xiaozhi event loop with that would spike voice latency. A peer container preserves the operational separation that already worked on the Raspberry Pi.

## What each service sees

**StackChan** knows only:
- An OTA HTTP URL (`http://<XIAOZHI_HOST>:8003/xiaozhi/ota/`)
- A WS URL (provided by OTA response, typically `ws://<XIAOZHI_HOST>:8000/xiaozhi/v1/`)

It does **not** know about dotty-pi, dotty-behaviour, or any LLM.

**xiaozhi-server** knows:
- Its own device-facing WS + OTA ports
- A handful of pluggable providers selected via `data/.config.yaml` `selected_module:`
- The `container_name: dotty-pi` config key, which PiVoiceLLM uses for `docker exec`

It does **not** know about llama-swap model names, brain.db, or OpenRouter keys.

**dotty-pi (pi agent)** knows:
- The llama-swap endpoint and model aliases (via `models.json` inside the container)
- The `dotty-pi-ext` extension with the five voice tools
- The persona files and `brain.db` (mounted from host appdata)

It does **not** know about the xiaozhi WebSocket protocol or audio.

**dotty-behaviour** knows:
- The xiaozhi admin endpoints (for inject-text, set-head-angles, abort)
- Vision/audio caption API keys (for scene synthesis)

**bridge.py** (dashboard service) knows:
- The xiaozhi admin endpoints (for dashboard relay)
- Its voice and perception routing tables were retired in #36; dashboard port to dotty-behaviour is pending

## Admin surface (two services, two prefixes)

Admin routes are split across two services and reached at different prefixes.

### bridge.py `/admin/*` (Docker host, `127.0.0.1:8081` only)

Runtime mutations. Bound to localhost only — LAN callers get `403`. Note: the voice-path mutations (`smart-mode` daemon restart, `persona` workspace files) referenced pre-cutover ZeroClaw config; those endpoints exist but their ZeroClaw-specific side-effects are retired.

| Endpoint | Effect |
|---|---|
| `POST /admin/kid-mode` `{enabled: bool}` | Persists + hot-reloads kid-mode. Pushes the kid pip via the xiaozhi admin relay. |
| `POST /admin/smart-mode` `{enabled: bool, device_id?}` | Persists + pushes the smart pip. Smart-mode model swap now handled in PiVoiceLLM. |
| `POST /admin/safety` `{action, tool}` | Edits `MCP_TOOL_ALLOWLIST` via marker block; py_compile-validated. |

### xiaozhi-server `/xiaozhi/admin/*` (Docker host, port 8003)

Operations that need to touch a live device session — head servos, MCP dispatch, TTS injection. Exposed by `custom-providers/xiaozhi-patches/http_server.py`.

| Endpoint | Purpose |
|---|---|
| `POST /xiaozhi/admin/inject-text` | Speak arbitrary text through TTS as if Dotty originated it. Used by face-greeter and proactive prompts. |
| `GET /xiaozhi/admin/devices` | List connected device-ids. |
| `POST /xiaozhi/admin/abort` | Abort an in-flight TTS turn (used by the face-lost aborter). |
| `POST /xiaozhi/admin/set-head-angles` | Move the head servos (used by sound-direction turn). |
| `POST /xiaozhi/admin/set-state` | Dispatch a `set_state` MCP call to firmware. |
| `POST /xiaozhi/admin/set-toggle` | Dispatch a `set_toggle` MCP call (kid/smart pip on the firmware ring). |
| `POST /xiaozhi/admin/set-face-identified` | Light the face-identified pixel for ~4 s. |
| `POST /xiaozhi/admin/take-photo` | Trigger a camera capture. |
| `GET /xiaozhi/admin/songs` | List audio assets available to `play_song`. |
| `POST /xiaozhi/admin/play-asset` | Play a named audio asset through the speaker. |
| `POST /xiaozhi/admin/say` | Synthesise + play arbitrary text. |

### Perception event bus

Firmware-resident producers emit JSON `event` frames over the xiaozhi WebSocket:

```json
{"type":"event","name":"face_detected","data":{}}
{"type":"event","name":"face_lost","data":{}}
{"type":"event","name":"sound_event","data":{"direction":"left","balance":0.997,"energy":1807933247}}
```

The xiaozhi-server's `EventTextMessageHandler` (`custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py`) POSTs each frame to `dotty-behaviour`'s `POST /api/perception/event`. dotty-behaviour maintains the pub/sub bus (`dotty-behaviour/perception/state.py`) and runs 11 consumer classes against it (`dotty-behaviour/consumers/`); the actually-running set is env-gated at runtime:

| Consumer | What it does |
|---|---|
| `FaceGreeter` | "Hi!" greeting (via `/xiaozhi/admin/inject-text`) on first face detection after a cooldown window. |
| `SoundTurner` | Head-turn (via `/xiaozhi/admin/set-head-angles`) toward sound direction. |
| `FaceLostAborter` | Aborts an in-flight TTS turn (via `/xiaozhi/admin/abort`) when the audience walks away. |
| `WakeWordTurner` | Head-turn toward the speaker on wake-word event. |
| `FaceIdentifiedRefresher` | Re-asserts the face-identified pixel every ~3 s so the firmware's 4 s timeout doesn't drop it. |
| `PurrPlayer` | Plays an idle purr asset when conditions match. |
| `SceneSynthesis` | Ambient vision narrative + audio caption synthesis loop. |
| `IdlePhotographer` | Periodic idle-state camera capture for scene context. |
| `SleepDreamer` | Sleep-state ambient consumer. |
| `DanceReflector` | Reflects dance start/stop events into behaviour. |
| `SecurityCycle` | Security-state surveillance scaffolding (Phase 8 PENDING — not a live capture path). |

WebSocket lifecycle gotcha: xiaozhi only opens the WS during a conversation. Firmware-side perception producers must call `OpenAudioChannel()` first, or events from idle silently drop.

## Threat-model implications

- **Device compromise** gives an attacker a WS session to xiaozhi-server and the ability to invoke any server-exposed MCP tool. It does **not** give them LLM keys or network access to OpenRouter beyond what proxied prompts can achieve.
- **Docker host compromise** gives them access to all four services — xiaozhi-server, dotty-pi (with brain.db and persona files), dotty-behaviour, bridge.py. The `/admin/*` mutation endpoints on bridge.py are `127.0.0.1`-only.
- **OpenRouter compromise** gives log access to every prompt sent via cloud models. Treat prompts as non-confidential.

See [`ROADMAP.md`](ROADMAP.md) for related backlog items (privacy-indicator LEDs, child-safety hardening).

## Deployment files (this repo)

The canonical working copies live in this repo.

| File / Directory | Deployed to | Purpose |
|---|---|---|
| `dotty-pi/` | Docker host `/mnt/user/appdata/dotty-pi-src/` | pi agent container (Dockerfile + docker-compose.yml) |
| `dotty-pi-ext/` | Docker host (bind-mounted into dotty-pi) | dotty-pi-ext extension — five voice tools |
| `dotty-behaviour/` | Docker host `/mnt/user/appdata/dotty-behaviour-src/` | Perception + ambient behaviour container |
| `bridge.py` | Docker host (bridge.py container) | Admin dashboard FastAPI service |
| `bridge/requirements.txt` | bridge.py container | Pinned Python deps |
| `custom-providers/pi_voice/` | xiaozhi container `core/providers/llm/pi_voice/` | PiVoiceLLM + PiClient |
| `custom-providers/openai_compat/` | xiaozhi container `core/providers/llm/openai_compat/` | OpenAICompat alternate provider |
| `custom-providers/edge_stream/` | xiaozhi container `core/providers/tts/` | Streaming EdgeTTS provider |
| `custom-providers/piper_local/` | xiaozhi container `core/providers/tts/` | Local Piper TTS provider |
| `custom-providers/asr/fun_local.py` | xiaozhi container `core/providers/asr/` | Patched FunASR provider (adds `language` config key) |
| `custom-providers/xiaozhi-patches/` | xiaozhi container (drop-in overrides) | Admin routes + shared_llm singleton |
| `.config.yaml` | Docker host `data/.config.yaml` | xiaozhi-server config override |
| `docker-compose.yml.template` | Docker host `<XIAOZHI_PATH>` | Container definition |
| `scripts/deploy-behaviour.sh` | run from admin workstation | Deploy dotty-behaviour to Docker host |

Volume mounts (xiaozhi-server) are listed in [quickstart.md](./quickstart.md#deployment-layout).

## See also

- [hardware.md](./hardware.md) — what the robot actually is.
- [voice-pipeline.md](./voice-pipeline.md) — what xiaozhi-server runs.
- [brain.md](./brain.md) — the pi agent, model matrix, and dotty-pi-ext tools.
- [protocols.md](./protocols.md) — what's on the wire (pi RPC mode, `/api/perception/event`).
- [quickstart.md](./quickstart.md) — deployment placeholders, volume mounts, common ops.
- [llm-backends.md](./llm-backends.md) — choosing between PiVoiceLLM and OpenAICompat.

Last verified: 2026-05-22.
