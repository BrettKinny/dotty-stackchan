FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server:server_latest

RUN pip install --no-cache-dir piper-tts scipy numpy
