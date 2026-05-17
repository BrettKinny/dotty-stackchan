---
title: About
description: What Dotty is and why it exists.
---

# About

## What is this?

Dotty is a self-hosted voice assistant built on the M5Stack [StackChan](https://github.com/m5stack/StackChan) desktop robot. You talk to the robot, it talks back -- speech recognition, language model, and text-to-speech all run on your own hardware. Kid Mode is enabled by default, making it safe for young children out of the box. Disable Kid Mode and Dotty becomes a general-purpose voice assistant.

Every component in the pipeline is swappable: the LLM, the TTS engine, the ASR provider, and the persona. The reference config uses Qwen via OpenRouter, but you can swap in Ollama + Piper + FunASR for a fully local deployment with no code changes and no data leaving your network.

## Features

- **Kid-safe by default.** Kid Mode is on out of the box. Per-turn sandwich enforcement in the bridge layer keeps the LLM age-appropriate and on-topic. Disable it for a general-purpose assistant.
- **Fully swappable pipeline.** Every component -- LLM, TTS, ASR, persona -- is a config-level choice. Bring your own models, your own agent framework, or your own personality.
- **Local speech recognition.** FunASR SenseVoiceSmall runs on your Docker host. Audio never leaves your LAN.
- **Pluggable LLM.** The reference config uses Qwen via OpenRouter. Swap in any OpenAI-compatible API or Ollama for fully local inference.
- **Local TTS option.** Piper TTS runs entirely on-host. EdgeTTS (Microsoft's cloud neural voices) is also supported as a low-friction alternative.
- **Emoji-driven facial expressions.** The LLM's response starts with an emoji (smile, laugh, sad, surprise, thinking, angry, neutral, love, sleepy). The firmware parses it into a face animation on the robot's display. Three layers enforce this: the agent prompt, the server system prompt, and a bridge-level fallback.
- **Fully local deployment.** Ollama (LLM) + Piper (TTS) + FunASR (ASR) = zero outbound network calls. Your data stays on your hardware.

## Who is this for?

- **Makers and tinkerers** who want a hackable voice robot they control end-to-end. You own the hardware, the config, the prompts, and the code. If something doesn't work the way you want, you change it.
- **Privacy-conscious users** who don't want their voice data flowing to someone else's cloud. Everything runs on your hardware. The only outbound call in the default config is the LLM, and that's replaceable with a local model.
- **StackChan community members** looking for a batteries-included voice stack for the M5Stack StackChan hardware -- ASR, LLM, TTS, expressions, and persona all wired together and documented.
- **Parents** who want a controllable, inspectable voice assistant for their kids. Kid Mode is enabled by default. You can read every prompt, every log, and every response.

This is a hackable starting point, not a product. There are no releases, no installer, no support channel. You deploy it by reading the README, editing config files, and running Docker commands.

## What makes it different

- **Everything is swappable.** LLM, TTS, ASR, and persona are all config-level choices. The custom provider architecture means you can drop in a new component without touching the rest of the pipeline.
- **Kid-safe by default.** Kid Mode ships enabled. Per-turn prompt enforcement keeps responses age-appropriate. Disable it with a config toggle for general-purpose use.
- **No mandatory cloud.** The default config makes one outbound LLM call. Replace it with Ollama and every byte stays on your LAN.
- **Infrastructure-as-config.** Docker Compose files, systemd units, custom providers, and config templates with placeholders. Clone, substitute your values, deploy.

## What's in scope

- A working voice pipeline: device audio in, transcription, LLM response, speech out, facial expression.
- Infrastructure-as-config: Docker Compose files, systemd units, custom provider code, config templates with placeholders.
- Documentation for the architecture, protocols, and deployment.
- A reference persona and agent configuration (ZeroClaw + Qwen).

## What's out of scope

- A polished end-user product. No GUI installer, no app store, no firmware OTA distribution.
- Multi-user / multi-device. The reference deployment is one robot talking to one server.
- Upstream firmware development. We build from `m5stack/StackChan` source but don't maintain firmware patches beyond what's needed for the voice integration.
- Cloud hosting. This is designed for a LAN deployment. You could expose it to the internet, but that's your problem.

## Privacy

All audio processing (VAD, ASR) happens on your LAN. The LLM call is the only component that crosses your network boundary in the default config, and you choose where it goes. Swap in Ollama to keep everything on-premises -- zero outbound calls, zero cloud dependencies.

---

## See also

- [README.md](../README.md) — deployment guide, architecture diagrams, ops commands.
- [architecture.md](./architecture.md) — how the components fit together.
- [hardware-support.md](./hardware-support.md) — what hardware you need.
- [faq.md](./faq.md) — common questions.

Last verified: 2026-05-17.
