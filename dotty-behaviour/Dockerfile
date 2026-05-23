# dotty-behaviour — the behaviour daemon for Dotty (Unraid-resident
# successor to the RPi-hosted zeroclaw-bridge).
#
# Hosts the perception event bus, 9 consumers, vision/audio explain
# endpoints, dashboard, proactive greeter, and per-device caches.
# Replaces bridge.py + bridge/* per the dotty-behaviour rehoming track
# of #36.
#
# Pinned base — bump deliberately, not via floating tags.
FROM python:3.12-slim-bookworm

WORKDIR /app

# tini for proper signal handling under docker compose restart.
# curl for the in-container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Flat layout — top-level modules + subpackages, no outer Python package.
COPY config.py main.py ./
COPY perception ./perception
COPY routes ./routes
COPY dispatch ./dispatch
COPY vision ./vision
COPY consumers ./consumers
COPY greeter ./greeter
COPY household ./household
COPY logs ./logs
COPY calendar_ ./calendar_

# Static assets shipped with the image (purr.opus etc). Lives outside /app
# because PURR_AUDIO_PATH defaults to /var/lib/dotty-behaviour/assets/purr.opus
# — same parent as the state/logs/secrets bind mounts but not bind-mounted itself.
COPY assets /var/lib/dotty-behaviour/assets

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOTTY_BEHAVIOUR_HOST=0.0.0.0 \
    DOTTY_BEHAVIOUR_PORT=8090

EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8090/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8090", "--log-level", "info"]
