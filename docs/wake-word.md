---
title: Wake Word
description: How Dotty's wake word works today (ESP-SR WakeNet9 "Hi, ESP"), how to switch to a different prebuilt English wake word, and the roadmap to a custom "Hey Dotty" model.
---

# Wake word

Dotty listens passively for a fixed phrase and only opens the conversation pipeline once it hears one. This page is the canonical reference for **what wake word fires today, how to change it, and the path to a branded "Hey Dotty"**.

## TL;DR

- **Today:** firmware ships with `SR_WN_WN9_HIESP` — phrase **"Hi, ESP"** — running on ESP-SR's WakeNet9 + AFE on the ESP32-S3 (PSRAM model). `SR_WN_WN9_HISTACKCHAN_TTS3` ("Hi, Stack Chan") was the earlier default and is kept available as an opt-in alternative.
- **Five-minute alternate (Path A):** flip `sdkconfig.defaults` to one of the other prebuilts — e.g. `SR_WN_WN9_COMPUTER_TTS=y` ("Computer") or `SR_WN_WN9_HISTACKCHAN_TTS3=y` ("Hi, Stack Chan"). Reflash. No code change needed — the handler is wake-phrase-agnostic.
- **Branded "Hey Dotty" (Path B, recommended long-term):** train a microWakeWord (TFLite) model on ~50–100 positive samples + ~1,000 negatives, quantise INT8, ship as a wake-word partition. Firmware integration hook is documented in `firmware/main/stackchan/wake_word/microwakeword_setup.md` in the firmware repo.
- **Path C (custom WakeNet9):** Espressif's first-party trainer. Smaller binary, more friction. Not recommended unless Path B blocks.

## Architecture (today)

```
Mic PCM ──▶ AFE (AEC + NS + VAD) ──▶ WakeNet9 ──▶ "wake event" ──▶ Application::HandleWakeWordDetectedEvent
                                          │
                                          └── model: wn9_hiesp (loaded from flash via esp_srmodel_init("model"))
```

- AFE = Audio Front End. Does acoustic-echo-cancel, noise-suppress, voice-activity-detect on the dual-mic input before the wake-net sees it. Mandatory on the S3 + PSRAM build.
- The wake-net `models_` list is populated at boot from a SPIFFS partition labelled `model`. `AfeWakeWord::Initialize` walks the list and picks the first entry whose name starts with `ESP_WN_PREFIX` ("wn"). That string is set by `idf.py menuconfig` → ESP Speech Recognition → Load Multiple Wake Words.
- Wake event flow: `AfeWakeWord::AudioDetectionTask` → `wake_word_detected_callback_` (set in `audio_service.cc`) → `Application::HandleWakeWordDetectedEvent` (`xiaozhi-esp32/main/application.cc:793`) → `ContinueWakeWordInvoke` → opens WebSocket and switches to listening state.

## File map (firmware repo)

| Concern | Path | Notes |
|---|---|---|
| Wake-word type selection (Kconfig) | `firmware/xiaozhi-esp32/main/Kconfig.projbuild:674` | `WAKE_WORD_TYPE` choice — AFE / ESP / Custom / Disabled. |
| Active wake-word model | `firmware/sdkconfig.defaults:20` | `CONFIG_SR_WN_WN9_HIESP=y` (the line to change). The previous default (`CONFIG_SR_WN_WN9_HISTACKCHAN_TTS3`) is preserved one line below, commented out. |
| AFE wrapper | `firmware/xiaozhi-esp32/main/audio/wake_words/afe_wake_word.cc` | Loads model, runs detection, emits callback. |
| Custom multi-net path | `firmware/xiaozhi-esp32/main/audio/wake_words/custom_wake_word.cc` | Pinyin-string-based — for Chinese custom wake. Not the right hook for "Hey Dotty". |
| State machine | `firmware/xiaozhi-esp32/main/application.cc:872` | `HandleStateChangedEvent` re-arms wake detection on idle/listening/speaking transitions. |
| esp-sr version | `firmware/xiaozhi-esp32/main/idf_component.yml` | `espressif/esp-sr: ~2.3.0` |

## Path A — switch to a prebuilt English wake word (ships today)

**Effort:** ~30 min including flash + verify. **Risk:** trivial — no code path changes.

### Available English prebuilt wake words

All entries below are options under `Component config → ESP Speech Recognition → Load Multiple Wake Words (WakeNet9)` in `idf.py menuconfig`, on the S3+PSRAM target. Pick exactly one.

| sdkconfig key | Phrase |
|---|---|
| `SR_WN_WN9_HIESP` | "Hi, ESP" *(default today)* |
| `SR_WN_WN9_ALEXA` | "Alexa" |
| `SR_WN_WN9_JARVIS_TTS` | "Jarvis" |
| `SR_WN_WN9_COMPUTER_TTS` | "Computer" |
| `SR_WN_WN9_HEYWILLOW_TTS` | "Hey, Willow" |
| `SR_WN_WN9_HIMFIVE` | "Hi, M Five" |
| `SR_WN_WN9_SOPHIA_TTS` | "Sophia" |
| `SR_WN_WN9_HEYWANDA_TTS` | "Hey, Wanda" |
| `SR_WN_WN9_HIJOLLY_TTS2` | "Hi, Jolly" |
| `SR_WN_WN9_HIFAIRY_TTS2` | "Hi, Fairy" |
| `SR_WN_WN9_HEYPRINTER_TTS` | "Hey, Printer" |
| `SR_WN_WN9_MYCROFT_TTS` | "Mycroft" |
| `SR_WN_WN9_HIJOY_TTS` | "Hi, Joy" |
| `SR_WN_WN9_HIJASON_TTS2` | "Hi, Jason" |
| `SR_WN_WN9_ASTROLABE_TTS` | "Astrolabe" |
| `SR_WN_WN9_HEYILY_TTS2` | "Hey, Ily" |
| `SR_WN_WN9_BLUECHIP_TTS2` | "Blue Chip" |
| `SR_WN_WN9_HIANDY_TTS2` | "Hi, Andy" |
| `SR_WN_WN9_HEYIVY_TTS2` | "Hey, Ivy" |
| `SR_WN_WN9_HISTACKCHAN_TTS3` | "Hi, Stack Chan" *(prior default; kept commented out in sdkconfig.defaults)* |
| `SR_WN_WN9_HEYKIRA_TTS3` | "Hey, Kira" |

There is no prebuilt "Hey Dotty" — that is what Path B is for. The closest single-syllable-after-"Hey" prebuilts are **Hey Ily**, **Hey Ivy**, and **Hey Kira**. The current shipped default is **"Hi, ESP"** — best-trained English model in the catalog. **"Computer"** is the easiest zero-friction alternative for kids.

### How to switch

Two equivalent routes — pick one.

**Route 1 — edit `sdkconfig.defaults` (recommended, version-controlled):**

1. In the firmware repo, open `firmware/sdkconfig.defaults`.
2. Comment out the active line `CONFIG_SR_WN_WN9_HIESP=y` (line 20) and uncomment / add the line for the desired key, e.g. `CONFIG_SR_WN_WN9_COMPUTER_TTS=y`. Only one `SR_WN_WN9_*` line should be active at a time.
3. Delete `firmware/sdkconfig` and `firmware/build/` so the next build regenerates from defaults. (Otherwise the cached `sdkconfig` keeps the old value.)
4. `idf.py build flash monitor`.
5. Verify: serial log should print `Model 0: wn9_<key>` during `AfeWakeWord::Initialize`. Speak the new phrase — expect `Wake word detected: <key>` in log.

**Route 2 — `idf.py menuconfig` (interactive, one-off):**

1. `idf.py menuconfig`.
2. Navigate: `Component config → ESP Speech Recognition → Load Multiple Wake Words (WakeNet9)`.
3. Uncheck `Hi, ESP (wn9_hiesp)`. Check the desired entry.
4. Save. `idf.py build flash monitor`.
5. To make the change durable, run `idf.py save-defconfig` and commit the resulting `sdkconfig.defaults`.

### Revert

Restore `CONFIG_SR_WN_WN9_HIESP=y` on line 20 of `firmware/sdkconfig.defaults`, delete `firmware/sdkconfig` + `firmware/build/`, rebuild.

## Path B — microWakeWord "Hey Dotty" (recommended long-term)

**Effort:** ~2 weeks calendar (sample collection across distances/speakers/days), ~1 person-week of focused work. **Risk:** medium — first-time training pipeline; quantisation gotchas; requires wake-word partition table change.

[microWakeWord](https://github.com/kahrendt/microWakeWord) (Kevin Ahrendt / Home Assistant project) is a TFLite-Micro-based wake-word framework specifically designed for ESP32-S3-class hardware. It is the same engine ESPHome's "Voice Assistant" uses. Outputs an `~80 KB` INT8 streaming-quantised model with detection latency under 100 ms.

### Why microWakeWord over WakeNet9 custom

- Open training pipeline (Python + TF), runs on Colab GPU.
- Requires **far fewer positive samples** (~50–100 vs WakeNet's ~1000+).
- Negative-set augmentation pipeline included (background noise, music, TV, speech).
- Streaming inference — designed to run continuously, low CPU, low RAM.
- Supported by an active community; Espressif's custom-WakeNet trainer is harder to access.

### Sample collection plan

The robot will mostly hear a kid's voice. Models trained only on adult voices misfire on children — kids' fundamental frequency is ~250–400 Hz vs adult ~85–180 Hz. Plan accordingly.

**Positive set — ~80 utterances of "Hey Dotty":**
- 40 from the kid the robot lives with, across 3 sessions on 3 different days (voice changes daily — colds, tiredness, excitement).
- 20 from a second adult voice (parent, second child, friend) — generalisation.
- 10 whisper / 10 shouted — energy-range coverage.
- Mix distances: 0.5 m, 1.5 m, 3 m. Mix angles: facing, side-on, behind.
- Mix backgrounds: quiet room, TV on low, dishwasher running, music on.
- Record at the device's mic format (16 kHz mono, 16-bit PCM) using the device itself if possible (a `record_dump` MCP tool can be added to the firmware), otherwise a phone mic in the same room.

**Negative set — ~1,500 clips:**
- ~500 random household audio clips (kid yelling, parent talking, TV chatter, music).
- ~500 clips from public datasets — Common Voice, AudioSet, Librispeech.
- ~500 hard negatives — phrases that *sound like* "Hey Dotty": "Hey Daddy", "Hey, body", "Hey Polly", "Heydoddy", "ate a dot tea". microWakeWord's training notebook has a TTS-based hard-negative generator; use it.

**Privacy:** all training audio stays on Brett's workstation. Nothing uploads to a third party. The trained `.tflite` artifact is what ships to the device; raw audio never leaves the lab. This is a non-negotiable per [kid-mode.md](./kid-mode.md).

### Training pipeline

1. Clone microWakeWord, install requirements (TF 2.16+, `audiomentations`, `librosa`).
2. Drop positives into `data/positive/`, negatives into `data/negative/`. Run the included augmentation step (pitch shift ±2 semitones, time stretch 0.9–1.1, room IRs, SNR shaping –5 to +20 dB).
3. Train on Colab T4 GPU, ~30–60 min for the default streaming-DS-CNN architecture. The notebook outputs `model.tflite` (INT8 quantised) and a JSON manifest with the detection threshold.
4. Sanity-check on hold-out set: target ≥95% true-positive rate, ≤1 false-positive per 8 hours of negative audio. If TP < 90%, collect more positives. If FP > 5/8h, collect more hard negatives.
5. Convert to ESP-IDF embedded form: either flash the `.tflite` to a dedicated partition and `mmap` at runtime, or `xxd`-dump it into a header.

### Firmware integration

See `firmware/main/stackchan/wake_word/microwakeword_setup.md` (in the firmware repo) for partition-table additions, the streaming-inference task scaffold, and the integration point in `Application::HandleWakeWordDetectedEvent`. The plan is to keep `AfeWakeWord` as a sibling so AEC + AGC + VAD still run; only the WakeNet inference pass is replaced with the microWakeWord TFLite micro-interpreter call.

## Path C — custom WakeNet9 (not recommended)

Espressif's first-party trainer for WakeNet9 is gated (requires submitting samples to Espressif's online portal and waiting for a generated `.bin`). Smaller artifact, but slower iteration loop and no community tooling around it. Use Path C only if Path B's TFLite-Micro inference proves too heavy on the S3 (it shouldn't — the S3 has 8 MB PSRAM and microWakeWord runs in <300 KB at runtime).

## Wake-word events on the wire

Once the wake event fires, `protocol_->SendWakeWordDetected(wake_word)` (`application.cc:859`) sends the detected phrase string to the server as the first turn of the conversation. With `CONFIG_SEND_WAKE_WORD_DATA=y` (current default) it also sends the captured wake-word PCM as Opus packets, so the server-side ASR has audio context if the user runs the wake phrase straight into a query ("Hey Dotty, what's the weather?"). This is what gives the robot its responsive "fast first turn" feel.

## Cross-references

- [Voice pipeline](./voice-pipeline.md) — what happens after the wake word fires (VAD → ASR → LLM → TTS).
- [Proactive greetings](./proactive-greetings.md) — Layer 6 lets the robot speak first; wake word still gates user-initiated speech.
- [Kid mode](./kid-mode.md) — privacy + training-data-stays-local rationale.
- [Modes & LED contract](./modes.md) — wake event triggers `idle → listening` and the LED ring transition.
