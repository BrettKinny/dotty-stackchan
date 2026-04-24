# zeroclaw-bridge — Raspberry Pi deployment

The bridge is a FastAPI service that owns a long-running `zeroclaw acp`
child process via stdio JSON-RPC. It accepts `POST /api/message` from
xiaozhi-server and returns an emoji-prefixed reply suitable for the
StackChan's face-animation protocol. See [`../README.md`](../README.md) for
how it fits in the full stack.

Two deployment options. Pick one; both read the same persona files under
`~/.zeroclaw/`, so you can switch between them without losing state.

- **Option A — Docker.** Pull a pre-built multi-arch image from GHCR. No
  Rust toolchain, no Python venv, no hand-edited systemd unit. Recommended.
- **Option B — Bare metal.** Install Rust, `cargo install zeroclaw`, set up
  a venv, install `zeroclaw-bridge.service`. Kept for anyone who doesn't
  want Docker on the Pi.

---

## Option A: Docker (recommended)

### Prerequisites

- Raspberry Pi 4 (or any Linux host) on 64-bit OS. DietPi 64-bit image
  reports `aarch64` from `uname -m`. The 32-bit armv7 image is **not
  supported** by the published multi-arch build.
- Docker + compose plugin. On DietPi: `sudo dietpi-software install docker`.
- A populated `~/.zeroclaw/` with your persona (SOUL.md, IDENTITY.md,
  MEMORY.md, AGENTS.md, optional USER.md) and `config.toml` with your
  LLM provider + encrypted api_key. Use `zeroclaw init` on any machine
  with the binary, then copy the directory to `/root/.zeroclaw/` on the
  Pi. The compose file bind-mounts this directory into the container.

### First run

```bash
# on the Pi, as root (bind mount assumes /root/.zeroclaw is root-owned)
mkdir -p /root/zeroclaw-bridge && cd /root/zeroclaw-bridge
curl -L -o compose.yml \
  https://raw.githubusercontent.com/BrettKinny/stackchan-infra/main/bridge/compose.bridge.yml
docker compose pull
docker compose up -d
```

Verify:

```bash
curl http://127.0.0.1:8080/health
# {"status":"ok","service":"zeroclaw-bridge","acp_running":true,...}
```

Tail logs:

```bash
docker compose logs -f
```

### Running `zeroclaw` management subcommands

The gateway pairing flow (used for tweaking persona via the web UI over an
SSH tunnel) still works — just invoke the binary inside the container:

```bash
docker exec -it zeroclaw-bridge zeroclaw gateway get-paircode
```

Optional convenience: drop a shim at `/usr/local/bin/zeroclaw` on the host
so muscle memory keeps working:

```bash
cat >/usr/local/bin/zeroclaw <<'EOF'
#!/bin/sh
exec docker exec -it zeroclaw-bridge zeroclaw "$@"
EOF
chmod +x /usr/local/bin/zeroclaw
```

### Upgrading

```bash
cd /root/zeroclaw-bridge
docker compose pull
docker compose up -d
```

Roll back by pinning `image:` to a previous tag in `compose.yml` and
repeating. CI publishes `bridge-vX.Y.Z` tags in addition to `latest`.

### Pinning the zeroclaw crate version

The image's built-in zeroclaw binary is whatever was the latest on
crates.io at CI build time. To pin a specific crate version, rebuild
locally:

```bash
git clone https://github.com/BrettKinny/stackchan-infra.git
cd stackchan-infra
docker build -f bridge/Dockerfile --build-arg ZEROCLAW_VERSION=0.4.2 -t zeroclaw-bridge:local .
```

Then reference `zeroclaw-bridge:local` in `compose.yml`.

### Coexisting with the systemd unit

The image listens on port 8080 just like the bare-metal service — only one
can run at a time. If you've been running Option B, stop it before
starting Option A:

```bash
sudo systemctl disable --now zeroclaw-bridge
```

State under `/root/.zeroclaw/` is preserved; both paths share it.

---

## Option B: Bare metal (systemd)

Retained for hosts without Docker. Same state directory as Option A, so
switching paths is a `systemctl start` / `docker compose up` toggle.

### Install

```bash
# Rust toolchain (takes a while on an RPi)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source $HOME/.cargo/env
cargo install zeroclaw   # ~30 minutes on a Pi 4

# Bridge code + venv
mkdir -p /root/zeroclaw-bridge && cd /root/zeroclaw-bridge
curl -L -o bridge.py \
  https://raw.githubusercontent.com/BrettKinny/stackchan-infra/main/bridge.py
python3 -m venv .venv
.venv/bin/pip install fastapi 'uvicorn[standard]' pydantic

# systemd unit
curl -L -o /etc/systemd/system/zeroclaw-bridge.service \
  https://raw.githubusercontent.com/BrettKinny/stackchan-infra/main/zeroclaw-bridge.service
systemctl daemon-reload
systemctl enable --now zeroclaw-bridge
```

Paths baked into the unit assume the above layout (`/root/zeroclaw-bridge/`
and `/root/.cargo/bin/zeroclaw`). Edit
`/etc/systemd/system/zeroclaw-bridge.service` if yours differ.

### Verify

```bash
curl http://127.0.0.1:8080/health
journalctl -u zeroclaw-bridge -f
```

### Upgrading the agent binary

```bash
cargo install zeroclaw --force
systemctl restart zeroclaw-bridge
```

---

## What lives where

```
/root/.zeroclaw/               # persona + agent state (both options share this)
├── config.toml                # LLM provider + encrypted api_key
├── workspace/
│   ├── SOUL.md                # core identity + operating principles
│   ├── IDENTITY.md            # personality, name, backstory
│   ├── MEMORY.md              # long-term facts
│   ├── AGENTS.md              # behavioral guardrails
│   └── USER.md                # optional per-user context
├── memory/brain.db            # SQLite; auto-created if missing
└── state/                     # cost + trace logs

/root/zeroclaw-bridge/         # Option A: just compose.yml
                               # Option B: bridge.py + .venv/
```

The bind mount in Option A keeps the container-internal path identical to
the host path so any absolute path references inside `config.toml` keep
working.
