---
title: Voice Mode Entry
description: Every way to invite Dotty into a voice turn — wake word, face detection, head-pet hold, and the LAN admin inject path — with a comparison table covering when each one works.
---

# Voice mode — how to enter it

A "voice turn" is the moment Dotty switches from passive idle to actively listening: the wake-word path opens, the mic opens, ASR captures speech, the LLM responds, and TTS plays. There are four distinct ways to enter that state today. This page collects them in one place, with a comparison table at the end so you can pick the right one for the room you're in.

The default phrase is **"Hi, ESP"** (firmware shipped this as the default in a recent change). See [wake-word.md](./wake-word.md) for how to change it.

## Entry paths

### 1. Wake word — firmware

> **"Hi, ESP" (default), "Hi, Stack Chan", "Computer", or any of the prebuilt WakeNet9 phrases.**

The classic path. The mic is always sampling at low cost (AFE + WakeNet9 INT8 on the ESP32-S3). When the wake-net spots the phrase, `Application::HandleWakeWordDetectedEvent` opens the WebSocket and the device transitions to listening. Works with no line of sight, no touch, no LAN. **Requires the user to speak.**

Cross-link: [wake-word.md](./wake-word.md) covers the whole stack — current model, the five-minute switch to a different prebuilt, and the long-term branded "Hey Dotty" microWakeWord roadmap.

### 2. Face detection — firmware → bridge

The on-device face tracker (PSRAM-mounted ESP-WHO model in `firmware/firmware/main/stackchan/modifiers/face_tracking.cpp`) emits a `face_detected` perception event when a human face enters the camera's field of view. The xiaozhi-server-side relay forwards that event to the bridge, where `_perception_face_greeter` (in `bridge.py`) injects the configured greeting (`FACE_GREET_TEXT`, default "Hi!") via the xiaozhi `/admin/inject-text` route. xiaozhi speaks the greeting and opens the mic for the reply.

Per-device cooldown stops a stationary user from re-triggering on every blink of the tracker. Set `FACE_GREET_TEXT=""` to suppress the verbal greeting and just open the mic silently — see the env block in `bridge.py` for the full set of knobs.

**Requires line of sight.** Useless in the dark or when the camera is occluded.

### 3. Head-pet hold — firmware

> **Hold a finger on Dotty's head capacitive pad for ≥2 seconds.**

Shipped in firmware commit `e8370d2`. The head-pet handler distinguishes a quick swipe (visual purr feedback only — see Path B in `head_pet.h`) from a sustained hold; on hold-detected the firmware fires `Application::WakeWordInvoke("head_pet_hold")` directly, which is exactly what the wake-net would do for a real wake-word hit. The mic opens, no audible cue.

This is the **dark-room friendly** entry point: works with no light, no line of sight, no spoken phrase. Brett's primary use case is morning interactions before the lights are up.

### 4. `/admin/inject-text` — server-side LAN admin

> **`curl -XPOST http://<bridge>/admin/inject-text -d '{"text":"...","device_id":"..."}'`**

This is the path the Discord daemon and the portal "Greet" button use. Strictly speaking it doesn't enter voice mode — it bypasses the listen pipeline entirely and inserts text directly into the LLM turn, skipping wake-word + ASR. Useful when you want Dotty to say something **without anyone needing to be physically present**: scheduled greetings, DM-style admin messages, automation hooks. Not exposed to the public internet.

## Comparison

| Entry path | Works in dark | Line of sight | Spoken utterance | Touch | LAN admin | Cooldown |
|---|---|---|---|---|---|---|
| Wake word ("Hi, ESP") | yes | no | yes | no | no | none (always armed) |
| Face detected | no | yes | no | no | no | per-device, `FACE_GREET_MIN_INTERVAL_SEC` |
| Head-pet hold (≥2 s) | yes | no | no | yes | no | none (immediate WakeWordInvoke) |
| `/admin/inject-text` | yes | no | no | no | yes | none |

## Choosing for your room

- **Lights on, you're across the room**: wake word.
- **Lights on, you're right in front of Dotty**: face detection beats waiting for the wake-net every time.
- **Lights off, hands free**: wake word still works.
- **Lights off, hands full** (carrying laundry, holding a kid): head-pet hold once you're close enough.
- **Lights off, hands full, mouth full**: head-pet hold.
- **You're not in the room at all**: `/admin/inject-text` from another device on the LAN.

## Cross-references

- [wake-word.md](./wake-word.md) — wake-net details, switching the phrase, microWakeWord roadmap.
- [voice-pipeline.md](./voice-pipeline.md) — what happens *after* the listen window opens (VAD → ASR → LLM → TTS).
- [modes.md](./modes.md) — the broader mode taxonomy and LED contract.
- [interaction-map.md](./interaction-map.md) — every Dotty-side input/output, including the non-voice ones.
