# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). From v0.1.0 forward this project follows [Semantic Versioning](https://semver.org/). Server and firmware tag independently as `server-vX.Y.Z` and `fw-vX.Y.Z`; see `COMPATIBILITY.md` for the matrix.

## [Unreleased]

Post-v0.1 work — code shipped to `main` but not yet deployed live or tagged.

### Added — server (2026-04-26 → 2026-05-15)
- **Two-tier voice path: `Tier1Slim` LLM provider** (`b73f583`, `custom-providers/tier1_slim/tier1_slim.py`) — slim inner-loop LLM in xiaozhi-server that runs a small/fast model (default `qwen3.5:4b` against llama-swap) for chitchat and escalates tool calls to the bridge via `POST /api/voice/escalate`. Tools: `memory_lookup`, `think_hard`, `take_photo`, `play_song`. Cuts plain-chat latency well below 1 s; reserves the heavy ZeroClaw / cloud path for tools that genuinely need it. `set_runtime()` allows hot-swapping model/url/api_key in flight (no daemon restart) — used by smart-mode flips.
- **xiaozhi-server admin endpoints** (`custom-providers/xiaozhi-patches/http_server.py`) — `/xiaozhi/admin/play-asset`, `/xiaozhi/admin/songs`, `/xiaozhi/admin/set-tier1slim-model` (hot-swap the running Tier1Slim provider; bridge calls this on smart-mode flip when `DOTTY_VOICE_PROVIDER=tier1slim`). `shared_llm` singleton in `portal_bridge.py` exposes the live provider to the admin routes.
- **Help-intent handler** (`1ccfdd6`, xiaozhi) — voice "what can you do?" yields a curated capability summary instead of the model freelancing.
- **Persona library collapsed to default + smart** (`3a055a6`) — three earlier persona files reduced to two; dashboard simplified accordingly.
- **TTL-bound face identification** (`5a3cab7`, `bridge.py`) — bridge owns identified-face TTL with refresh loop; firmware face-pip flickers if TTL expires without refresh, ensuring stale identification doesn't pin the green pip indefinitely.
- **Vision capture modal** (`6c8fb45`, dashboard) — full-size vision capture in dashboard with download.
- **Sleep banner moved to Perception card** (`74f8dc9`, dashboard).
- **Dashboard state-card polling + kid_mode hot-load cleanup** (`089c575`).
- **Dashboard auto-refresh stabilised** (`ae54e93`).

### Changed — server (2026-04-26 → 2026-05-15)
- **`DOTTY_VOICE_PROVIDER` hot-swap landed** (`e2930ce`, `bridge.py`) — smart-mode flips now pick their dispatch path based on the env var. `=tier1slim` → in-process hot-swap via `/xiaozhi/admin/set-tier1slim-model` (no docker restart, no daemon restart, instant). `=zeroclaw` (default) → legacy `~/.zeroclaw/config.toml` rewrite + `systemctl restart zeroclaw-bridge`. Same commit retargeted `think_hard` to `qwen3.6:27b-think` on llama-swap.
- **`/xiaozhi/admin/set-tier1slim-model` endpoint** (`b83898e`, `custom-providers/xiaozhi-patches/http_server.py`) — the receiving side of the hot-swap. Mutates the live Tier1Slim provider's `model` / `url` / `api_key` via `set_runtime()`. Refuses to blank a non-empty `api_key` so a half-configured OFF→ON flip fails fast instead of 401-looping.
- **VLM fallback hardened** (`aa2d8ba`, `bridge.py`) — missing VLM API key now surfaces a clear no-vision message instead of letting the model confabulate a description with no image input.
- **llama-swap concurrent-models recipe shipped** (`968949a`, `docs/cookbook/llama-swap-concurrent-models.md`) — documented `voice` matrix set (`qwen3.5:4b` + `qwen3.6:27b-think` co-resident) and `coding` matrix set (`qwen3.6:27b` solo) for the `pi` CLI. Avoids evicting the voice pair on coding sessions; cold-reload cost paid on next voice turn after a `pi` run.
- **Voice local backend migrated Ollama → llama.cpp / llama-swap** (`34552e3`, `zeroclaw-bridge.service`) — `VOICE_LOCAL_PROFILE_KEY` bumped from `:11434` → `:8080`. 2.15× generation speedup (8 → 18 tok/s on dual RTX 3060), eliminates 2.7 GB of CPU offload, fits the model fully on GPU. Cold load ~20 s (was 70 s).
- **Bridge `VOICE_THINKER_TIMEOUT=90`** added to systemd unit template (`452bbd7`) — keeps long `think_hard` escalations from being killed by the default request timeout.
- **Top-level reboot button removed from dashboard header** (`3198b8e`) — too easy to misclick; functionality remains accessible via Admin card.

### Added — firmware (StackChan/dotty fork, 2026-04-26 → 2026-05-15)
- **Privacy sleep extended to camera + mic** (`ac51662`, `1754499`) — entering `sleep` state now disables camera and routes mic-off through the xiaozhi privacy gate, not just the LED indicator.
- **Listening LED edge cleared on enter-sleep** (`deca11e`).
- **Sleep torque release with timeout fallback** (`cd23282`) — preferred path is settle-based release in `StateManager::_update`; 3 s timeout fallback when settle never fires. (Known issue: still being torqued in some cases — see `dotty-private/tasks.md`.)
- **Face-identified flicker grace + perception event emit** (`613a0ca`) — adds `kFaceIdentifiedFlickerGraceMs = 1500` to ride out brief detection hiccups; emits perception event so the bridge mirror updates.
- **AXP2101 PEK IRQ register addresses corrected** (`5ea12e0`) — long-press / power-button events now register at `0x41/0x49`, not `0x42/0x4A`. Previous addresses worked in many cases but missed the canonical IRQ status bits.
- **LEDs cleared before AXP self-off on long-press** (`0736a1e`) — clean visual shutdown.
- **`face_tracking` WakeWordInvoke gated on `GetDeviceState()`** (`1775759`) — kills the double-wake on flickering walk-in (face_tracking was firing WakeWordInvoke even when device was already in `LISTENING`).
- **kid_mode pip retuned salmon (220, 80, 80)** (`dcad76f`) — earlier hue (168, 80, 100) had B > G after RGB565 quantization, reading as cool purple/magenta. New hue keeps G == B; renders warm.
- **V4L2 ioctl EINVAL fixed** (`37e92d6`) — restored Linux `_IOR` encoding after lwip clobbered it. Camera streams cleanly again.
- **HEADMOVE writer instrumentation** (`1e30a05`) — every head-write site (idle_motion, mcp_set_head_angles, keyframe_servo, head_pet, state_manager) now tags its writes for trace logging. Diagnostic-only.

### Pending wiring (work-in-progress)
- **Firmware StateManager (Phase 4)** — `firmware/main/stackchan/modes/state_manager.{h,cpp}` plus MCP handlers (`self.robot.set_state`, `self.robot.set_toggle`). Bridge-side wiring exists (perception bus, dashboard mirror, `/xiaozhi/admin/set-state` dispatch); firmware side not yet built. The dispatch reaches the device WS today but no MCP handler consumes it. Honest status tracked in `docs/modes.md`.

### Removed — server (2026-04-25 sprint)
- **dlib biometric face recognition** — `bridge/face_db.py`, `bridge/face_recognizer.py`, the `face-recognition` requirement, the `/api/face/{enroll,recognize,forget,list,last-action}` endpoints, the per-channel `_voice_identity_pending` / `_identity_state` machinery, and the voice-driven enrollment / list / forget intents in `receiveAudioHandle.py`. The description-based identity path (Layer 4 v1.5 — VLM returns a description plus a roster name match against `household.yaml`'s `appearance:` field) is now the sole identity feed. The biometric path was opt-in v2 only, never reached production (dlib won't build on Python 3.13 / DietPi), and conflicted with the project's no-storage identity posture. Firmware-side `FaceRecognizer` + `ParentalGate` + the inert call at `face_detector.cpp:273` will be removed in a follow-up firmware-only PR.
- **Blind mode v1** — time-based civil-dusk-to-dawn gating (`_is_blind`, `_civil_twilight_bounds`, `_blind_mode_gauge_refresher`, `dotty_blind_mode_active` Prometheus gauge, `DOTTY_BLIND_*` env vars) removed in favour of a simple time-window guard on `_perception_face_greeter` (`FACE_GREET_HOUR_START` / `FACE_GREET_HOUR_END`, default 06–21). The walk-in soak revealed that the "too dark to see" reply was wrong indoors at night with lights on (modern VLMs handle indoor low light fine), and blocked legitimate vision use after dusk. Killing 3 AM "Hi!" greets is the only gate worth keeping; replaced with a 5-line hour check.
- **Phase 2 audio scene classifier (YAMNet)** — `bridge/audio_scene.py`, `bridge/yamnet_classmap.py`, `tests/test_audio_scene.py`, `scripts/fetch-yamnet.sh`, `docs/audio-scene-classifier.md`, the `_audio_scene_*` globals + thread-bridge helper in `bridge.py`, the `/api/audio-scene/feed` HTTP endpoint, lifespan startup/shutdown hooks, and the `# tflite-runtime>=2.13` optional dep comment. ~1058 LOC + 10 tests + 200-line docs page. Default-OFF scaffold (`AUDIO_SCENE_ENABLED=false`) shipped 2026-04-26 then sat dormant — `tflite-runtime` was never installed on the ZeroClaw host, no xiaozhi-side forwarder ever materialised, and no production traffic touched the endpoint. Same speculative-scaffold pattern as the rich_mcp / engagement_decider rips. Hybrid smart-mode LED firmware-side (`set_led_multi` MCP tool, `NeonLight::setColorAt`) and bridge-side consumer (`_send_led_multi`, `conn.smart_mode_active`) survive — independently useful for smart-mode and unrelated to the classifier. The dependent "Dance when music is detected" task entry was removed at the same time. If audio-scene classification ever becomes a real product need, start from current state, not this scaffold.

### Changed — server (2026-04-25 sprint)
- **Length-aware brevity** — voice replies default to 1-2 short sentences (was 1-3), but the model is now invited to take a fuller swing on open-ended asks ("tell me a story", "explain why X", "list some Y") up to 6 sentences. Enforced via `_BASE_SUFFIX` rule 3 in `custom-providers/textUtils.py`, the `VOICE_TURN_SUFFIX_SHORT` reminders in `bridge.py`, and a `MAX_SENTENCES` default bump from 3 to 6 (still env-overridable). `personas/{default,assistant,playful}.md` + `.config.yaml` template + `docs/kid-mode.md` + `docs/cookbook/disable-kid-mode.md` all updated to the new wording. Cheapest possible "model-from-context" change — no classifier, no trigger phrases, no server-side routing. Smart-mode bypass unchanged (Sonnet still answers at full length when invoked).

### Added — server (2026-04-25 sprint)
- **Calendar polish** (`bridge.py`) — `Event` TypedDict + `by_person` cache, person-tag regex, `summarize_for_prompt()` single privacy chokepoint stripping ISO timestamps + emails before any prompt injection. New `GET /api/calendar/today` endpoint. Background poll loop with exponential backoff. Nightly-flush evicts stale events on date roll-over.
- **Voice catalog + installer** (`docs/voice-catalog.md`, `scripts/voice-install.sh`) — 12 Piper + 6 EdgeTTS voices curated. `make voice-install VOICE=<key>` and `make voice-list`.
- **Observability** (`bridge/metrics.py`, `monitoring/grafana-dashboard.json`, `docs/observability.md`) — Prometheus `/metrics` with 9 metrics (first-audio latency histogram, request duration/errors per endpoint, ACP session gauge, smart-mode/kid-mode counters, perception event counter, calendar fetch failures). Two-layer defensive guard so metrics regression cannot break request path.
- **Layer 6 ProactiveGreeter** (`bridge/proactive_greeter.py`, `bridge/server_push.py`, `docs/proactive-greetings.md`) — face_recognized → cooldown + time-of-day windowing + kid-safe sandwich + calendar-aware greeting via inject-tts. Template fallback. 14 unit tests.
- **Hybrid smart-mode LED bridge half** (`receiveAudioHandle.py`) — `_send_led_multi` helper + `conn.smart_mode_active` flag. Holds index 0 purple while the rest of the ring shows listen/think/talk. Re-asserts on every color change. try/except guarded for old-firmware compatibility.
- **Face greeter env-tunable** — `FACE_GREET_TEXT` (set "" to disable verbal greet) + `FACE_GREET_MIN_INTERVAL_SEC` (default 30s).
- **Purr-on-head-pet (server)** (`bridge.py`, `bridge/assets/`) — `_perception_purr_player` consumes `head_pet_started`, pushes purr audio via inject-text. Per-device cooldown. Bypasses kid-mode sandwich (fixed asset). Asset path is a drop-in (not committed; see `bridge/assets/README.md`).
- **Server-side Layer 4 face recognition** (`bridge/face_db.py`, `bridge/face_recognizer.py`) — Option B fallback to the on-device path.
- **Household roster** (`bridge/household.py`, `household.example.yaml`) — family roster with per-person config.
- **Speaker voiceprint** (`bridge/speaker.py`) — voiceprint speaker identification module.
- **Wake-word options doc** (`docs/wake-word.md`) — current architecture, 21 prebuilt English wake words, three paths to "Hey Dotty" (Path A interim shipped, Path B microWakeWord roadmap, Path C wakenet9 custom). Sample collection guide.
- **SBOM scaffold** (`scripts/generate-sbom.sh`, `docs/sbom.md`) — CycloneDX-ish component+license inventory. `make sbom`.
- **Signed releases scaffold** (`docs/signed-releases.md`, `KEYS.txt`) — GPG signing walkthrough + CI integration snippet (commented-out signing step ready to enable).
- **Versioned docs via mike** (`mkdocs.yml`, `.github/workflows/docs-deploy.yml`, `docs/requirements.txt`, `docs/versioning.md`) — `/latest/`, `/v0.1/`, `/dev/` URL structure.

### Added — firmware (StackChan/dotty fork, 2026-04-25 sprint)
- **Layer 1 privacy LEDs scaffold** — `PrivacyLeds` singleton drives right-ring index 6 (mic) + index 7 (camera). RAII `MicPeripheralGuard` + `CameraPeripheralGuard` tie LED state to peripheral enable codepath. New `self.robot.get_privacy_state` MCP tool. `set_led_multi` rejects indices 6/7.
- **Layer 4 face recognition scaffold** — `FaceRecognizer` (NVS-backed, max 10 enrolled, embedding stub until ESP-DL `face_recognition.so` is wired). `ParentalGate` (PIN + long-press, single-shot 30s token). 4 MCP tools: `face_unlock`, `face_enroll`, `face_forget`, `face_list`. New `face_recognized` perception event.
- **Hybrid smart-mode LED firmware half** — `NeonLight::setColorAt` public + `self.robot.set_led_multi` MCP tool.
- **Head-pet hold-to-listen wake** — touch ≥2s → `WakeWordInvoke("head_pet_hold")` opens listen window. Works in the dark. Also emits `head_pet_started` / `head_pet_ended` perception events for the purr consumer.
- **Wake-word default switched** — `sdkconfig.defaults`: Chinese "Hi, Stack Chan" → English "Hi, ESP". Interim while custom "Hey Dotty" microWakeWord is being trained. `microwakeword_setup.md` documents long-term plan.

### Changed — firmware (2026-04-25 sprint)
- **Face tracking smoother + faster** — EMA alpha 0.3→0.5, `lookAtNormalized` speed 350→500, 6% bbox-center deadband. MSR threshold 0.25→0.40 cuts stage-2 work for marginal candidates. All knobs `constexpr` for one-line revert.

### Fixed — firmware (2026-04-25 sprint)
- **Camera arbiter TOCTOU race** — fold flag check inside mutex region, eliminating 2s stall window.
- **Stale `idle_motion_modifier_id_` in `FaceTrackingModifier`** — lookup by stable name at call time instead of caching ID at construction. Added `Modifier::name()` virtual + `StackChan::getModifierByName()` API.

### Removed — server (2026-04-25 sprint, second pass)
- **Rich MCP tool surface** (`bridge/rich_mcp.py`, `bridge/rich_mcp_dispatch.py`, `docs/rich-mcp.md`, 13 tests). Never enabled in production (`DOTTY_RICH_MCP=false` default). Cut as dormant scaffolding — voice-only is the intended product surface; don't re-add.
- **Phase 4 EngagementDecider** (`bridge/engagement_decider.py`, `bridge/intent_templates.py`, `docs/engagement-decider.md`, 32 tests). Never enabled in production (`ENGAGEMENT_ENABLED=false` default). Cut for the same reason. Proactive utterances remain served by `bridge/proactive_greeter.py`.
- `docs/mcp-tools-capture.json` trimmed 17 → 13 tools — the 4 `robot.face_*` entries were rich_mcp fabrications (firmware actually exposes `camera.face_*` and has no `face_unlock` tool at all). `set_led_multi` and `get_privacy_state` retained as real firmware tools.

### Pending wiring (2026-04-25 sprint, not yet shipped)
- Camera `VIDIOC_STREAMOFF` peripheral-off when face-detect is paused (closes the Layer 1 privacy LED hole noted in `eb595f2`). **Status 2026-05-15: superseded by `ac51662` privacy-sleep camera disable** — the broader privacy posture now covers this hole at sleep entry, though pause-aware streamoff is still a finer-grained want.
- Reproducible firmware builds — IDF Dockerfile SHA256 pin + `dependencies.lock` + `make verify-firmware` target.

## [0.1.0] - 2026-04-25

First tagged release — early-feedback alpha. Works end-to-end on the maintainer's hardware (M5Stack StackChan + Docker host + ZeroClaw host + ZeroClaw + OpenRouter Mistral Small 3.2). External users welcome; see `ROADMAP.md` for known issues.

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
- **Custom LLM provider (ZeroClawLLM)** — `zeroclaw.py` proxies xiaozhi-esp32-server LLM calls to the ZeroClaw agent on the ZeroClaw host via the FastAPI bridge.
- **FastAPI bridge (`bridge.py`)** — HTTP-to-ACP translator on the ZeroClaw host; speaks JSON-RPC 2.0 over stdio to a long-running `zeroclaw acp` child process.
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
- **Placeholder-based configuration** — all real IPs, usernames, and paths replaced with named placeholders (`<XIAOZHI_HOST>`, `<ZEROCLAW_HOST>`, `<ROBOT_NAME>`, etc.) for safe public sharing.
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
