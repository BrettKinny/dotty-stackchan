# Pinned 2026-04-25 from :server_latest
FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server@sha256:2f18537ac753884b5afb8e18d4e1a80926f795a5b5128cd81107028f6edb8120

RUN pip install --no-cache-dir piper-tts scipy numpy
