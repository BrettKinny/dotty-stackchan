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
- **Length-aware brevity** -- default 1-2 short sentences, up to 6 for open-ended asks (story, explanation, list); cap enforced in code via `MAX_SENTENCES`
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

Actively being worked on or partially complete. **Big push 2026-04-25 evening:** ~26 commits scaffolding much of what was previously "Planned" — see [CHANGELOG.md](CHANGELOG.md) `[Unreleased]` for the full inventory. Most items below have code on `main` but are not yet deployed live or fully wired.

- **Fully-local Ollama profile** -- `compose.local.override.yml` with NVIDIA GPU passthrough (compose file shipped, needs model pull + testing with dual RTX 3060s)
- **CI pipeline** -- YAML lint, compose validation, config parse check, firmware dry-build, docs link check
- **Firmware release workflow** -- GitHub Actions building `.bin` artifacts on tag push
- **Quickstart improvements** -- linear "flash, clone, configure, talk" path assuming published firmware releases
- **First-audio latency reduction** -- p50 1.9s with Mistral 3.2 (down from 5s with Qwen3-30B); next lever is self-hosted Ollama on local GPU.
- **ASR accuracy for children's speech** -- post-ASR corrections live; Whisper Phase 1 scaffold landed at v0.1; A/B verification pending.
- **Face detection + tracking** -- shipped firmware-side; smoother+faster tuning queued (EMA 0.5, speed 500, deadband, MSR thr 0.40). Flash + bench-test pending.
- **Layer 4 identity (description-based)** -- shipped + deployed. VLM (Gemini 2.0 Flash) returns a free-form description plus a roster name match against `~/.zeroclaw/household.yaml`'s `appearance:` field. No biometrics, no persistent identifiers. The earlier dlib biometric scaffold (`bridge/face_db.py` + `face_recognizer.py` + on-device `FaceRecognizer` + `ParentalGate` + 4 MCP tools) was removed — description-based covers the use case and biometrics conflicted with the no-storage identity posture.
- **Layer 6 proactive greetings** -- `bridge/proactive_greeter.py` + lifespan wiring shipped. Cooldown + time-of-day windowing + kid-safe sandwich + calendar-aware prompt + template fallback. Depends on Layer 4 for named greetings; works today with `face_detected` (unknown identity) for generic.
- **Layer 1 privacy-indicator LEDs** -- firmware scaffold drives mic/camera state via RAII peripheral guards. Camera `VIDIOC_STREAMOFF` wiring deferred (closes the always-streaming hole; queued).
- **Hybrid smart-mode LED** -- both halves shipped (firmware `set_led_multi` MCP tool + bridge `_send_led_multi` helper). Holds purple pixel during smart-mode turns. Re-asserts on color changes since firmware bypasses animation.
- **Head-pet hold-to-listen wake** -- firmware scaffold (`WakeWordInvoke("head_pet_hold")` after 2s touch). Works in the dark. Also emits `head_pet_started`/`_ended` perception events for purr consumer.
- **Wake word "Hey Dotty"** -- interim shipped: firmware default switched Chinese → English "Hi, ESP". Custom "Hey Dotty" microWakeWord roadmap documented (`docs/wake-word.md`); needs sample collection + Colab training (~2 weeks calendar).
- **Purr-on-head-pet** -- server consumer shipped (`_perception_purr_player`); fires on `head_pet_started`. Asset path `bridge/assets/purr.opus` is a drop-in (asset itself not committed).
- **Servo speed caps** -- already capped at 100-1000 in firmware; spring-physics damping handles smoothness. No further work planned.
- **Abort race condition** -- fixed at v0.1 via kill-and-respawn ACP child on barge-in.
- **Dancing mode** -- shipped at v0.1; karaoke + LLM-initiated dance + Phase 2 vocal singing remain.
- **Voice catalog + install helper** -- `docs/voice-catalog.md` (12 Piper + 6 EdgeTTS) + `make voice-install`.
- **Versioned docs via mike** -- `/latest/`, `/v0.1/`, `/dev/` URL structure scaffolded; first deploy run pending.
- **Observability hooks** -- Prometheus `/metrics` + Grafana dashboard scaffold shipped. Deploy on RPi pending.
- **Reproducible + signed firmware builds** -- SBOM + signed-releases scaffolds shipped. Maintainer GPG key + IDF Dockerfile SHA256 pin pending.

## Planned

Designed but not yet started. Roughly in priority order.

- **Speech bubble sync** -- tie on-screen text bubble visibility to actual audio playback state (deferred at v0.1 — Brett says timing looks fine in practice)
- **Singing mode** -- vocal synthesis or pitch-shifted TTS over backing tracks (Phase 2 of dance work)
- **Runtime OTA provisioning** -- captive-portal WiFi + OTA URL setup on first boot (no rebuild to retarget)
- **Layer 2.5 stereo mic + camera person tracking** -- sound-source localization + camera fusion for 360° awareness in idle mode
- **Phase 3 continuous vision classifier** -- EfficientDet/YOLOX at 1Hz on the Unraid GPU once dual RTX 3060s land
- **Variant board port guide** -- walkthrough for adding support for other ESP32-S3 boards

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
