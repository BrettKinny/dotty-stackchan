---
title: Voice Mode Entry
description: Every way to invite Dotty into a voice turn — wake word, face detection, head-pet hold, clap-to-wake, touchscreen tap, and the LAN admin inject path — with a comparison table covering when each one works.
---

# Voice mode — how to enter it

A "voice turn" is the moment Dotty switches from passive idle to actively listening: the wake-word path opens, the mic opens, ASR captures speech, the LLM responds, and TTS plays. There are six distinct ways to enter that state today. This page collects them in one place, with a comparison table at the end so you can pick the right one for the room you're in.

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

### 4. Clap-to-wake — server-side perception consumer

> **Clap once (or otherwise produce a sharp transient above CLAP_WAKE_MIN_AMPLITUDE).**

Off by default. Opt in with `CLAP_WAKE_ENABLED=true` on the bridge.

Subscribes to the same perception bus the face-greeter uses, filters `sound_event` events by either `data.kind == "clap"` (server-side YAMNet emits this) or `data.energy / data.amplitude` crossing a configurable threshold. On a hit, `_perception_clap_waker` fires the configured `CLAP_WAKE_TEXT` (default "Yes?") through `/admin/inject-text`, which speaks the cue and opens the mic. Per-device cooldown (default 10 s) prevents a sustained clap pattern from looping.

Tunables (env vars on the bridge):

| Variable | Default | Notes |
|---|---|---|
| `CLAP_WAKE_ENABLED` | `false` | Master switch. Consumer isn't even spawned when off. |
| `CLAP_WAKE_MIN_AMPLITUDE` | `0.6` | Loudness threshold for the amplitude fallback. Set to `0` to require an explicit `kind=="clap"` classification. |
| `CLAP_WAKE_COOLDOWN_SEC` | `10` | Per-device cooldown after a successful wake. |
| `CLAP_WAKE_TEXT` | `"Yes?"` | Spoken cue. Empty string ≈ silent open (see `_perception_clap_waker` for the caveat). |

Trade-off vs. wake word: a clap is faster than speaking a phrase but has more false positives in a noisy household. **Useful in the dark when your hands are full** (you can elbow-clap a doorframe).

### 5. Touchscreen tap — firmware

> **Tap the M5 CoreS3 screen 5+ times.**

The I2C touchpad on the CoreS3 is flaky in this build, so a single tap isn't reliable enough to use as a wake gesture; the firmware requires 5+ taps before invoking the listen window. Treat this as a **last-resort fallback**, not a primary entry point. If the screen stops responding entirely, the head-pet hold path (entry #3) is the better backup since it uses a separate GPIO touch input rather than the I2C controller.

### 6. `/admin/inject-text` — server-side LAN admin

> **`curl -XPOST http://<bridge>/admin/inject-text -d '{"text":"...","device_id":"..."}'`**

This is the path the Discord daemon and the portal "Greet" button use. Strictly speaking it doesn't enter voice mode — it bypasses the listen pipeline entirely and inserts text directly into the LLM turn, skipping wake-word + ASR. Useful when you want Dotty to say something **without anyone needing to be physically present**: scheduled greetings, DM-style admin messages, automation hooks. Not exposed to the public internet.

## Comparison

| Entry path | Works in dark | Line of sight | Spoken utterance | Touch | LAN admin | Cooldown |
|---|---|---|---|---|---|---|
| Wake word ("Hi, ESP") | yes | no | yes | no | no | none (always armed) |
| Face detected | no | yes | no | no | no | per-device, `FACE_GREET_MIN_INTERVAL_SEC` |
| Head-pet hold (≥2 s) | yes | no | no | yes | no | none (immediate WakeWordInvoke) |
| Clap-to-wake | yes | no | no (clap only) | no | no | per-device, `CLAP_WAKE_COOLDOWN_SEC` (10 s default) |
| Touchscreen tap (5+) | yes | no (but you need to find the screen) | no | yes | no | none |
| `/admin/inject-text` | yes | no | no | no | yes | none |

## Choosing for your room

- **Lights on, you're across the room**: wake word.
- **Lights on, you're right in front of Dotty**: face detection beats waiting for the wake-net every time.
- **Lights off, hands free**: wake word still works.
- **Lights off, hands full** (carrying laundry, holding a kid): clap-to-wake if enabled, otherwise head-pet hold once you're close enough.
- **Lights off, hands full, mouth full**: head-pet hold.
- **You're not in the room at all**: `/admin/inject-text` from another device on the LAN.

## Cross-references

- [wake-word.md](./wake-word.md) — wake-net details, switching the phrase, microWakeWord roadmap.
- [voice-pipeline.md](./voice-pipeline.md) — what happens *after* the listen window opens (VAD → ASR → LLM → TTS).
- [modes.md](./modes.md) — the broader mode taxonomy and LED contract.
- [interaction-map.md](./interaction-map.md) — every Dotty-side input/output, including the non-voice ones.
