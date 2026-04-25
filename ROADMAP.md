# Roadmap

> This is a living document. See [CONTRIBUTING.md](CONTRIBUTING.md) to get involved.

## Shipping now (v1.0)

These features are implemented and running on production hardware.

- **Kid Mode** -- opt-in child-safety guardrails: topic blocklist, self-harm redirect, content filter, age-appropriate vocabulary (on by default, disable with `DOTTY_KID_MODE=false`)
- **Local ASR** -- FunASR SenseVoiceSmall, English-pinned, runs on your Docker host
- **Local TTS** -- Piper voice synthesis, no cloud dependency
- **Streaming LLM responses** -- NDJSON token-level streaming with first-token latency ~1.2s
- **Emoji-driven expressions** -- LLM output prefixed with emoji; firmware maps to face animations
- **Persona system** -- swappable persona files (`personas/*.md`), customizable via `make setup`
- **MCP tool integration** -- 11 firmware-advertised tools (head servos, LEDs, camera, reminders, volume, brightness, screen theme)
- **Photo-based vision** -- "What do you see?" triggers camera capture + vision model description
- **Calendar context injection** -- Google Calendar events surfaced to the LLM for contextual reminders
- **3-sentence response cap** -- keeps voice replies short and kid-friendly
- **ASR noise filtering** -- rejects punctuation-only / sub-threshold utterances
- **ACP session caching** -- long-lived sessions with idle/turn-count/wall-clock rotation
- **Single-host deployment** -- `compose.all-in-one.yml` runs everything on one machine
- **Multi-host deployment** -- documented split across Docker host + RPi
- **`make setup` wizard** -- interactive first-run: name your robot, fetch models, validate config
- **MkDocs Material docs site** -- architecture, protocols, quickstart, troubleshooting, FAQ

## In progress

Actively being worked on or partially complete.

- **Fully-local Ollama profile** -- `compose.local.override.yml` with NVIDIA GPU passthrough (compose file shipped, needs model pull + testing with dual RTX 3060s)
- **CI pipeline** -- YAML lint, compose validation, config parse check, firmware dry-build, docs link check
- **Firmware release workflow** -- GitHub Actions building `.bin` artifacts on tag push
- **Quickstart improvements** -- linear "flash, clone, configure, talk" path assuming published firmware releases
- **First-audio latency reduction** -- profiled at 5-7s (LLM-dominated); local model hosting is the primary lever
- **ASR accuracy for children's speech** -- post-ASR corrections live; Whisper alternative planned for local GPU

## Planned

Designed but not yet started. Roughly in priority order.

- **Face detection + tracking** -- ESP-WHO on the ESP32-S3; Dotty follows you with its head via `lookAtNormalized()` servo API
- **Face recognition + proactive greetings** -- on-device face enrollment; Dotty greets your family by name with calendar context ("Hey, library day!")
- **Servo speed caps** -- firmware-level velocity/acceleration guard to keep head motion calm and predictable
- **Speech bubble sync** -- tie on-screen text bubble visibility to actual audio playback state
- **Abort race condition fix** -- suppress late-arriving LLM chunks when the user interrupts mid-response
- **Dancing mode** -- choreographed servo sequences synced to audio (Macarena first)
- **Singing mode** -- lightweight vocal synthesis or pitch-shifted TTS over backing tracks
- **Privacy-indicator LEDs** -- hardware-guaranteed LED tied to mic/camera peripheral enable (prerequisite for always-on face detection)
- **Kid Mode channel routing** -- voice channel kid-safe, Discord channel adult-mode by default
- **Runtime OTA provisioning** -- captive-portal WiFi + OTA URL setup on first boot (no rebuild to retarget)

## Community wishlist

Ideas we would welcome help with. None are blockers.

- **ESP Web Tools web flasher** -- one-click browser flash via `esptool.js` on GitHub Pages
- **Voice catalog + install helper** -- curated Piper/EdgeTTS voices with a download script
- **Versioned docs via `mike`** -- `/latest/` + `/v1.0/` so older firmware users see matching docs
- **Observability hooks** -- Prometheus metrics on the bridge (latency, token counts, error rates) + starter Grafana dashboard
- **Variant board port guide** -- walkthrough for adding support for other ESP32-S3 boards
- **Face/emoji asset catalog** -- document the expression-id-to-emoji mapping; show how to add a new face
- **Firmware/server compatibility matrix** -- pin which server versions work with which firmware versions
- **`make audit` network verifier** -- user-runnable tool to confirm "local except LLM" claim against their own install
- **Reproducible + signed firmware builds** -- toolchain-pinned `.bin` with GPG-signed release artifacts
