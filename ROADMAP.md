# Roadmap

Personal hobby project. Might ship, might not — no timeline, no guarantees. If you want something built, feel free to contribute or ask nicely.

## What works today

The core voice loop runs end-to-end on a single Docker host:

- Voice I/O through xiaozhi-esp32-server (ASR + TTS)
- Brain via the `dotty-pi` container (pi coding agent with voice tools)
- Perception events (face detection, sound events) through `dotty-behaviour`
- Admin dashboard via `bridge.py`
- Kid Mode, on by default — prompt-level steering for age-appropriate responses, **not** an output content filter, and no substitute for supervision
- Local ASR (SenseVoiceSmall) and local TTS (Piper) — no cloud dependency
- Streaming responses, emoji expressions, swappable personas

Rough edges: face emoji rendering misses 4 of 9 emotions visually; the sound-direction localizer has a left-bias on CoreS3 hardware.

## Maybe someday

No promises — just a few ideas I might get around to, roughly in order of interest:

- Better wake word ("Hey Dotty" instead of the current "Hi, ESP")
- Story Mode improvements (longer narratives, character voices)
- Improve Security Mode

I'm not planning to build variant-board ports, firmware compatibility matrices, signed/reproducible builds, observability dashboards, or OTA provisioning flows myself. If any of those matter to you, PRs are welcome.
