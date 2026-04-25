---
title: Swap Voice
description: Change the TTS voice for Piper (local) or EdgeTTS (cloud).
---

# Swap Voice

Two TTS backends, both configured in `.config.yaml`.

## Piper (local, offline)

1. Download a voice `.onnx` + `.onnx.json` from
   [Piper samples](https://rhasspy.github.io/piper-samples/) into `models/piper/`.

2. Update `.config.yaml`:

```yaml
selected_module:
  TTS: LocalPiper
TTS:
  LocalPiper:
    model_path: models/piper/en_US-lessac-medium.onnx
```

3. Restart: `docker compose restart xiaozhi-server`

## EdgeTTS (cloud, many voices)

1. List voices: `pip install edge-tts && edge-tts --list-voices | grep en-`
2. Update `.config.yaml`:

```yaml
selected_module:
  TTS: EdgeTTS            # or StreamingEdgeTTS
TTS:
  EdgeTTS:
    voice: en-AU-WilliamNeural    # change to your pick
```

3. Restart: `docker compose restart xiaozhi-server`

## Tips

- Piper is fully offline with no latency jitter. Prefer it for reliability.
- EdgeTTS has more variety but needs internet and occasionally throttles.
- **English voices only** -- non-English voices produce empty audio. See
  [voice-pipeline.md](../voice-pipeline.md).
