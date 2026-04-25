---
title: Audio Scene Classifier (YAMNet)
description: Server-side audio scene classification — YAMNet TFLite taps the pre-ASR audio frame stream and emits sound_event perception events for doorbells, kettles, baby cries, footsteps, and other home-assistant-relevant classes.
---

# Audio Scene Classifier

Phase 2 of Dotty's perception roadmap. The bridge taps the same 16 kHz
audio frames that flow into ASR, runs them through a quantised
[YAMNet](https://tfhub.dev/google/yamnet/1) model, and emits
`sound_event` perception events whenever a curated home-assistant class
crosses a confidence threshold.

This complements the firmware-side `sound_event(direction)` from the
on-device sound localiser:

| Producer | Says |
|---|---|
| Firmware sound localiser | **Where** the sound came from (`left` / `centre` / `right`) |
| YAMNet audio scene classifier | **What** the sound was (`doorbell`, `kettle`, `dog`, …) |

The two events share the same `name: "sound_event"` envelope on the
perception bus — consumers can subscribe once and get both signals.

## Architecture

```
┌───────────────┐    16 kHz int16 PCM     ┌──────────────────────┐
│ xiaozhi audio │ ──────────────────────► │  AudioSceneClassifier│
│ frame source  │     (pre-ASR tap)       │  (sliding 0.96 s     │
└───────────────┘                         │   buffer, 15 360 spl)│
                                          └──────────┬───────────┘
                                                     │ ThreadPool(1)
                                                     ▼
                                          ┌──────────────────────┐
                                          │  YAMNet TFLite       │
                                          │  (521-class scores)  │
                                          └──────────┬───────────┘
                                                     │ filter +
                                                     │ threshold +
                                                     │ per-class
                                                     │ cooldown
                                                     ▼
                                          ┌──────────────────────┐
                                          │  perception bus      │
                                          │  sound_event(kind=…) │
                                          └──────────────────────┘
```

Single-worker thread pool — if a frame arrives while the previous one
is still classifying, the new frame is dropped. This is intentional:
ASR must never wait on us, and a missed 0.96 s YAMNet window doesn't
matter (the next one is right behind it).

## Curated class list

YAMNet exposes 521 AudioSet classes. Most are noise for a home
assistant (e.g. *Helicopter*, *Pulleys*, *Toot*). The whitelist in
[`bridge/audio_scene.py`](https://github.com/BrettKinny/dotty-stackchan/blob/main/bridge/audio_scene.py)
filters down to classes that should actually trigger Dotty behaviour:

| YAMNet display_name(s) | Friendly `kind` | Rationale |
|---|---|---|
| Doorbell, Ding-dong | `doorbell` | Top-priority household event |
| Knock, Tap | `knock` | Same intent, different doorless homes |
| Baby cry, infant cry | `baby_cry` | Caregiver-relevant alert |
| Crying, sobbing | `crying` | Adult/child distress |
| Dog, Bark | `dog` | Pet engagement / disturbance |
| Cat | `cat` | Pet engagement |
| Music | `music` | Mode switching, mood |
| Speech | `speech` | Generic conversation context |
| Child speech, kid speaking | `child_speech` | Kid Mode signal |
| Kettle whistle | `kettle` | Kitchen ambient awareness |
| Walk, footsteps | `footsteps` | Approach detection (with sound localiser direction) |
| Alarm, Alarm clock | `alarm` | Wake / safety |
| Smoke detector, smoke alarm | `smoke_alarm` | Safety-critical |
| Fire alarm | `fire_alarm` | Safety-critical |
| Telephone bell ringing, Ringtone | `phone` | Comms context |
| Laughter | `laughter` | Mood |
| Silence | `silence` | Useful for VAD-style state |

Edits to the whitelist live in `_USEFUL_CLASSES` at the top of
`bridge/audio_scene.py`. Each class can map to any friendly `kind`
string — multiple display names → one `kind` is fine (used above for
doorbells, dogs, alarms, phones).

## Event shape

```json
{
  "device_id": "bridge",
  "ts": 1735000000.123,
  "name": "sound_event",
  "data": {
    "kind": "doorbell",
    "confidence": 0.812,
    "raw_class": "Doorbell",
    "iso_ts": "2026-04-25T07:30:00.123456+00:00",
    "source": "yamnet"
  }
}
```

Mirrors the existing perception event shape so `data.kind` lives next
to `data.direction` from the firmware path. Consumers should branch
on `data.source` if they only want one or the other.

## Fetch the model

```bash
make fetch-yamnet
```

Downloads the float YAMNet `.tflite` from Google's audioset bucket
into `models/yamnet/yamnet.tflite`, plus the official 521-class CSV
map alongside it. The script is idempotent — re-running with the
file present is a no-op.

For tighter latency on the RPi, generate an INT8 quantised variant
with `tf.lite.TFLiteConverter` (post-training quantisation, default
optimisations, representative dataset of audioset clips). Drop it at
`models/yamnet/yamnet_int8.tflite` and point the env var at it:

```bash
export YAMNET_MODEL_PATH=models/yamnet/yamnet_int8.tflite
```

## Performance notes

YAMNet's compute footprint is small by modern audio-DL standards but
still non-trivial on a Raspberry Pi 4:

| Host | Model | Latency / 0.96 s window | Recommendation |
|---|---|---|---|
| RPi 4 (DietPi) | YAMNet float | ~70–120 ms | Workable; budget 10–15 % CPU |
| RPi 4 (DietPi) | YAMNet INT8 | ~30–50 ms | Comfortable; preferred on Pi |
| Unraid x86_64 (CPU container) | YAMNet float | ~10–20 ms | Plenty of headroom |
| Unraid x86_64 (CPU container) | YAMNet INT8 | ~5–12 ms | Run as many copies as you like |

If RPi contention with the ASR pipeline proves real (look for
`audio_scene` log spam about dropped frames), move the classifier
onto a dedicated container on Unraid and feed it the audio stream
over the existing perception ingest path. The classifier itself is
unchanged — only the host moves.

Single-worker thread pool intentionally caps backpressure: a slow
inference run drops frames rather than queuing them. This means the
classifier emits at *most* one event per frame per class per cooldown
window, regardless of CPU spikes.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `YAMNET_MODEL_PATH` | `models/yamnet/yamnet.tflite` | Path to the `.tflite` model |
| `YAMNET_THRESHOLD` | `0.4` | Minimum class probability to emit |
| `YAMNET_COOLDOWN_SEC` | `5.0` | Per-class cooldown after an emit |

Threshold defaults to `0.4` to bias toward precision over recall — a
spurious doorbell event is more annoying than a missed one. Tune
upward if you see false positives in your environment, downward if
real events are getting filtered.

## Privacy

**All audio processing happens locally.** YAMNet runs inside the
bridge process (or a sibling container on the same LAN host). No
audio frames, embeddings, or class scores leave the device. The only
thing that ever crosses the network is the resulting `sound_event`
metadata — class label, confidence, timestamp — which already crosses
on the perception bus when consumers subscribe.

The model file itself is downloaded once from Google's public
audioset bucket via `make fetch-yamnet`. After that the classifier
is fully offline.

## Optional dependency

`tflite-runtime` is **not** in the bridge's required dependency set.
The classifier module imports cleanly without it; calls to `feed()`
become no-ops and a single warning logs at start time. This keeps
the bridge bootable on hosts where YAMNet hasn't been installed yet.

To enable on the host actually running inference:

```bash
pip install 'tflite-runtime>=2.13'
systemctl restart zeroclaw-bridge   # or docker compose restart bridge
```

## Cross-references

* Firmware sound localiser: see [`docs/protocols.md`](protocols.md)
  for the `sound_event(direction)` shape on the wire.
* Perception bus: see [`docs/architecture.md`](architecture.md) for
  the in-process pub/sub pattern shared with `face_detected` /
  `face_lost`.
* Mode taxonomy: see [`docs/modes.md`](modes.md) — audio scene
  events feed the smart-mode LED hybrid logic shipped in the same
  OTA cycle as this scaffold.
