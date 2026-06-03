# Pinned 2026-05-17 from :server_0.9.3
FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server@sha256:3accd82a7d1a6c01c58f32f6199400a11655d607780b80219493123dedbb347e

RUN pip install --no-cache-dir piper-tts scipy numpy mido faster-whisper sherpa-onnx==1.13.2

# fluidsynth + General MIDI soundfont for runtime rendering of dance/song MIDI
# files to Opus. Installed as the LAST layer so iteration on Python deps above
# doesn't invalidate the soundfont download (~141MB).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm \
    && rm -rf /var/lib/apt/lists/*
