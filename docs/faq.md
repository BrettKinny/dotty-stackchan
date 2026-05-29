---
title: FAQ
description: Frequently asked questions about hardware, setup, and configuration.
---

# FAQ

---

### What hardware do I need?

The verified setup is an **M5Stack CoreS3** mounted in the **M5Stack StackChan servo kit** (2x feedback servos — yaw continuous-rotation + SCS0009 pitch — for pan/tilt, 12 RGB LEDs, 3D-printed chassis). You also need a Docker-capable host on your LAN (a spare PC or any Linux box with Docker) to run the voice and brain containers.

See [hardware-support.md](./hardware-support.md) for the full spec table and support tiers.

---

### Can I use a different LLM?

Yes. The LLM is pluggable via `selected_module.LLM` in `data/.config.yaml`:

1. **`PiVoiceLLM` (the default)** routes voice turns to the `dotty-pi` container — the pi agent — which runs a local model on llama-swap. To change the model, see [dotty-pi/README.md](../dotty-pi/README.md) for the model-selection rules.
2. **`OpenAICompat`** points straight at OpenAI, OpenRouter, Ollama, or any OpenAI-compatible API.

See [llm-backends.md](./llm-backends.md) for the full comparison. Any model that handles English well and can follow emoji-prefix instructions will work. Larger models give better persona adherence; smaller models respond faster.

---

### Is it fully offline?

Almost, and it can be with two swaps:

- **ASR** (speech recognition): already fully local. FunASR runs on your server.
- **TTS** (speech synthesis): local if you use Piper TTS. EdgeTTS requires internet (it hits Microsoft's servers).
- **LLM**: local by default — the `dotty-pi` agent runs against a local llama-swap model. Cloud is only used if you switch to a cloud backend or turn on smart-mode.

With Piper TTS and the default local model, nothing leaves your LAN. The trade-off is that local LLMs need a GPU or beefy CPU to run at conversational speed.

---

### How much does it cost to run?

**Hardware (one-time):**
- M5Stack StackChan kit: check current pricing on the [M5Stack store](https://shop.m5stack.com/). Expect roughly $60-80 USD for the CoreS3 + servo kit.
- Docker host: whatever you already have. Any machine that can run Docker (and, for the local LLM, a GPU or beefy CPU).

**Recurring:**
- Electricity for the host (negligible for most home setups).
- LLM API costs **only** if you use a cloud backend or turn on smart-mode — the default local model is free beyond electricity. Cloud backends (OpenRouter, OpenAI, etc.) are pay-per-token.

---

### Is it safe for kids?

**Kid Mode is ON by default** (`DOTTY_KID_MODE=true`). It enforces child-safe guardrails. You can disable it with `DOTTY_KID_MODE=false` for general-purpose use.

What Kid Mode enforces:
- Per-turn sandwich enforcement forces the LLM to respond in English with an emoji prefix, which limits the scope of unexpected output.
- The persona prompt (`personas/dotty_voice.md`) defines the robot's personality and boundaries with kid-safe defaults.
- Content and tone are constrained to be age-appropriate.

What Kid Mode does **not** do:
- Content-filter the LLM's output at a network level. If the LLM says something inappropriate, the stack passes it through.
- Prevent a determined child from asking adversarial questions.
- Guarantee the LLM won't hallucinate inappropriate content (no model can).

This is a self-hosted system — you control the prompt, the model, and every log. That's more control than any cloud voice assistant gives you, but it's not a substitute for parental judgment.

---

### Can I change the robot's personality?

Yes. The persona is a Markdown file — `personas/dotty_voice.md`, loaded by the active LLM provider. Edit it and restart the relevant container.

There's also a secondary `prompt:` key in `data/.config.yaml` that gets injected as a system message — a useful place for voice-pipeline-level hints. Full instructions: [cookbook/change-persona.md](./cookbook/change-persona.md).

---

### What voices are available?

Depends on which TTS provider you use:

- **Piper TTS (local):** browse the [piper-voices catalog](https://huggingface.co/rhasspy/piper-voices) on HuggingFace. Dozens of languages and speakers. The reference config uses `en_GB-cori-medium`.
- **EdgeTTS (cloud):** any Microsoft Edge Neural voice. Hundreds of voices across 70+ languages. Set the voice ID in `data/.config.yaml` under `TTS.EdgeTTS.voice` (e.g., `en-US-AvaNeural`, `en-AU-WilliamNeural`, `ja-JP-NanamiNeural`). Restart the container after changing.

To switch between Piper and EdgeTTS, change the `selected_module` for TTS in `data/.config.yaml` and restart the container.

---

### Does it work with other StackChan variants?

It depends on what you mean by "StackChan variant":

- **M5Stack CoreS3 + StackChan servo kit** (the official M5Stack product): This is what we test on. It works.
- **Original Stack-chan by Shinya Ishikawa** (`meganetaaan/stack-chan`): That's a different firmware (TypeScript on Moddable SDK). The server-side infrastructure doesn't care what firmware the device runs as long as it speaks the Xiaozhi WebSocket protocol. But the original Stack-chan firmware doesn't speak Xiaozhi — so no, not without porting the firmware.
- **Other ESP32-S3 boards running `78/xiaozhi-esp32`**: The server side will work (same WebSocket protocol), but you won't get StackChan-specific features (servos, avatar, LEDs) without board-specific firmware work.

See [hardware-support.md](./hardware-support.md) for the full support matrix.

---

## See also

- [about.md](./about.md) — what the project is and who it's for.
- [hardware-support.md](./hardware-support.md) — hardware requirements and support tiers.
- [troubleshooting.md](./troubleshooting.md) — symptom-based debugging guide.
- [SETUP.md](SETUP.md) — deployment guide.

Last verified: 2026-05-22.
