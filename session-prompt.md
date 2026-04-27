# Claude Code Session Prompt — StackChan Infrastructure Setup

> Historical bootstrap prompt. Kept for reference — describes how this
> infra was originally stood up across two remote machines. Not all of it
> matches the current implementation (see `README.md` and `SETUP.md` for
> current truth), and the firmware-provisioning steps in particular now
> require the build-from-source flow in `SETUP.md`.

Paste this into your terminal:

```
claude --prompt-file ./session-prompt.md
```

---

## Prompt content (save as `session-prompt.md`):

I need you to set up infrastructure across two remote machines for an M5Stack StackChan robot. You'll be SSHing from this workstation to both targets via Tailscale. Read the CLAUDE.md in this directory first for full architecture context.

## What you're building

A self-hosted voice pipeline that routes StackChan's audio through a xiaozhi-esp32-server (ASR + TTS) running on a Linux Docker host, with all AI processing forwarded to a ZeroClaw instance running on a separate host.

## Discovery steps (do these first)

1. Run `tailscale status` (if you use Tailscale) to find the hostnames and IPs for both the Docker host and the ZeroClaw host. Identify which is which from the OS/hostname.
2. SSH into the Docker host. Find its LAN IP (not Tailscale IP) — StackChan will need this because it's on WiFi, not Tailnet. Check `ip addr` or `hostname -I`. Also confirm Docker is available and pick a directory for the xiaozhi-server install (e.g. `/opt/xiaozhi-server/` or `/srv/xiaozhi-server/`).
3. SSH into the ZeroClaw host. Find its LAN IP similarly. Confirm ZeroClaw is running — check `zeroclaw status` or look for the gateway process on port 18789. Note the exact port and any API endpoints it exposes. Also check what Python version is available and whether pip/fastapi are already installed.
4. Test basic connectivity: from the Docker host, can you reach the ZeroClaw host's LAN IP? You may need to test this from inside a throwaway container (`docker run --rm alpine ping ZEROCLAW_LAN_IP`).

## Docker host setup (xiaozhi-esp32-server)

On the Docker host:

1. Create the directory structure at your chosen install path (e.g. `/opt/xiaozhi-server/`) with subdirs: `data/`, `models/SenseVoiceSmall/`, `tmp/`.
2. Clone `https://github.com/xinnan-tech/xiaozhi-esp32-server.git` into a `repo/` subdir.
3. Download the SenseVoiceSmall ASR model (`model.pt`, ~250MB) into `models/SenseVoiceSmall/`. Try ModelScope first: `https://www.modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt`. If that's slow, use HuggingFace: `https://huggingface.co/FunAudioLLM/SenseVoiceSmall/resolve/main/model.pt`. Verify the file is >200MB after download.
4. Create the custom ZeroClaw LLM provider at `repo/main/xiaozhi-server/core/providers/llm/zeroclaw/zeroclaw.py` plus `__init__.py`. The provider:
   - Extends `LLMProviderBase` from `core.providers.llm.base`
   - Sends HTTP POST to the ZeroClaw bridge on the ZeroClaw host
   - Passes the user's transcribed text plus a system prompt that enforces emoji-first responses for StackChan face animations
   - Handles connection errors gracefully with emoji-prefixed fallback messages
   - Implements both `response()` and `response_stream()` (stream can just yield the non-stream result for now)
   - Returns `False` from `function_call_supported()` — ZeroClaw handles its own tools
5. Create `data/.config.yaml` with:
   - `selected_module.ASR: FunASRLocal`
   - `selected_module.LLM: ZeroClawLLM`
   - `selected_module.TTS: EdgeTTS`
   - `selected_module.VAD: SileroVAD`
   - EdgeTTS voice: `en-AU-WilliamNeural`
   - ZeroClaw URL pointing to the ZeroClaw host's LAN IP, port 8080
   - A system prompt that identifies as a desktop robot assistant. Enforce emoji-first responses. Keep TTS-friendly (short sentences).
   - VAD silence duration 700ms (so it doesn't cut off slow speakers)
   - Use the actual LAN IPs you discovered, not placeholders.
6. Create `docker-compose.yml` that:
   - Uses `ghcr.io/xinnan-tech/xiaozhi-esp32-server:server_latest`
   - Exposes ports 8000 (WebSocket) and 8003 (OTA/HTTP)
   - Mounts `data/.config.yaml`, `models/`, `tmp/`, and the custom ZeroClaw provider directory
   - Sets TZ to Australia/Brisbane
   - Pip installs `aiohttp` on startup (the base image may not have it)
   - **Important**: Check the actual container's internal directory structure first before writing the volume mounts. Run `docker run --rm ghcr.io/xinnan-tech/xiaozhi-esp32-server:server_latest ls /opt/xiaozhi-server/` (or wherever the app lives) to find the correct internal paths. The mount targets must match where the app actually loads providers from.
7. Start the container, tail the logs, and confirm you see the WebSocket and OTA addresses in the output.

## ZeroClaw host setup (ZeroClaw HTTP bridge)

On the ZeroClaw host:

1. First, understand how ZeroClaw actually accepts messages. Check the running config, look at the gateway's API, examine any webchat or REST endpoints. The bridge needs to translate a simple HTTP POST into whatever ZeroClaw actually expects. Don't assume the API shape — discover it.
2. Create `~/zeroclaw-bridge/bridge.py` — a FastAPI app that:
   - Listens on 0.0.0.0:8080
   - Accepts POST `/api/message` with `{"content": "...", "channel": "stackchan", "session_id": "...", "metadata": {...}}`
   - Forwards to ZeroClaw's actual API/gateway
   - Returns `{"response": "emoji-prefixed text"}`
   - Has a GET `/health` endpoint
3. Install deps (fastapi, uvicorn, whatever HTTP client is needed).
4. Create a systemd user service for it so it persists across reboots.
5. Start it and verify the health endpoint responds.

## Testing

After both sides are up:

1. Curl the bridge health endpoint from the Docker host (from inside a Docker container to simulate the xiaozhi-server's network perspective).
2. Send a test message through the bridge and confirm you get an emoji-prefixed response back.
3. Check xiaozhi-server logs to confirm the WebSocket endpoint is listening and the OTA endpoint reports healthy.
4. If the repo includes a test HTML page (usually at `repo/main/xiaozhi-server/test/test_page.html`), note its location so I can open it in a browser for audio testing.

## Final output

When everything is confirmed working, print a clear summary:

```
=== STACKCHAN SETUP COMPLETE ===

OTA URL (enter this in StackChan's Advanced Settings):
  http://X.X.X.X:8003/xiaozhi/ota/

WebSocket endpoint:
  ws://X.X.X.X:8000/xiaozhi/v1/

ZeroClaw bridge:
  http://X.X.X.X:8080/api/message

Test page for browser audio testing:
  file:///path/to/test_page.html
  (point it at the WebSocket endpoint above)

When StackChan arrives:
  1. Flash open firmware built from https://github.com/m5stack/StackChan
     (see SETUP.md for the current build/flash flow — stock firmware
     ships with BLE+cloud provisioning, not the SoftAP flow that some
     older guides describe).
  2. First boot: device POSTs to the OTA URL, gets redirected to the
     WebSocket endpoint, and connects automatically.
```

## Important constraints

- Use `micro` if you need to interactively edit files (not nano, not vim).
- Don't install anything on the local workstation — everything happens via SSH to the remote machines.
- All IPs in config files must be real LAN IPs discovered at runtime, not Tailscale IPs (StackChan isn't on the Tailnet).
- If any step fails, diagnose from the logs before retrying. Don't just re-run blindly.
- The xiaozhi-esp32-server Docker image's internal directory structure may differ from the repo layout. Inspect the container before writing volume mounts.
