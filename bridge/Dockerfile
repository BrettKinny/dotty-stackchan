# dotty-bridge — admin dashboard service (FastAPI, served at /ui).
#
# Post-#36 the bridge.py module survived as the dashboard; its voice +
# perception-bus roles moved to dotty-pi / dotty-behaviour. This image
# is the Unraid-resident deploy home for that dashboard, alongside
# dotty-behaviour and dotty-pi.
#
# Layout reminder: bridge.py lives at the repo ROOT (legacy from the
# zeroclaw-bridge era), while bridge/ holds supporting modules
# (dashboard.py, csrf.py, metrics.py, speaker.py, templates/, static/,
# requirements.txt, …). bridge.py also imports textUtils from
# custom-providers/ via a sys.path shim, so that tree ships in the image
# too.
#
# Pinned base — bump deliberately, not via floating tags.
FROM python:3.12-slim-bookworm

WORKDIR /app

# tini for proper signal handling under docker compose restart.
# curl for the in-container healthcheck (GET /health).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/*

# Install deps first so iterative source edits don't bust the pip layer.
COPY bridge/requirements.txt ./bridge/requirements.txt
RUN pip install --no-cache-dir -r bridge/requirements.txt

# Source. bridge.py at /app/bridge.py (its `__file__`-relative resolution
# of bridge/static/, bridge/templates/, and custom-providers/ then lines
# up with the in-container layout).
COPY bridge.py ./bridge.py
COPY bridge ./bridge
COPY custom-providers ./custom-providers

# Stamp the deployed git SHA so the dashboard version chip reads it
# (the image has no .git checkout, so dashboard.py's git fallback can't
# resolve a version). deploy-bridge-unraid.sh passes the HEAD short SHA.
ARG BRIDGE_VERSION=unknown
RUN printf '%s' "$BRIDGE_VERSION" > /app/.bridge-version

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8081

EXPOSE 8081

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "bridge.py"]
