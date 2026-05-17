<p align="center">
  <img src="bridge/assets/dotty-hero.svg" alt="Dotty mascot" width="200">
</p>

# Dotty

**Your self-hosted [StackChan](https://github.com/m5stack/StackChan) robot assistant — kid-safe by default, hackable by design, private by architecture.**

> ⚠️ **Heads up: this is not a stable project yet.** Dotty is buggy, frequently broken, and actively changing day-to-day. End-to-end behaviour works on the maintainer's hardware but regressions land all the time, the API and config surface shifts without notice, and a fresh deploy on someone else's gear has not been verified. Treat this as a hobby-grade work-in-progress, not a polished product. Bugs, PRs, and "this didn't work for me" issues all very welcome. 🍺☕ If you do try a fresh end-to-end deploy, please get in touch — I'll buy you a beer or a coffee.
>
> **Known rough edges:** face emoji rendering is missing visual differentiation for 4 of 9 emotions (sad / surprise / love / laughing); sound-direction localizer has a hardware-AEC-related left-bias on M5Stack CoreS3 (energy detection works, direction is unreliable); kid-voice ASR accuracy on SenseVoice has a kid-speech gap that whisper.cpp will close in a follow-up.

Dotty is a fully self-hosted voice stack for the M5Stack StackChan desktop robot. Open-source firmware on the device, [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) for voice I/O, and a small FastAPI bridge to whatever LLM agent you want as the brain. ASR, TTS, and session state all run on your own hardware. The LLM is pluggable — the shipped default is a two-tier path (a small fast model handles plain chat; tool calls escalate to a more capable model), with [llama-swap](./docs/cookbook/llama-swap-concurrent-models.md) as the recommended local backend. Swap in [Ollama](./docs/cookbook/run-fully-local.md) for the simpler single-binary option, or point at OpenRouter / any OpenAI-compatible API if you'd rather use the cloud.

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
- **MCP tools** — ZeroClaw exposes tools (web search, memory, etc.) to the LLM via the Model Context Protocol.
- **States, toggles & LEDs** — `kid_mode` and `smart_mode` toggles are shipped and persist across reboots; smart-mode flips hot-swap the inner-loop LLM in-process (no daemon restart). The six-state mutex (`idle / talk / story_time / security / sleep / dance`) + 12-pixel LED ring are designed and partially wired bridge-side; the firmware `StateManager` that paints the ring is a Phase 4 work item not yet built. See "States, Toggles & LEDs" below and [`docs/modes.md`](./docs/modes.md).
- **Vision (camera)** — the StackChan's built-in camera can capture images for multimodal LLM queries.
- **Calendar context** — optional calendar integration feeds upcoming events into the conversation context.
- **Hackable** — every seam is swappable: LLM, TTS, ASR, agent framework. Fork it, rip out what you don't want, wire in your own.

## States, Toggles & LEDs

> Honesty note: the bridge-side machinery for the six-state model — perception bus, dashboard mirror, voice-phrase routing, `kid_mode` / `smart_mode` toggles — is shipped and working. The firmware `StateManager` that owns the on-device LED contract and emits `state_changed` events is a designed-but-not-yet-built Phase 4 deliverable. The table below describes the target model; see [`docs/modes.md`](./docs/modes.md) for which parts ship today.

Behaviour is modelled as a **six-state mutex** (`idle / talk / story_time / security / sleep / dance`) plus two orthogonal toggles (`kid_mode`, `smart_mode`). Voice phrases, camera edges, and dashboard controls all flow through it.

The 12-pixel LED ring will show the current state at a glance once Phase 4 ships. **Left ring 0-5 is the state arc** — all six pixels paint the state colour, matching the dashboard's state buttons:

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

The bridge serves a web dashboard at `http://<ZEROCLAW_HOST>:8080/ui` — host status, mode toggles (Kid Mode / Smart Mode), state switcher, perception card (face / identity), emoji presets, and a live event log (turns, perception events, errors). Light and dark themes follow the system preference. It's served from the same FastAPI process as the bridge, so there's nothing extra to deploy and no external service ever sees your data.

<p align="center">
  <img src="docs/assets/dashboard-light.png" alt="Dotty dashboard — light theme" width="48%">
  &nbsp;
  <img src="docs/assets/dashboard-dark.png" alt="Dotty dashboard — dark theme" width="48%">
</p>

## Reference deployment

- **Hardware**: M5Stack StackChan (CoreS3 + servo kit), firmware built from `m5stack/StackChan`.
- **Brain**: a two-tier voice path — `qwen3.5:4b` on local [llama-swap](./docs/cookbook/llama-swap-concurrent-models.md) handles plain conversational turns directly; tool calls escalate to `qwen3.6:27b-think` (also on llama-swap) for hard reasoning or to [ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw) for memory lookups. The legacy single-tier path (ZeroClaw + Qwen3-30B via OpenRouter on every turn) is still supported via `selected_module.LLM: ZeroClawLLM`. See [`docs/tier1slim.md`](./docs/tier1slim.md) and [`docs/brain.md`](./docs/brain.md).
- **Voice I/O**: xiaozhi-esp32-server on Docker (any Linux Docker host; single-host works too).

## What runs where

| Component | Host | Notes |
|---|---|---|
| StackChan (device) | ESP32-S3 on the desk | Firmware built from `m5stack/StackChan` (see `SETUP.md`) |
| xiaozhi-esp32-server | Docker host (`<XIAOZHI_HOST>`) | Docker, ports 8000 + 8003 |
| zeroclaw-bridge | ZeroClaw host (`<ZEROCLAW_HOST>`) | FastAPI on port 8080, systemd |
| ZeroClaw daemon | ZeroClaw host (`<ZEROCLAW_HOST>`) | `<ZEROCLAW_BIN>` |
| Admin workstation | any LAN box | Development / `ssh` only |

## Get it running

- [`docs/quickstart.md`](./docs/quickstart.md) — 15-minute happy path: flash, configure, first turn. Includes placeholder substitution table, deployment layout, endpoints, reboot survival, and common ops snippets.
- [`docs/troubleshooting.md`](./docs/troubleshooting.md) — symptom-first lookup for common (and obscure) failure modes.

## Deeper reference

For what the stack *is* underneath — hardware specs, protocol docs, model facts, and features we aren't using — see [`docs/`](./docs/README.md):

- [docs/architecture.md](./docs/architecture.md) — end-to-end data flow, topology, deployment files, admin surface, perception bus, threat model.
- [docs/hardware.md](./docs/hardware.md) — M5Stack StackChan body + firmware lineage + on-device MCP tool catalog.
- [docs/voice-pipeline.md](./docs/voice-pipeline.md) — xiaozhi-esp32-server internals, FunASR/SenseVoice, VAD, TTS.
- [docs/tier1slim.md](./docs/tier1slim.md) — the default two-tier voice LLM provider, escalation contract, hot-swap.
- [docs/brain.md](./docs/brain.md) — model matrix, ZeroClaw architecture, the FastAPI bridge.
- [docs/protocols.md](./docs/protocols.md) — Xiaozhi WS framing, MCP-over-WS, ACP JSON-RPC, bridge HTTP API, emotion channel.
- [docs/modes.md](./docs/modes.md) — behavioural mode taxonomy + LED contract + transition diagram (with shipped-vs-planned breakdown).
- [docs/latent-capabilities.md](./docs/latent-capabilities.md) — features upstream supports that we aren't using yet.
- [docs/references.md](./docs/references.md) — canonical upstream URLs, model cards, licenses.

## References

- xiaozhi-esp32-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- xiaozhi-esp32 firmware (upstream): https://github.com/78/xiaozhi-esp32
- ZeroClaw: https://github.com/zeroclaw-labs/zeroclaw
- StackChan (hardware + open firmware): https://github.com/m5stack/StackChan
- Emotion protocol: https://xiaozhi.dev/en/docs/development/emotion/
