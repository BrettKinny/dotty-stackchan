---
title: Quickstart
description: From zero to first voice turn in 15 minutes.
---

# Quickstart

Get Dotty talking in 15 minutes. This is the single opinionated happy
path -- see [SETUP.md](../SETUP.md) for build-from-source and
alternative configurations.

## What you need

| Item | Notes |
|------|-------|
| **M5Stack CoreS3 + StackChan servo kit** | The robot. See [hardware-support.md](hardware-support.md) for details. |
| **Linux or macOS host with Docker** | Runs the voice pipeline. Any distro works. |
| **2.4 GHz WiFi** | The ESP32-S3 does not support 5 GHz. |

## 1. Flash the firmware

Download the latest release from
[GitHub Releases](https://github.com/BrettKinny/dotty-stackchan/releases)
(look for a tag starting with `fw-v`). You need three files:
`stack-chan.bin`, `ota_data_initial.bin`, and `generated_assets.bin`.

Install esptool and flash over USB-C:

```bash
pip install esptool

python -m esptool --chip esp32s3 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
  0xd000 ota_data_initial.bin \
  0x20000 stack-chan.bin \
  0x610000 generated_assets.bin
```

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
OpenAI-compatible key). Skip this if running fully local via Ollama.

## 4. Run setup

```bash
make setup
```

The interactive wizard prompts for your server IP, robot name, timezone,
and LLM provider. It downloads the ASR and TTS models (~100 MB),
substitutes placeholders in config files, and starts the Docker
container.

Verify everything is healthy:

```bash
make doctor
```

All checks should pass (green). If any fail, see
[troubleshooting.md](troubleshooting.md).

## 5. Connect the robot

1. Power on the StackChan (USB-C or battery).
2. On the device screen, navigate to **Settings > Advanced Options**.
3. Enter the OTA URL: `http://<YOUR_SERVER_IP>:8003/xiaozhi/ota/`
4. The robot connects via WebSocket and shows a face.

## 6. First voice turn

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
- [Disable Kid Mode](cookbook/disable-kid-mode.md) -- for adult-only use.
- [Architecture overview](architecture.md) -- full data flow.
- [Kid Mode](kid-mode.md) -- on by default, what it enforces.

## Troubleshooting

```bash
make doctor          # health checks
make logs            # tail server logs
curl http://<YOUR_SERVER_IP>:8080/health   # test the bridge
```

See [troubleshooting.md](troubleshooting.md) for common issues.
