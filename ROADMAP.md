# Roadmap

> This is a living document. See [CONTRIBUTING.md](CONTRIBUTING.md) to get involved.

## Shipping now (v0.1)

v0.1 is the first tagged release — early-feedback alpha. Everything in this list runs end-to-end on the maintainer's hardware. v1.0 is gated on real-world feedback from external users; see [Known issues](#known-issues-as-of-v01) below.

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
- **Kid Mode channel routing** -- voice channels are kid-safe by default; the bridge's kid-mode sandwich (English-pin, emoji prefix, topic blocklist, jailbreak resistance) only applies when the inbound `channel` is in `VOICE_CHANNELS`, so messaging-platform channels (Discord, Telegram, etc.) skip it automatically. Pair with a separate ZeroClaw daemon on a more capable model for an adult-mode chat surface
- **Bridge `/admin/*` endpoints** -- localhost-only HTTP API for runtime config mutation: toggle kid-mode (`/admin/kid-mode`), overwrite persona files (`/admin/persona`), swap a daemon's `default_model` in its `config.toml` (`/admin/model`), and amend the MCP tool allowlist (`/admin/safety`, py_compile-validated). Paths and systemd unit names are env-configurable

## Known issues (as of v0.1)

The 30+ planning docs accumulated during the v0.1 prep sprint surfaced these. None are blockers for trying Dotty out, but you should know about them:

- **Face emoji rendering** — only 5 of 9 enforced emotions render distinctly on the LCD. Sad clamps to a one-eye wink (rotation `-400` clamps to 0 on left eye), Surprise is byte-identical to Neutral (weight `120` clamps to `100`), Loving is a copy-paste of Happy, Laughing is an alias of Happy by design. Fix is queued (~25-40 LoC firmware patch).
- **Sound-direction localizer always reads left.** I2S channel 1 on the M5Stack CoreS3 is the AEC speaker-loopback reference, not the right mic. Energy detection works; direction does not. Sound-driven head-turn behaves accordingly.
- **Kid-voice ASR accuracy** — SenseVoiceSmall mangles short kid utterances ("macarena" → "maarna"). Post-ASR corrections + phrase boost help but have hit their ceiling. whisper.cpp / faster-whisper swap planned (Phase 1 CPU-only ships immediately, Phase 2 GPU once dual RTX 3060s arrive).
- **Privacy-indicator LEDs not yet hardwired.** The camera streams DMA buffers permanently after init; mic + camera enable are software-controlled with no hardware-guaranteed indicator. **Hard prereq for face recognition / continuous vision; do not ship those features without it.**
- **Smart Mode regression** (fixed in v0.1 itself) — between `434988d` and the v0.1 fix, every voice "smart mode" trigger silently fell back to the default model. If you're forking from before the v0.1 tag, pull the fix.

## In progress

Actively being worked on or partially complete.

- **Fully-local Ollama profile** -- `compose.local.override.yml` with NVIDIA GPU passthrough (compose file shipped, needs model pull + testing with dual RTX 3060s)
- **CI pipeline** -- YAML lint, compose validation, config parse check, firmware dry-build, docs link check
- **Firmware release workflow** -- GitHub Actions building `.bin` artifacts on tag push
- **Quickstart improvements** -- linear "flash, clone, configure, talk" path assuming published firmware releases
- **First-audio latency reduction** -- p50 1.9s with Mistral 3.2 (down from 5s with Qwen3-30B); next lever is self-hosted Ollama on local GPU.
- **ASR accuracy for children's speech** -- post-ASR corrections live; whisper.cpp / faster-whisper swap planned (CPU-first, GPU follow-up).

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
