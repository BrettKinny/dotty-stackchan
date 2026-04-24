# Multi-host deployment (Unraid + Raspberry Pi)

The default setup in `compose.all-in-one.yml` runs everything on one Docker host. This document describes the **multi-host** split: xiaozhi-server on an Unraid (or other Docker host) and ZeroClaw + the bridge on a separate Raspberry Pi.

## When you'd want this

- **Dedicated hardware for the brain.** ZeroClaw runs as a native binary (Rust, not containerized). If you already have a Pi running ZeroClaw for other channels (chat, CLI, other agents), keeping the bridge there avoids duplicating the install.
- **Resource isolation.** The voice pipeline (ASR model loading, TTS) and the LLM bridge have different resource profiles. Splitting them across hosts avoids contention.
- **Existing Unraid setup.** If Unraid is already your Docker host and you don't want to install Rust/ZeroClaw tooling on it, the Pi is a natural home for the bridge.

## How it differs from all-in-one

| Aspect | All-in-one | Multi-host |
|---|---|---|
| Compose file | `compose.all-in-one.yml` | `docker-compose.yml` (xiaozhi only) |
| Bridge runs as | Docker container | systemd service on the Pi |
| LLM URL in `.config.yaml` | `http://bridge:8080/api/message/stream` (Docker network) | `http://<RPI_IP>:8080/api/message/stream` (real LAN IP) |
| ZeroClaw install | On the Docker host, bind-mounted into the bridge container | Native on the Pi (`cargo install zeroclaw`) |
| Network | Docker bridge network between services | LAN — xiaozhi-server reaches the Pi over WiFi/Ethernet |

## Setup

The main [README](../../README.md) already documents this layout in detail. The short version:

1. **Unraid (Docker host):**
   - Clone this repo to `<UNRAID_XIAOZHI_PATH>`.
   - Edit `data/.config.yaml`: set `LLM.ZeroClawLLM.url` to `http://<RPI_IP>:8080/api/message/stream`.
   - Run `docker compose up -d` (uses the standard `docker-compose.yml`).

2. **Raspberry Pi:**
   - Install ZeroClaw: `cargo install zeroclaw` (see [zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw)).
   - Configure the agent: edit `~/.zeroclaw/config.toml` with your LLM provider, API keys, and persona.
   - Copy `bridge.py` and `bridge/requirements.txt` to `<RPI_BRIDGE_PATH>`.
   - Create a venv and install deps: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
   - Install the systemd unit: copy `zeroclaw-bridge.service` to `/etc/systemd/system/`, edit paths, then `systemctl enable --now zeroclaw-bridge`.

3. **StackChan device:**
   - Set OTA URL to `http://<UNRAID_IP>:8003/xiaozhi/ota/`.

## Migrating from all-in-one to multi-host

1. Stop the all-in-one stack: `docker compose -f compose.all-in-one.yml down`.
2. Edit `data/.config.yaml`: change the LLM URL from `http://bridge:8080/...` to `http://<RPI_IP>:8080/...`.
3. Set up the Pi as described above.
4. Start xiaozhi-server alone: `docker compose up -d` (standard `docker-compose.yml`).

## Reference

- Full architecture diagram: [README.md](../../README.md#high-level-architecture)
- Endpoint table: [README.md](../../README.md#endpoints)
- Bridge internals: [docs/protocols.md](../protocols.md) (ACP JSON-RPC section)
- Troubleshooting: [README.md](../../README.md#troubleshooting)
