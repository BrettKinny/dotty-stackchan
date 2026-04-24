# Multi-stage image for zeroclaw-bridge.
#
# Stage 1 builds the zeroclaw Rust binary from crates.io. Stage 2 is a
# slim Python runtime with FastAPI + uvicorn and bridge.py. The bridge
# owns the `zeroclaw acp` child via stdio JSON-RPC, so both must ship
# in the same image — splitting them buys nothing.
#
# Build context: the repo root (one level up from this Dockerfile), so
# bridge.py at the repo root is reachable via COPY.

# ---------- Stage 1: build zeroclaw ----------
FROM rust:1-slim AS build

ARG ZEROCLAW_VERSION
# If ZEROCLAW_VERSION is unset, cargo installs the latest published version.

RUN apt-get update \
 && apt-get install -y --no-install-recommends pkg-config libssl-dev \
 && rm -rf /var/lib/apt/lists/*

RUN if [ -n "$ZEROCLAW_VERSION" ]; then \
      cargo install zeroclaw --version "$ZEROCLAW_VERSION" --root /out --locked ; \
    else \
      cargo install zeroclaw --root /out --locked ; \
    fi

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.32.*" pydantic==2.*

COPY --from=build /out/bin/zeroclaw /usr/local/bin/zeroclaw
COPY bridge.py /app/bridge.py

ENV ZEROCLAW_BIN=/usr/local/bin/zeroclaw \
    PORT=8080 \
    PYTHONUNBUFFERED=1

WORKDIR /app
EXPOSE 8080

# Run as root: the bind-mounted /root/.zeroclaw/ on the host is root-owned
# and the Pi is single-tenant. Dropping privileges adds UID-drift risk for
# zero security gain in this deployment shape.
CMD ["python", "bridge.py"]
