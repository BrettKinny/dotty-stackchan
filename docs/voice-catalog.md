---
title: Voice Catalog
description: Curated Piper and EdgeTTS voices that suit a Dotty-class kid-friendly robot persona.
---

# Voice Catalog

A short, curated list of TTS voices that play well with Dotty's persona —
warm, cheerful, easy on the ear at low volume on a tiny speaker. The full
upstream catalogues are huge; this page is the opinionated subset we've
actually listened to and like.

For instructions on switching, see [Swap Voice](cookbook/swap-voice.md).
For an automated download of any Piper voice listed below, see the
[install helper](#install-helper) section at the bottom.

## Quick guide

- **Piper** runs locally, no cloud, no jitter. Prefer it for reliability.
- **EdgeTTS** has more variety and naturalness but needs internet.
- "Best for" is opinion only — try a couple, your room and speaker matter.
- Sample rate `22050` Hz is the Piper default; the firmware resamples
  transparently. File sizes are approximate.

## Piper voices

All voices live on the public mirror at
[huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
Each voice ships as a `.onnx` model plus a `.onnx.json` config — both
needed.

| Key                              | Lang   | Quality | Character           | Best for      | Size  |
|----------------------------------|--------|---------|---------------------|---------------|-------|
| `en_US-amy-medium`               | en_US  | medium  | Warm, friendly      | Kid + Adult   | ~63 MB |
| `en_US-amy-low`                  | en_US  | low     | Warm, friendly      | Kid + Adult   | ~28 MB |
| `en_US-kristin-medium`           | en_US  | medium  | Cheerful, bright    | Kid Mode      | ~63 MB |
| `en_US-hfc_female-medium`        | en_US  | medium  | Neutral, clear      | Adult         | ~63 MB |
| `en_US-lessac-medium`            | en_US  | medium  | Neutral, articulate | Adult         | ~63 MB |
| `en_US-lessac-low`               | en_US  | low     | Neutral, articulate | Adult         | ~28 MB |
| `en_US-libritts_r-medium`        | en_US  | medium  | Multi-speaker       | Both          | ~75 MB |
| `en_GB-cori-medium`              | en_GB  | medium  | Soft, warm UK       | Kid + Adult   | ~63 MB |
| `en_GB-jenny_dylan-medium`       | en_GB  | medium  | Playful, lively UK  | Kid Mode      | ~63 MB |
| `en_GB-southern_english_female-low` | en_GB | low     | Cheerful UK         | Kid + Adult   | ~28 MB |
| `en_GB-alba-medium`              | en_GB  | medium  | Scottish, cosy      | Both          | ~63 MB |
| `en_GB-semaine-medium`           | en_GB  | medium  | Neutral UK          | Adult         | ~63 MB |

The default voice that ships with `make fetch-models` is
`en_GB-cori-medium` — a safe, friendly starting point.

### Notes on quality tiers

- `low` (16 kHz, ~28 MB) is fine for casual chat on a small speaker. The
  Pi can synthesize it at well over realtime even on a Pi 4.
- `medium` (22050 Hz, ~63 MB) is the sweet spot for desk listening.
- `high` exists for some voices (~110 MB) but the difference is hard to
  hear through the StackChan's tiny driver — skip it.

## EdgeTTS voices

EdgeTTS calls Microsoft's cloud, which means latency jitter and
occasional throttling, but you get a much wider voice pool. Use the slug
in the `voice:` field under `TTS.EdgeTTS` (or `TTS.StreamingEdgeTTS`).

| Slug                       | Lang   | Character             | Best for      |
|----------------------------|--------|-----------------------|---------------|
| `en-AU-NatashaNeural`      | en-AU  | Warm, friendly AU     | Kid + Adult   |
| `en-AU-WilliamNeural`      | en-AU  | Calm, neutral AU      | Adult         |
| `en-GB-SoniaNeural`        | en-GB  | Warm, professional UK | Both          |
| `en-GB-MaisieNeural`       | en-GB  | Young, cheerful UK    | Kid Mode      |
| `en-US-AriaNeural`         | en-US  | Bright, expressive US | Both          |
| `en-US-JennyNeural`        | en-US  | Friendly assistant US | Both          |

To list every available voice yourself:

```bash
pip install edge-tts
edge-tts --list-voices | grep en-
```

## Install helper

To download any Piper voice from the table above into `models/piper/`:

```bash
make voice-list                                  # show this catalog
make voice-install VOICE=en_US-kristin-medium    # download only
make voice-install VOICE=en_US-kristin-medium APPLY=1   # download + edit .config.yaml
```

The same script is at `scripts/voice-install.sh` if you'd rather call
it directly. Run `./scripts/voice-install.sh --help` for flags.

After installing a Piper voice, run `make doctor` to verify the file is
in place, then restart the server: `docker compose restart xiaozhi-server`.

## How to switch voices

See [Swap Voice](cookbook/swap-voice.md) for the full walkthrough on
editing `.config.yaml` for either backend. The short version:

```yaml
selected_module:
  TTS: LocalPiper
TTS:
  LocalPiper:
    voice: en_US-kristin-medium
    model_path: /opt/xiaozhi-esp32-server/models/piper/en_US-kristin-medium.onnx
    config_path: /opt/xiaozhi-esp32-server/models/piper/en_US-kristin-medium.onnx.json
```

Then `docker compose restart xiaozhi-server`.

Last verified: 2026-04-25.
