---
title: Quickstart
description: From zero to first voice turn in 15 minutes.
---

# Quickstart

Get Dotty talking in 15 minutes. This is the single opinionated happy
path -- see [SETUP.md](SETUP.md) for build-from-source and
alternative configurations.

## What you need

| Item | Notes |
|------|-------|
| **M5Stack CoreS3 + StackChan servo kit** | The robot. See [hardware-support.md](hardware-support.md) for details. |
| **Linux or macOS host with Docker** | Runs all four server-side containers. Any distro works. |
| **2.4 GHz WiFi** | The ESP32-S3 does not support 5 GHz. |

## 1. Flash the firmware

Download the latest release from
[GitHub Releases](https://github.com/BrettKinny/dotty-stackchan/releases)
(look for a tag starting with `fw-v`). Grab all six binaries:
`bootloader.bin`, `partition-table.bin`, `ota_data_initial.bin`,
`stack-chan.bin`, `generated_assets.bin`, and `human_face_detect.espdl`.

Install esptool and flash over USB-C:

```bash
pip install esptool

python -m esptool --chip esp32s3 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
  0x0      bootloader.bin \
  0x8000   partition-table.bin \
  0xd000   ota_data_initial.bin \
  0x20000  stack-chan.bin \
  0xa60000 generated_assets.bin \
  0xe70000 human_face_detect.espdl
```

Flashing the bootloader (`0x0`) and partition table (`0x8000`) is
**required** — skip them and the device keeps whatever partition layout
the previous firmware left behind. That layout won't match these
images, and the robot boot-loops on `No bootable app partitions`.

Verify checksums against `SHA256SUMS.txt` in the release if desired.

## 2. Clone the repo

```bash
git clone --recursive https://github.com/BrettKinny/dotty-stackchan.git
cd dotty-stackchan
```

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env` and set `OPENROUTER_API_KEY=<YOUR_API_KEY>` (or any
OpenAI-compatible key). You can skip this if you're running fully local
— either via Ollama (single binary, simple) or via llama-swap (Docker,
supports multiple resident models). See
[cookbook/run-fully-local.md](cookbook/run-fully-local.md) and
[cookbook/llama-swap-concurrent-models.md](cookbook/llama-swap-concurrent-models.md).

The shipped `.config.yaml` selects `PiVoiceLLM` as the default LLM
provider, which runs the `dotty-pi` container (the pi coding agent)
on the same Docker host. One alternate provider — `OpenAICompat`
(any OpenAI-compatible cloud or local endpoint) — is available via
`selected_module.LLM` in `data/.config.yaml`.

## 4. Run setup

```bash
make setup
```

The interactive wizard prompts for your server IP, robot name, timezone,
and LLM provider. It downloads the ASR and TTS models (~100 MB),
substitutes placeholders in config files, and starts the Docker
containers.

Verify everything is healthy:

```bash
make doctor
```

All checks should pass (green). If any fail, see
[troubleshooting.md](troubleshooting.md).

## 5. Bring up the containers

All four server-side services run as Docker containers on the same host.
`docker compose up -d` (from `docker-compose.yml.template` after `make
setup` substitutes your placeholders) starts the main `xiaozhi-esp32-server`
container. The brain container and the perception/dashboard container
are brought up separately:

- **dotty-pi** (the voice-tool brain): see [dotty-pi/README.md](../dotty-pi/README.md)
  for build and run instructions.
- **dotty-behaviour** (perception bus + admin dashboard): see
  [dotty-behaviour/README.md](../dotty-behaviour/README.md) for build and
  run instructions. The `scripts/deploy-behaviour.sh` helper deploys it.
- **bridge.py** (admin dashboard service, `:8081`): runs as a container
  on the same host (`bridge/Dockerfile` + `bridge/docker-compose.yml`,
  deployed via `scripts/deploy-bridge-unraid.sh`).

No separate host, no systemd bridge unit, no SSH to a second machine.

## 6. Connect the robot

1. Power on the robot (USB-C or battery).
2. On the device screen, navigate to **Settings > Advanced Options**.
3. Enter the OTA URL: `http://<XIAOZHI_HOST>:8003/xiaozhi/ota/`
4. The robot connects via WebSocket and shows a face.

## 7. First voice turn

Tap the screen to enter voice mode and say "Hello Dotty!"

You should see:

| LED colour | State |
|------------|-------|
| Green | Listening -- you are speaking |
| Orange | Thinking -- waiting for LLM response |
| Blue | Talking -- playing the response |

The face expression changes to match the response emoji. First-turn
latency is roughly 5 seconds, dominated by the LLM round-trip.

## Next steps

- [Change the persona](cookbook/change-persona.md) -- give Dotty a different personality.
- [Swap the voice](cookbook/swap-voice.md) -- try a different TTS voice.
- [Run fully local](cookbook/run-fully-local.md) -- Ollama compose profile, zero cloud dependencies.
- [Run two local models concurrently](cookbook/llama-swap-concurrent-models.md) -- keep a small voice model and a big "think" model both resident via llama-swap's matrix DSL.
- [Disable Kid Mode](cookbook/disable-kid-mode.md) -- for unrestricted use.
- [Architecture overview](architecture.md) -- full data flow.
- [Kid Mode](kid-mode.md) -- on by default, what it enforces.

---

## Placeholders

This repo uses placeholders in place of real IPs, usernames, and filesystem paths. Substitute these everywhere before deploying:

| Placeholder | Meaning |
|---|---|
| `<XIAOZHI_HOST>` | LAN IP of the server running all Docker containers. The robot reaches this on WiFi, so it must be a LAN IP, not a Tailscale/VPN IP. |
| `<XIAOZHI_USER>` | SSH user for the server (whatever your distro defaults to: `root`, `ubuntu`, `dietpi`, etc.). |
| `<XIAOZHI_HOSTNAME>` | Hostname or Tailscale name of the server (optional, IP works for everything). |
| `<XIAOZHI_PATH>` | Path on the server where you clone/install this repo (e.g. `/opt/xiaozhi-server/` or `/srv/xiaozhi-server/`). |
| `<YOUR_NAME>` | Your name / org, used in the persona prompt in `.config.yaml`. |
| `<ROBOT_NAME>` | Name the robot introduces itself as, referenced in the persona prompt in `.config.yaml`. Any string — pick whatever you want. The default example uses the hardware name ("StackChan"). |

Port numbers (`8000`, `8003`, `8081`, `8090`) are product-generic and should not be changed unless you also reconfigure the respective services.

Files you will definitely need to edit before first run:

- `.config.yaml` — replace `<XIAOZHI_HOST>` and customise the `prompt:` block.
- `docker-compose.yml` — set `TZ` to your timezone.

---

## Deployment layout

All four containers run on the single Docker host (`<XIAOZHI_HOST>`):

| Container | Purpose | Port |
|---|---|---|
| `xiaozhi-esp32-server` | Voice pipeline: ASR, TTS, WebSocket to StackChan | 8000 (WS), 8003 (OTA/HTTP) |
| `dotty-pi` | pi coding agent — the voice-tool brain | internal (via `docker exec`) |
| `dotty-behaviour` | Perception bus + ambient consumers + calendar | 8090 |
| `bridge.py` | Admin dashboard | 8081 |

Container volume mounts for `xiaozhi-esp32-server`:

| Host path | Container path | Purpose |
|---|---|---|
| `data/.config.yaml` | `/opt/xiaozhi-esp32-server/data/.config.yaml` | Config override (read-only mount) |
| `models/SenseVoiceSmall/` | `/opt/xiaozhi-esp32-server/models/SenseVoiceSmall/` | ASR weights |
| `models/piper/` | `/opt/xiaozhi-esp32-server/models/piper/` | Piper TTS voice models (`.onnx` + `.json`) |
| `tmp/` | `/opt/xiaozhi-esp32-server/tmp/` | Scratch |
| `custom-providers/pi_voice/` | `/opt/xiaozhi-esp32-server/core/providers/llm/pi_voice/` | PiVoiceLLM provider (directory mount) |
| `custom-providers/openai_compat/` | `/opt/xiaozhi-esp32-server/core/providers/llm/openai_compat/` | OpenAICompat alternate provider |
| `custom-providers/edge_stream/edge_stream.py` | `/opt/xiaozhi-esp32-server/core/providers/tts/edge_stream.py` | Streaming EdgeTTS provider (file mount) |
| `custom-providers/piper_local/piper_local.py` | `/opt/xiaozhi-esp32-server/core/providers/tts/piper_local.py` | Local Piper TTS provider (file mount) |
| `custom-providers/asr/fun_local.py` | `/opt/xiaozhi-esp32-server/core/providers/asr/fun_local.py` | Patched FunASR — adds `language` config key so SenseVoiceSmall can be pinned to English |

The full file inventory lives in [architecture.md](./architecture.md#deployment-files-this-repo).

---

## Endpoints

| What | URL | Who calls it |
|---|---|---|
| OTA (enter into StackChan settings) | `http://<XIAOZHI_HOST>:8003/xiaozhi/ota/` | The robot on boot |
| WebSocket | `ws://<XIAOZHI_HOST>:8000/xiaozhi/v1/` | The robot after OTA handshake |
| Perception / ambient events | `http://<XIAOZHI_HOST>:8090` | xiaozhi-server → dotty-behaviour |
| Admin dashboard | `http://<XIAOZHI_HOST>:8081/ui` | Humans (LAN-only HTMX UI) |
| Bridge health | `http://<XIAOZHI_HOST>:8081/health` | Humans, monitoring |

---

## Reboot survival

All containers use `restart: unless-stopped`. Ensure dockerd starts at
boot on your distro. Use `docker compose restart` or
`docker restart <container>` for transient restarts rather than `docker
compose down` (which marks the container stopped and prevents
auto-restart on reboot).

---

## Common operations

```bash
# Tail xiaozhi-server logs (voice pipeline)
ssh <XIAOZHI_USER>@<XIAOZHI_HOST> 'docker logs -f xiaozhi-esp32-server'

# Tail dotty-behaviour logs (perception + dashboard)
ssh <XIAOZHI_USER>@<XIAOZHI_HOST> 'docker logs -f dotty-behaviour'

# Tail dotty-pi logs (brain container)
ssh <XIAOZHI_USER>@<XIAOZHI_HOST> 'docker logs -f dotty-pi'

# Restart voice pipeline after config change
ssh <XIAOZHI_USER>@<XIAOZHI_HOST> 'cd <XIAOZHI_PATH> && docker compose restart'

# Admin dashboard
open http://<XIAOZHI_HOST>:8081/ui

# Bridge health
curl http://<XIAOZHI_HOST>:8081/health
```

### Changing voice
The default TTS is `LocalPiper` (offline, runs inside the container). To change the Piper voice, edit `TTS.LocalPiper.voice` and the corresponding `model_path` / `config_path` in `data/.config.yaml`. To switch to cloud EdgeTTS instead, set `selected_module.TTS: EdgeTTS` and edit `TTS.EdgeTTS.voice` (any Microsoft Edge Neural voice ID works, e.g. `en-US-AvaNeural`). Restart the container after changes.

### Changing persona (the robot's personality)
Edit `personas/dotty_voice.md` (loaded by the pi agent on the `PiVoiceLLM` path) and restart the relevant container. The `prompt:` key in `data/.config.yaml` is also injected as a secondary system message. Full instructions: [cookbook/change-persona.md](cookbook/change-persona.md).

### Changing VAD sensitivity
`VAD.SileroVAD.min_silence_duration_ms` in `data/.config.yaml`. Default: 700 ms. Lower = cuts off quicker. Higher = waits longer for slow speakers.

### Changing the LLM model
For the `PiVoiceLLM` path (default): see [dotty-pi/README.md](../dotty-pi/README.md) for the model selection rules — in particular, the llama-swap matrix DSL constraint that prevents the voice-model set from being evicted. For the `OpenAICompat` path: edit `LLM.OpenAICompat.model` (or repoint `url` / `api_key`) in `data/.config.yaml` and `docker compose restart`. Note: there is no live in-flight model-swap on either path — smart-mode model-swap is v2 scope and not wired (the instant hot-swap once provided by the removed Tier1Slim provider is gone).

---

## Troubleshooting

```bash
make doctor          # health checks
make logs            # tail server logs
curl http://<XIAOZHI_HOST>:8081/health   # test the bridge/dashboard
```

See [troubleshooting.md](troubleshooting.md) for common issues.
