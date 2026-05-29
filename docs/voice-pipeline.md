---
title: Voice Pipeline
description: xiaozhi-esp32-server pipeline stages -- VAD, ASR, LLM proxy, and TTS.
---

# Voice pipeline — xiaozhi-esp32-server

## TL;DR

- **Server** is `xinnan-tech/xiaozhi-esp32-server` running in Docker on a Linux host. Plugin-based: each of VAD, ASR, LLM, TTS, Memory, Intent is a swappable provider picked via `data/.config.yaml`'s `selected_module:` block.
- Our live pipeline: **SileroVAD** (speech-end) → **FunASR SenseVoiceSmall** or **WhisperLocal** (ASR, pinned to English) → **PiVoiceLLM** custom provider (current default — `docker exec -i dotty-pi pi --mode rpc` over stdio, brain is the `dotty-pi` container) or **OpenAICompat** (alternate — points straight at any OpenAI-compatible endpoint) → **LocalPiper** en_GB-cori-medium (TTS; EdgeTTS / StreamingEdgeTTS as alternates).
- The xiaozhi container also runs a perception relay (`EventTextMessageHandler`) that forwards firmware `face_detected` / `face_lost` / `sound_event` / `state_changed` frames to `dotty-behaviour`'s `/api/perception/event`.
- **Emotion** is not a pipeline stage — it's extracted post-hoc from the LLM's emoji prefix and emitted as a separate WS frame. See [protocols.md](./protocols.md#emotion-protocol).
- Custom providers are mounted into the container via Docker volumes at `/opt/xiaozhi-esp32-server/core/providers/{asr,tts,llm}/…`. They override the baked-in files at module-import time.
- **Lots of upstream features are unused** — voiceprint speaker-ID, VLLM vision, knowledge-base RAG, PowerMem, multi-user routing. See [latent-capabilities.md](./latent-capabilities.md#voice-pipeline-unused).

## Provider catalog (upstream)

From the `xinnan-tech/xiaozhi-esp32-server` README (see [references.md](./references.md#voice)):

| Stage | Provider options |
|---|---|
| **VAD** | SileroVAD (local, free) |
| **ASR (local)** | FunASR, SherpaASR |
| **ASR (cloud)** | FunASRServer, Volcano Engine, iFLYTEK, Tencent Cloud, Alibaba Cloud, Baidu Cloud, OpenAI |
| **LLM** | OpenAI-compatible (Alibaba Bailian, Volcano, DeepSeek, Zhipu, Gemini, iFLYTEK), Ollama, Dify, FastGPT, Coze, Xinference, HomeAssistant |
| **VLLM** (vision) | Alibaba Bailian, Zhipu ChatGLM |
| **TTS (local)** | FishSpeech, GPT_SOVITS_V2/V3, Index-TTS, PaddleSpeech |
| **TTS (cloud)** | EdgeTTS, iFLYTEK, Volcano, Tencent, Alibaba, CosyVoice, OpenAI TTS |
| **Memory** | mem0ai, PowerMem, mem_local_short, nomem |
| **Intent** | intent_llm, function_call, nointent |
| **Knowledge base** | RagFlow |

**What we use:** SileroVAD + FunASR (patched) + custom PiVoiceLLM + LocalPiper (or EdgeTTS on rollback). Every other row is unused.

## Our deployed stages

### VAD — SileroVAD

SileroVAD v6.x, JIT model ~2 MB, runs on the server CPU, <1 ms per chunk in practice. 8 kHz or 16 kHz sample rates supported; xiaozhi-server uses 16 kHz to match the device Opus stream.

Tunables live under `VAD.SileroVAD.*` in `data/.config.yaml`:

| Tunable | Meaning | Our value |
|---|---|---|
| `min_silence_duration_ms` | Silence length after speech to call it "end" | 700 |
| `threshold` | Speech-confidence threshold (0–1) | upstream default |
| `speech_pad_ms` | Extra audio captured either side of detected speech | upstream default |
| `neg_threshold` | Below-this-probability = definitely silence | upstream default |

Known limit: **whispered speech under-triggers**. If the robot stops responding to a quieter speaker, this is the first thing to check.

### ASR — FunASR SenseVoiceSmall (patched)

Model: `FunAudioLLM/SenseVoiceSmall` on HuggingFace. From the model card:

- Supports 50+ languages total; the five *tested* languages are Mandarin (`zh`), Cantonese (`yue`), English (`en`), Japanese (`ja`), Korean (`ko`). Plus `nospeech`.
- Parameter count ~= Whisper-Small.
- **70 ms to process 10 s of audio — 15× faster than Whisper-Large, 5× faster than Whisper-Small.**
- Non-autoregressive end-to-end architecture (fast, no decode loop).

**Our patch.** Upstream `fun_local.py` hardcodes `language="auto"`, which mis-detects short or unclear English as Korean or Japanese. The repo-hosted `fun_local.py` adds a `language` config key (read from `ASR.FunASR.language` in `.config.yaml`) and passes it through to `model.generate`. We set `language: en`.

Deployment: mounted as a file-level override at `/opt/xiaozhi-esp32-server/core/providers/asr/fun_local.py`.

### LLM — provider selected at a time

Pick one via `selected_module.LLM` in `.config.yaml`. The default is `PiVoiceLLM`; `OpenAICompat` is the alternate. See [llm-backends.md](./llm-backends.md) for the full comparison.

#### `PiVoiceLLM` (default)

Custom provider at `custom-providers/pi_voice/` (mounted into `/opt/xiaozhi-esp32-server/core/providers/llm/pi_voice/`). It doesn't run a model itself — it hands each voice turn to the **`dotty-pi` container** by running `docker exec -i dotty-pi pi --mode rpc` and exchanging JSONL messages over stdio. The pi agent owns the conversation loop (`qwen3.5:4b` on local llama-swap) and the five `dotty-pi-ext` voice tools (`memory_lookup`, `remember`, `think_hard`, `take_photo`, `play_song`); only TTS-bound text streams back. See [brain.md](./brain.md).

#### `OpenAICompat` (alternate)

Custom provider at `custom-providers/openai_compat/openai_compat.py`. Talks directly to any OpenAI-compatible `/v1/chat/completions` endpoint — a cloud provider (OpenAI, OpenRouter) or a local llama-swap instance. Stateless and tool-less: no memory and no voice tools, so it's a chitchat-only alternate to the full `PiVoiceLLM` path. See [llm-backends.md](./llm-backends.md).

### Perception relay (xiaozhi → dotty-behaviour)

`custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py` adds an `EventTextMessageHandler` that intercepts firmware `event` frames over the WS and POSTs each one to `dotty-behaviour`'s `/api/perception/event`. This is what feeds the `dotty-behaviour` perception consumers — see [architecture.md](./architecture.md#perception-event-bus).

### TTS — LocalPiper (active) / EdgeTTS (rollback)

**Active: Piper local.**
- Engine: piper-tts 1.4.2 on ONNX runtime.
- Voice: `en_GB-cori-medium` (Piper "medium" quality tier, British English).
- Voice files (~63 MB total): `.onnx` + `.onnx.json` sibling, fetched from `huggingface.co/rhasspy/piper-voices`.
- Measured on a modest i5-3570 Docker host: 0.22 s synth for 2.8 s of audio — 12.7× realtime.
- Image: `xiaozhi-esp32-server-piper:local` (local `Dockerfile` extends the upstream image with piper-tts).
- Runs fully offline — no external HTTP calls.
- **License note (unverified).** Piper voices are MIT-licensed as a repo, but individual voices carry their own upstream license depending on training data. Verify the Cori-specific voice license before redistributing your robot's recordings beyond personal use. Starting point: [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).

**Rollback: EdgeTTS (`type: edge`).**
- Uses Microsoft's unofficial Edge "Read aloud" endpoint (reverse-engineered; no official API key).
- Voice: `en-US-AnaNeural` (our previous child-sounding voice).
- Streaming supported; non-streaming is the default that ships with the upstream image.
- **Known failure mode**: returns silent audio when the input text is not in the voice's language. This is the symptom we chased for the Qwen-Chinese-leak bug — an `en-US-*` voice with Chinese text = empty buffer, not an error.
- **Risk**: MS can rate-limit, change endpoints, or kill the product. Keep an eye on [rany2/edge-tts](https://github.com/rany2/edge-tts) for ecosystem signals.

One-line rollback command is in `../README.md` → "Common ops".

## Custom provider mechanism

xiaozhi-server discovers providers by module path. `selected_module.TTS: LocalPiper` resolves to `core/providers/tts/piper_local` (snake_case of the module dir), and the server imports its class. Docker volume-mounting a local file *over* the container's baked-in file is therefore enough to patch or replace a provider — no image rebuild required for single-file overrides.

**Implication for upgrades.** When the upstream image changes, the mount still works as long as:
1. The provider-directory convention hasn't changed.
2. The provider base-class signature hasn't changed.

Both of those do occasionally break on upstream major bumps. Pin the image tag in `docker-compose.yml` and test an upgrade on a branch before merging.

## Emotion handling inside the pipeline

xiaozhi-server doesn't run an emotion classifier. It **strips the leading emoji** from the LLM response text, maps it to an emotion identifier from the Xiaozhi emotion catalog (see [protocols.md](./protocols.md#emotion-protocol)), and emits two separate WS frames to the device:
- `{"type":"llm","emotion":"…","text":"😊"}`
- `{"type":"tts","state":"sentence_start","text":"Sure, the weather…"}`

The TTS provider receives text **with the emoji already stripped**. The device receives the emotion and sets the face animation; the speaker plays the clean text.

**Surprising consequence**: the LLM must emit the emoji as its very first character for emotion dispatch to fire. On the `PiVoiceLLM` path, enforcement relies on the persona prompt and the `.config.yaml` `prompt:` block — the `bridge.py` `_ensure_emoji_prefix` fallback only applies to the retired ZeroClaw path. See [protocols.md](./protocols.md#emotion-protocol) for the enforcement layers.

**Note — we don't use SenseVoice's built-in SER.** The model card advertises speech emotion recognition and audio-event detection (bgm / applause / laughter / crying / coughing / sneezing). xiaozhi-server's FunASR provider returns only the transcription text; the SER/AED fields aren't piped through. That's a genuine latent capability — see [latent-capabilities.md](./latent-capabilities.md#voice-pipeline-unused).

## See also

- [protocols.md](./protocols.md#xiaozhi-websocket) — how audio gets in and out (and the `/api/perception/event` wire format).
- [brain.md](./brain.md) — the pi agent, model matrix, and dotty-pi-ext voice tools.
- [latent-capabilities.md](./latent-capabilities.md#voice-pipeline-unused) — unused upstream features.
- [references.md](./references.md#voice) — all upstream voice-stack links.

Last verified: 2026-05-17.
