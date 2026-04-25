# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This project does not yet follow [Semantic Versioning](https://semver.org/) —
there are no tagged releases. The history below is a retrospective changelog
built from the commit log.

## [Unreleased]

### Added
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
- **FunASR language mis-detection** — upstream hardcodes `language="auto"`, causing SenseVoiceSmall to classify short/unclear English audio as Korean or Japanese. Config-driven language override resolves this.
- **Child-safety self-harm response** — LLM was redirecting to blanket-fort building instead of naming a trusted adult; dedicated rule fixed the last failing red-team case (10/10 pass rate).
- **TTS provider loading failure** — directory-form Docker mounts caused silent fallthrough; flat-file mounts fixed "unsupported TTS type" errors at connect time.
