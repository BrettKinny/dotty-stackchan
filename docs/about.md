---
title: About
description: What Dotty is and why it exists.
---

# About

## What is this?

A self-hosted voice stack for the M5Stack [StackChan](https://github.com/m5stack/StackChan) desktop robot. You talk to the robot, it talks back — speech recognition, language model, and text-to-speech all run on your own hardware (a Docker host and a Raspberry Pi on your LAN). The only thing that leaves your network by default is the LLM call, and that's pluggable too: swap in Ollama for a fully offline deployment with no code changes.

## Features

- **Local speech recognition.** FunASR SenseVoiceSmall runs on your Docker host. Audio never leaves your LAN.
- **Pluggable LLM.** The reference config uses Qwen3 via OpenRouter. Swap in any OpenAI-compatible API, Ollama for local inference, or bring your own agent framework.
- **Local TTS option.** Piper TTS runs entirely on-host. EdgeTTS (Microsoft's cloud neural voices) is also supported as a low-friction alternative.
- **Emoji-driven facial expressions.** The LLM's response starts with an emoji (smile, laugh, sad, surprise, thinking, angry, neutral, love, sleepy). The firmware parses it into a face animation on the robot's display. Three layers enforce this: the agent prompt, the server system prompt, and a bridge-level fallback.
- **Child-safety guardrails.** Per-turn sandwich enforcement in the bridge layer keeps the LLM on-topic and in-language. The architecture supports dual kid/adult modes (kid-safe by default).

## Who is this for?

- **Makers** who want a self-hosted voice robot and are comfortable with Docker, SSH, and reading config files.
- **Parents** who want a controllable, inspectable voice assistant for their kids — one where you can read every prompt, every log, and every response.
- **StackChan community members** looking for a working server-side stack to pair with the M5Stack hardware.

This is a hackable starting point, not a product. There are no releases, no installer, no support channel. You deploy it by reading the README, editing config files, and running Docker commands.

## What's in scope

- A working voice pipeline: device audio in, transcription, LLM response, speech out, facial expression.
- Infrastructure-as-config: Docker Compose files, systemd units, custom provider code, config templates with placeholders.
- Documentation for the architecture, protocols, and deployment.
- A reference persona and agent configuration (ZeroClaw + Qwen3).

## What's out of scope

- A polished end-user product. No GUI installer, no app store, no firmware OTA distribution.
- Multi-user / multi-device. The reference deployment is one robot talking to one server.
- Upstream firmware development. We build from `m5stack/StackChan` source but don't maintain firmware patches beyond what's needed for the voice integration.
- Cloud hosting. This is designed for a LAN deployment. You could expose it to the internet, but that's your problem.

## Privacy

All audio processing (VAD, ASR) happens on your LAN. The LLM call is the only thing that crosses your network boundary in the default config, and you choose where it goes. Swap in a local model to keep everything on-premises.

---

## See also

- [README.md](../README.md) — deployment guide, architecture diagrams, ops commands.
- [architecture.md](./architecture.md) — how the components fit together.
- [hardware-support.md](./hardware-support.md) — what hardware you need.
- [faq.md](./faq.md) — common questions.

Last verified: 2026-04-24.
