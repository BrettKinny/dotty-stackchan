# Pinned 2026-05-17 from :server_0.9.3
FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server@sha256:3accd82a7d1a6c01c58f32f6199400a11655d607780b80219493123dedbb347e

# Versions pinned to the image that passed the 2026-10-20 A/V run; an unpinned
# rebuild could otherwise change the Whisper stack underneath WhisperLocal.
RUN pip install --no-cache-dir piper-tts==1.8.0 scipy==1.15.3 numpy==1.26.4 mido==1.3.3 \
        faster-whisper==1.2.1 ctranslate2==4.8.3 sherpa-onnx==1.13.2

# CTranslate2 (faster-whisper) dlopens libcublas.so.12 and cuDNN by name and
# doesn't look inside the pip CUDA wheels, so WhisperLocal on device=cuda fails
# every utterance with "libcublas.so.12 is not found" without this.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/site-packages/nvidia/cudnn/lib

# Patch upstream TTS consumers and the audio sender to treat explicitly
# registered server-push sentence IDs as independent from chat-turn
# stale-message arbitration (#104).
COPY scripts/patch-tts-server-push.py /tmp/patch-tts-server-push.py
RUN python /tmp/patch-tts-server-push.py \
        /opt/xiaozhi-esp32-server/core/providers/tts \
        /opt/xiaozhi-esp32-server/core/handle/sendAudioHandle.py

# Keep ~1.2 s of audio from before the VAD fires (upstream ~0.6 s), so a soft
# first word still reaches ASR.
COPY scripts/patch-asr-preroll.py /tmp/patch-asr-preroll.py
RUN python /tmp/patch-asr-preroll.py /opt/xiaozhi-esp32-server/core/providers/asr/base.py

# fluidsynth + General MIDI soundfont for runtime rendering of dance/song MIDI
# files to Opus. Installed as the LAST layer so iteration on Python deps above
# doesn't invalidate the soundfont download (~141MB).
RUN apt-get update \
    && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm \
    && rm -rf /var/lib/apt/lists/*
