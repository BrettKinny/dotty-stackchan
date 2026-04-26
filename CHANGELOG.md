# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). From v0.1.0 forward this project follows [Semantic Versioning](https://semver.org/). Server and firmware tag independently as `server-vX.Y.Z` and `fw-vX.Y.Z`; see `COMPATIBILITY.md` for the matrix.

## [Unreleased]

Post-v0.1 work — code shipped to `main` but not yet deployed live or tagged. ~26 commits across the public server repo + the StackChan firmware fork during the 2026-04-25 evening sprint.

### Added — server
- **Calendar polish** (`bridge.py`) — `Event` TypedDict + `by_person` cache, person-tag regex, `summarize_for_prompt()` single privacy chokepoint stripping ISO timestamps + emails before any prompt injection. New `GET /api/calendar/today` endpoint. Background poll loop with exponential backoff. Nightly-flush evicts stale events on date roll-over.
- **Voice catalog + installer** (`docs/voice-catalog.md`, `scripts/voice-install.sh`) — 12 Piper + 6 EdgeTTS voices curated. `make voice-install VOICE=<key>` and `make voice-list`.
- **Observability** (`bridge/metrics.py`, `monitoring/grafana-dashboard.json`, `docs/observability.md`) — Prometheus `/metrics` with 9 metrics (first-audio latency histogram, request duration/errors per endpoint, ACP session gauge, smart-mode/kid-mode counters, perception event counter, calendar fetch failures). Two-layer defensive guard so metrics regression cannot break request path.
- **Layer 6 ProactiveGreeter** (`bridge/proactive_greeter.py`, `bridge/server_push.py`, `docs/proactive-greetings.md`) — face_recognized → cooldown + time-of-day windowing + kid-safe sandwich + calendar-aware greeting via inject-tts. Template fallback. 14 unit tests.
- **Phase 2 AudioSceneClassifier** (`bridge/audio_scene.py`, `bridge/yamnet_classmap.py`, `scripts/fetch-yamnet.sh`, `docs/audio-scene-classifier.md`) — YAMNet INT8 TFLite scaffold. Sliding 0.96s buffer @ 16kHz, whitelist filter (doorbell/knock/cat/dog/baby-cry/music/footsteps/etc.), threshold + per-class cooldown. Emits `sound_event(class=...)`. Defensive — degrades gracefully without `tflite-runtime`. 10 unit tests.
- **Hybrid smart-mode LED bridge half** (`receiveAudioHandle.py`) — `_send_led_multi` helper + `conn.smart_mode_active` flag. Holds index 0 purple while the rest of the ring shows listen/think/talk. Re-asserts on every color change. try/except guarded for old-firmware compatibility.
- **Face greeter env-tunable** — `FACE_GREET_TEXT` (set "" to disable verbal greet) + `FACE_GREET_MIN_INTERVAL_SEC` (default 30s).
- **Purr-on-head-pet (server)** (`bridge.py`, `bridge/assets/`) — `_perception_purr_player` consumes `head_pet_started`, pushes purr audio via inject-text. Per-device cooldown. Bypasses kid-mode sandwich (fixed asset). Asset path is a drop-in (not committed; see `bridge/assets/README.md`).
- **Clap-to-wake** (`bridge.py`, `docs/voice-mode-entry.md`) — `_perception_clap_waker` for dark rooms. `CLAP_WAKE_ENABLED=false` default. Doc compares all 6 voice-mode entry paths (wake word, face, head-pet hold, clap, touchscreen, /admin/inject-text).
- **Server-side Layer 4 face recognition** (`bridge/face_db.py`, `bridge/face_recognizer.py`) — Option B fallback to the on-device path.
- **Household roster** (`bridge/household.py`, `household.example.yaml`) — family roster with per-person config.
- **Speaker voiceprint** (`bridge/speaker.py`) — voiceprint speaker identification module.
- **Wake-word options doc** (`docs/wake-word.md`) — current architecture, 21 prebuilt English wake words, three paths to "Hey Dotty" (Path A interim shipped, Path B microWakeWord roadmap, Path C wakenet9 custom). Sample collection guide.
- **SBOM scaffold** (`scripts/generate-sbom.sh`, `docs/sbom.md`) — CycloneDX-ish component+license inventory. `make sbom`.
- **Signed releases scaffold** (`docs/signed-releases.md`, `KEYS.txt`) — GPG signing walkthrough + CI integration snippet (commented-out signing step ready to enable).
- **Versioned docs via mike** (`mkdocs.yml`, `.github/workflows/docs-deploy.yml`, `docs/requirements.txt`, `docs/versioning.md`) — `/latest/`, `/v0.1/`, `/dev/` URL structure.

### Added — firmware (StackChan/dotty fork)
- **Layer 1 privacy LEDs scaffold** — `PrivacyLeds` singleton drives right-ring index 6 (mic) + index 7 (camera). RAII `MicPeripheralGuard` + `CameraPeripheralGuard` tie LED state to peripheral enable codepath. New `self.robot.get_privacy_state` MCP tool. `set_led_multi` rejects indices 6/7.
- **Layer 4 face recognition scaffold** — `FaceRecognizer` (NVS-backed, max 10 enrolled, embedding stub until ESP-DL `face_recognition.so` is wired). `ParentalGate` (PIN + long-press, single-shot 30s token). 4 MCP tools: `face_unlock`, `face_enroll`, `face_forget`, `face_list`. New `face_recognized` perception event.
- **Hybrid smart-mode LED firmware half** — `NeonLight::setColorAt` public + `self.robot.set_led_multi` MCP tool.
- **Head-pet hold-to-listen wake** — touch ≥2s → `WakeWordInvoke("head_pet_hold")` opens listen window. Works in the dark. Also emits `head_pet_started` / `head_pet_ended` perception events for the purr consumer.
- **Wake-word default switched** — `sdkconfig.defaults`: Chinese "Hi, Stack Chan" → English "Hi, ESP". Interim while custom "Hey Dotty" microWakeWord is being trained. `microwakeword_setup.md` documents long-term plan.

### Changed — firmware
- **Face tracking smoother + faster** — EMA alpha 0.3→0.5, `lookAtNormalized` speed 350→500, 6% bbox-center deadband. MSR threshold 0.25→0.40 cuts stage-2 work for marginal candidates. All knobs `constexpr` for one-line revert.

### Fixed — firmware
- **Camera arbiter TOCTOU race** — fold flag check inside mutex region, eliminating 2s stall window.
- **Stale `idle_motion_modifier_id_` in `FaceTrackingModifier`** — lookup by stable name at call time instead of caching ID at construction. Added `Modifier::name()` virtual + `StackChan::getModifierByName()` API.

### Removed — server
- **Rich MCP tool surface** (`bridge/rich_mcp.py`, `bridge/rich_mcp_dispatch.py`, `docs/rich-mcp.md`, 13 tests). Never enabled in production (`DOTTY_RICH_MCP=false` default). Cut as dormant scaffolding — voice-only is the intended product surface; don't re-add.
- **Phase 4 EngagementDecider** (`bridge/engagement_decider.py`, `bridge/intent_templates.py`, `docs/engagement-decider.md`, 32 tests). Never enabled in production (`ENGAGEMENT_ENABLED=false` default). Cut for the same reason. Proactive utterances remain served by `bridge/proactive_greeter.py`.
- `docs/mcp-tools-capture.json` trimmed 17 → 13 tools — the 4 `robot.face_*` entries were rich_mcp fabrications (firmware actually exposes `camera.face_*` and has no `face_unlock` tool at all). `set_led_multi` and `get_privacy_state` retained as real firmware tools.

### Pending wiring (not yet shipped)
- Camera `VIDIOC_STREAMOFF` peripheral-off when face-detect is paused (closes the Layer 1 privacy LED hole noted in `eb595f2`).
- Reproducible firmware builds — IDF Dockerfile SHA256 pin + `dependencies.lock` + `make verify-firmware` target.

## [0.1.0] - 2026-04-25

First tagged release — early-feedback alpha. Works end-to-end on the maintainer's hardware (M5Stack StackChan + Unraid Docker host + Raspberry Pi bridge + ZeroClaw + OpenRouter Mistral Small 3.2). External users welcome; see `ROADMAP.md` for known issues.

### Fixed in v0.1.0
- **Smart Mode marker check.** `zeroclaw.py` `_payload` was matching `[SMART_MODE]\n` against the composed `[Context] … [User] …` payload (marker landed at offset ~2700, so `startswith` was always False). Every voice "smart mode" turn since `434988d` silently fell back to the default voice model. Fix detects markers on the raw user message before `_compose()` wraps it.

### Changed
- **Default LLM switched** from `qwen/qwen3-30b-a3b-instruct-2507` to `mistralai/mistral-small-3.2-24b-instruct` (2.6× speedup, p50 1.9 s vs 5 s, no quality regression on smoke battery).
- **Rebranded to Dotty.** Project identity renamed from `stackchan-infra` to Dotty (`dotty-stackchan`). Default robot name is "Dotty" (customizable via `make setup`). Channel identifier `stackchan` → `dotty` (both accepted during transition). Python constants `STACKCHAN_TURN_*` → `VOICE_TURN_*`. All docs, config, and build files updated.
- **3-sentence response limit** enforced in both `/api/message` and `/api/message/stream` endpoints. `MAX_SENTENCES` env var (default 3).
- **Streaming `final` line** now always includes emoji prefix correction.

### Added
- **ASR noise filter** — `_is_noise()` rejects punctuation-only or very short ASR results before they trigger a thinking animation or LLM call. Configurable via `MIN_UTTERANCE_CHARS`.
- **ASR name correction** — `_apply_asr_corrections()` fixes common SenseVoice misrecognitions of the robot name.
- **Content-filter test probes** — 10 new adversarial prompts targeting the `_BLOCKED_WORDS_RE` regex filter.
- **Custom LLM provider (ZeroClawLLM)** — `zeroclaw.py` proxies xiaozhi-esp32-server LLM calls to the ZeroClaw agent on the RPi via the FastAPI bridge.
- **FastAPI bridge (`bridge.py`)** — HTTP-to-ACP translator on the RPi; speaks JSON-RPC 2.0 over stdio to a long-running `zeroclaw acp` child process.
- **ACP session caching** — reuses a single ZeroClaw session across turns instead of creating/destroying one per request; rotates on idle timeout, turn count, or wall-clock age. Shaves ~1-2 s off first-audio latency.
- **NDJSON streaming endpoint** — `/api/message/stream` streams tokens as newline-delimited JSON so TTS can start on the first sentence while the LLM is still generating.
- **Streaming EdgeTTS provider (`edge_stream.py`)** — custom xiaozhi-server TTS provider using Microsoft Edge Neural voices with streaming audio delivery.
- **Local Piper TTS provider (`piper_local.py`)** — offline-first TTS alternative using `piper-tts` (`en_GB-cori-medium`); drop-in replacement for EdgeTTS with no cloud dependency.
- **FunASR English language pin (`fun_local.py`)** — patched ASR provider adds a `language` config key so SenseVoiceSmall can be pinned to English, preventing mis-detection of short utterances as Korean/Japanese.
- **Emoji emotion protocol** — three-layer enforcement (ZeroClaw agent prompt, xiaozhi system prompt, `_ensure_emoji_prefix` fallback in `bridge.py`) ensures every LLM response starts with an emoji that the firmware parses into a face animation.
- **Thinking emotion frame** — emits `{"type":"llm","emotion":"thinking"}` to the device between ASR completion and the LLM call so the avatar shows a thinking face during the wait.
- **Child-safety enforcement sandwich** — five numbered rules in `VOICE_TURN_SUFFIX` (audience framing for ages 4-8, forbidden-topic list, roleplay-lock, profanity-lock, ambiguity tie-breaker) injected at max-attention position for Qwen3 compliance. Tier 1 of a pre-designed four-tier lockdown plan.
- **Self-harm routing rule** — dedicated rule routes self-harm disclosures to a trusted adult instead of a generic cheerful redirect.
- **Technical documentation suite (`docs/`)** — eight linked markdown files covering architecture, hardware, voice pipeline, brain, protocols, latent capabilities, and upstream references.
- **Docker packaging for zeroclaw-bridge** — multi-stage Dockerfile (Rust builder to python:3.12-slim runtime), deploy-side compose file, and GitHub Actions workflow publishing multi-arch images (amd64 + arm64) to `ghcr.io/brettkinny/zeroclaw-bridge`.
- **Dual deployment paths** — both bare-metal systemd and Docker deployment for the bridge, sharing the same `~/.zeroclaw/` state directory.
- **Placeholder-based configuration** — all real IPs, usernames, and paths replaced with named placeholders (`<UNRAID_IP>`, `<RPI_IP>`, `<ROBOT_NAME>`, etc.) for safe public sharing.
- **systemd unit (`zeroclaw-bridge.service`)** — bare-metal bridge deployment with `Restart=on-failure`.
- **docker-compose.yml** — container definition for xiaozhi-esp32-server with volume mounts for all custom providers.

### Changed
- **Depersonalized repo** — renamed from "Dotty" to a generic StackChan stack; persona name is now user-configurable via `<ROBOT_NAME>` placeholder.
- **Default LLM endpoint switched to streaming** — `.config.yaml` now points `ZeroClawLLM.url` at `/api/message/stream` by default; the buffered `/api/message` endpoint remains available for backward compatibility and smoke tests.
- **TTS mounts switched to flat-file format** — directory-form mounts silently fell through to "unsupported TTS type" errors; now matches the working `fun_local.py` ASR pattern.

### Fixed
- **Abort race condition** — kill and respawn ACP child on barge-in to prevent stale chunk contamination.
- **FunASR language mis-detection** — upstream hardcodes `language="auto"`, causing SenseVoiceSmall to classify short/unclear English audio as Korean or Japanese. Config-driven language override resolves this.
- **Child-safety self-harm response** — LLM was redirecting to blanket-fort building instead of naming a trusted adult; dedicated rule fixed the last failing red-team case (10/10 pass rate).
- **TTS provider loading failure** — directory-form Docker mounts caused silent fallthrough; flat-file mounts fixed "unsupported TTS type" errors at connect time.
