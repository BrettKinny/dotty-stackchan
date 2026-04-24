---
title: FAQ
description: Frequently asked questions about hardware, setup, and configuration.
---

# FAQ

---

### What hardware do I need?

The verified setup is an **M5Stack CoreS3** mounted in the **M5Stack StackChan servo kit** (2x SG90 servos for pan/tilt, 12 RGB LEDs, 3D-printed chassis). You also need a Docker-capable host on your LAN (Unraid, a spare PC, any Linux box with Docker) and a Raspberry Pi for the agent brain.

See [hardware-support.md](./hardware-support.md) for the full spec table and support tiers.

---

### Can I use a different LLM?

Yes. The LLM is pluggable at two levels:

1. **At the ZeroClaw level** (the brain): change the `default_model` and provider in ZeroClaw's `config.toml` on the RPi. ZeroClaw supports OpenAI-compatible APIs, Anthropic, Gemini, Ollama, and 17+ other backends. Restart the bridge after changing.
2. **At the xiaozhi-server level**: if you want to bypass ZeroClaw entirely, swap the `selected_module` for LLM in `data/.config.yaml` to any of the built-in providers (OpenAI-compatible, Ollama, Dify, FastGPT, etc.). You lose ZeroClaw's agent features (memory, tools, persona files) but gain a simpler stack.

The reference config uses Qwen3-30B-A3B via OpenRouter. Any model that handles English well and can follow emoji-prefix instructions will work. Larger models give better persona adherence; smaller models respond faster.

---

### Is it fully offline?

Almost, and it can be with two swaps:

- **ASR** (speech recognition): already fully local. FunASR runs on your Docker host.
- **TTS** (speech synthesis): local if you use Piper TTS. EdgeTTS requires internet (it hits Microsoft's servers).
- **LLM**: cloud by default (OpenRouter). Swap in Ollama pointing at a local model for fully offline inference.

With Piper TTS and a local Ollama model, nothing leaves your LAN. The trade-off is that local LLMs need a GPU or beefy CPU to run at conversational speed.

---

### How much does it cost to run?

**Hardware (one-time):**
- M5Stack StackChan kit: check current pricing on the [M5Stack store](https://shop.m5stack.com/). Expect roughly $60-80 USD for the CoreS3 + servo kit.
- Docker host: whatever you already have. Any machine that can run Docker and has a few GB of RAM.
- Raspberry Pi: any Pi 3 or later. The ZeroClaw binary is ~9 MB and uses <5 MB RAM.

**Recurring:**
- Electricity for the hosts (negligible for most home setups).
- LLM API costs if using a cloud provider. OpenRouter pricing for Qwen3-30B-A3B is on the order of $0.10-0.30 per million tokens — casual household use is pennies per day. Check [OpenRouter's pricing page](https://openrouter.ai/qwen/qwen3-30b-a3b-instruct-2507) for current rates.
- $0 if you run a local model via Ollama.

---

### Is it safe for kids?

The architecture supports child-safety guardrails, but this is a self-hosted system — you are responsible for the configuration.

What the stack does:
- Per-turn sandwich enforcement in the bridge forces the LLM to respond in English with an emoji prefix, which limits the scope of unexpected output.
- The ZeroClaw persona files (`SOUL.md`, `IDENTITY.md`) define the robot's personality and boundaries.
- The design supports a dual kid/adult mode (kid-safe by default, adult mode toggled explicitly).

What the stack does **not** do:
- Content-filter the LLM's output at a network level. If the LLM says something inappropriate, the bridge passes it through.
- Prevent a determined child from asking adversarial questions.
- Guarantee the LLM won't hallucinate inappropriate content (no model can).

The safety posture is: you control the prompt, you control the model, you can read every log. That's more control than any cloud voice assistant gives you, but it's not a substitute for parental judgment.

---

### Can I change the robot's personality?

Yes. The persona is defined in Markdown files on the RPi:

- `~/.zeroclaw/workspace/SOUL.md` — core identity and values.
- `~/.zeroclaw/workspace/IDENTITY.md` — name, backstory, role.

These are hot-read by ZeroClaw — edit them and the next conversation turn picks up the changes, no restart needed.

There's also a secondary `prompt:` key in `data/.config.yaml` on the Unraid host that gets injected as a system message. ZeroClaw's own persona files take precedence, but this is a useful place for voice-pipeline-level hints.

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
- [README.md](../README.md) — deployment guide.

Last verified: 2026-04-24.
