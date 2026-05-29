<p align="center">
  <img src="bridge/assets/dotty-hero.svg" alt="Dotty mascot" width="200">
</p>

# Dotty

**Your self-hosted [StackChan](https://github.com/m5stack/StackChan) robot assistant — kid-safe by default, hackable by design, private by architecture.**

> ⚠️ **Heads up: this is not a stable project yet.** Dotty is buggy, frequently broken, and actively changing day-to-day. End-to-end behaviour works on the maintainer's hardware but regressions land all the time, the API and config surface shifts without notice, and a fresh deploy on someone else's gear has not been verified. Treat this as a hobby-grade work-in-progress, not a polished product. Bugs, PRs, and "this didn't work for me" issues all very welcome. 🍺☕ If you do try a fresh end-to-end deploy, please get in touch — I'll buy you a beer or a coffee. The best place to ask questions, get help, or show off a build is the [Dotty community Discord](https://discord.gg/7sKE5c6A).
>
> **Known rough edges:** face emoji rendering is missing visual differentiation for 4 of 9 emotions (sad / surprise / love / laughing); sound-direction localizer has a hardware-AEC-related left-bias on M5Stack CoreS3 (energy detection works, direction is unreliable); kid-voice ASR accuracy on SenseVoice has a kid-speech gap that whisper.cpp will close in a follow-up.

Dotty is a fully self-hosted voice stack for the M5Stack StackChan desktop robot. Open-source firmware on the robot, [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) for voice I/O, and a local **pi** coding agent as the brain. ASR, TTS, and session state all run on your own hardware. The LLM is pluggable — the shipped default runs a small fast model for plain conversation and escalates hard questions to a more capable model, with [llama-swap](./docs/cookbook/llama-swap-concurrent-models.md) as the recommended local backend. Swap in [Ollama](./docs/cookbook/run-fully-local.md) for the simpler single-binary option, or point at OpenRouter / any OpenAI-compatible API if you'd rather use the cloud.

Out of the box, Dotty ships in **Kid Mode** — age-appropriate language, safety guardrails, and content filtering are on by default. Disable Kid Mode for a general-purpose assistant.

## Why I built this

I didn't like the idea of a camera and microphone running in my house unless I could (1) self-host the whole stack end-to-end and (2) understand the whole stack end-to-end. Off-the-shelf voice assistants fail both tests — audio leaves the house, the model is opaque, and you're trusting a vendor's privacy posture forever.

So Dotty is the version that passes: every component runs on hardware I own, every seam is documented and swappable, and the only thing that can leave the LAN is whatever LLM call I explicitly route out (and even that swaps to a local model with a config change). It's also meant to be fun — a friendly desktop robot for the kids, and an interesting hobby project to keep building on.

## Features

- **Kid Mode (on by default)** — age-appropriate responses, content filtering, and safety guardrails. Toggle off for general-purpose use. See [`docs/kid-mode.md`](./docs/kid-mode.md).
- **Local ASR** — FunASR SenseVoiceSmall runs on your hardware, no cloud transcription.
- **Local or cloud TTS** — Piper (offline) or EdgeTTS (cloud). Swap with a config change.
- **Streaming responses** — the bridge streams LLM output to the voice pipeline for lower perceived latency.
- **Emoji expressions** — every response starts with an emoji that the firmware maps to a face animation (smile, laugh, sad, surprise, thinking, angry, love, sleepy, neutral).
- **Voice tools** — the pi agent can search its memory, escalate hard questions to a bigger model, take a photo, and play songs, all mid-conversation.
- **States, toggles & LEDs** — a six-state mutex (`idle / talk / story_time / security / sleep / dance`) plus two orthogonal toggles (`kid_mode`, `smart_mode`), all owned by the firmware StateManager and surfaced on the 12-pixel LED ring. Shipped on the active firmware fork (commit `d78118b`, 2026-04-27); the `firmware/firmware/` submodule pin in this repo lags, so flash from the active fork to get it. See "States, Toggles & LEDs" below and [`docs/modes.md`](./docs/modes.md).
- **Vision (camera)** — the robot's built-in camera can capture images for multimodal LLM queries.
- **Calendar context** — optional calendar integration feeds upcoming events into the conversation context.
- **Hackable** — every seam is swappable: LLM, TTS, ASR, agent framework. Fork it, rip out what you don't want, wire in your own.

## States, Toggles & LEDs

Behaviour is a **six-state mutex** (`idle / talk / story_time / security / sleep / dance`) plus two orthogonal toggles (`kid_mode`, `smart_mode`), all owned by the firmware StateManager (shipped on the active fork in commit `d78118b`, 2026-04-27; bench checks tracked in [#38](https://github.com/BrettKinny/dotty-stackchan/issues/38)). Voice phrases, camera edges, and dashboard controls all flow through it.

> Note: the `firmware/firmware/` submodule pin in this repo deliberately lags the active fork — flashing from the submodule won't give you Phase 4 yet. See the "Firmware iteration" section in [`CLAUDE.md`](./CLAUDE.md) and the submodule-pin caveat in [`docs/modes.md`](./docs/modes.md).

The 12-pixel LED ring shows the current state at a glance. **Left ring 0-5 is the state arc** — all six pixels paint the state colour, matching the dashboard's state buttons:

|   | State |
|---|---|
| ⚫ | `idle` — ambient awareness; ring off. Default. |
| 🟢 | `talk` — conversation engaged. |
| 🟠 | `story_time` — long-running interactive story. |
| ⚪ | `security` — watching the room (1 Hz white flash). |
| 🔵 | `sleep` — quiescent, mic open for "wake up". |
| 🟣 | `dance` — rainbow sweep + choreography. |

On the right ring, **indices 8-9 are toggle pips** for kid_mode (salmon pink) and smart_mode (orange), and **index 11 (bottom) lights red while you have the turn** (LISTENING). The `idle → talk` transition fires on `face_detected` from the firmware; VLM identity recognition runs in parallel and feeds the LLM context.

Full state taxonomy, colour palette, transition diagram, and per-state backing architecture: [`docs/modes.md`](./docs/modes.md).

## Web dashboard (locally hosted)

The dashboard service serves a web dashboard at `http://<XIAOZHI_HOST>:8081/ui` — host status, mode toggles (Kid Mode / Smart Mode), state switcher, perception card (face / identity), emoji presets, and a live event log (turns, perception events, errors). Light and dark themes follow the system preference. It runs as a small FastAPI service (`bridge.py`) on your own hardware — no external service ever sees your data.

<p align="center">
  <img src="docs/assets/dashboard-light.png" alt="Dotty dashboard — light theme" width="48%">
  &nbsp;
  <img src="docs/assets/dashboard-dark.png" alt="Dotty dashboard — dark theme" width="48%">
</p>

## Reference deployment

- **Hardware**: M5Stack StackChan (CoreS3 + servo kit), firmware built from `m5stack/StackChan`.
- **Brain**: a **pi** coding agent running in the `dotty-pi` container. It runs `qwen3.5:4b` on local [llama-swap](./docs/cookbook/llama-swap-concurrent-models.md) for the conversation loop and escalates hard questions to `qwen3.6:27b-think` (also on llama-swap) via its `think_hard` tool. xiaozhi-server's `PiVoiceLLM` provider hands each voice turn to the agent. See [`docs/brain.md`](./docs/brain.md).
- **Voice I/O**: xiaozhi-esp32-server on Docker (any Linux Docker host).

## What runs where

| Component | Host | Notes |
|---|---|---|
| StackChan (device) | ESP32-S3 on the desk | Firmware built from `m5stack/StackChan` (see `SETUP.md`) |
| xiaozhi-esp32-server | server (`<XIAOZHI_HOST>`) | Docker — voice I/O, ports 8000 + 8003 |
| dotty-pi | server (`<XIAOZHI_HOST>`) | Docker — the pi agent, Dotty's voice brain |
| dotty-behaviour | server (`<XIAOZHI_HOST>`) | Docker — FastAPI: perception bus, ambient consumers, vision, greeter; port 8090 |
| dashboard service | server (`<XIAOZHI_HOST>`) | Docker — FastAPI admin dashboard (`bridge.py`); port 8081 |
| Admin workstation | any LAN box | Development / `ssh` only |

## Get it running

The stack is the device plus four server-side pieces — xiaozhi-server (voice I/O), `dotty-pi` (the pi agent brain), `dotty-behaviour` (perception, ambient behaviour, and the proactive greeter), and the admin dashboard service. The four server pieces run as Docker containers on a single Docker host, alongside a local model backend ([llama-swap](./docs/cookbook/llama-swap-concurrent-models.md), or [Ollama](./docs/cookbook/run-fully-local.md) for the simpler single-binary option).

Then:

- [`docs/quickstart.md`](./docs/quickstart.md) — 15-minute happy path: flash, configure, first turn. Includes placeholder substitution table, deployment layout, endpoints, reboot survival, and common ops snippets.
- [`docs/troubleshooting.md`](./docs/troubleshooting.md) — symptom-first lookup for common (and obscure) failure modes.

## Deeper reference

For what the stack *is* underneath — hardware specs, protocol docs, model facts, and features we aren't using — see [`docs/`](./docs/README.md):

- [docs/architecture.md](./docs/architecture.md) — end-to-end data flow, topology, deployment files, admin surface, perception bus, threat model.
- [docs/hardware.md](./docs/hardware.md) — M5Stack StackChan body + firmware lineage + on-device MCP tool catalog.
- [docs/voice-pipeline.md](./docs/voice-pipeline.md) — xiaozhi-esp32-server internals, FunASR/SenseVoice, VAD, TTS.
- [docs/brain.md](./docs/brain.md) — model matrix, the pi agent runtime, and how voice turns reach it.
- [docs/protocols.md](./docs/protocols.md) — Xiaozhi WS framing, MCP-over-WS, pi RPC, the dashboard HTTP API, emotion channel.
- [docs/modes.md](./docs/modes.md) — behavioural mode taxonomy + LED contract + transition diagram (with shipped-vs-planned breakdown).
- [docs/latent-capabilities.md](./docs/latent-capabilities.md) — features upstream supports that we aren't using yet.
- [docs/references.md](./docs/references.md) — canonical upstream URLs, model cards, licenses.

## References

- xiaozhi-esp32-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- xiaozhi-esp32 firmware (upstream): https://github.com/78/xiaozhi-esp32
- StackChan (hardware + open firmware): https://github.com/m5stack/StackChan
- Emotion protocol: https://xiaozhi.dev/en/docs/development/emotion/
