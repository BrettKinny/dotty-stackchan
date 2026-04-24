---
title: Quickstart
description: From zero to first voice turn in 15 minutes.
---

# Quickstart

Get your StackChan talking in 15 minutes. This is the single opinionated
happy path — see [SETUP.md](../SETUP.md) for alternatives and details.

## 1. What you need

- **M5Stack CoreS3 + StackChan servo kit** (the robot)
- **A Linux box with Docker** (any distro — Ubuntu, Arch, Unraid, etc.)
- **2.4 GHz WiFi** (the ESP32-S3 doesn't support 5 GHz)

## 2. Flash the firmware

Download the latest binaries from
[GitHub Releases](https://github.com/BrettKinny/stackchan-infra/releases)
(look for a tag starting with `fw-v`).

Install esptool and flash:

```bash
pip install esptool

python -m esptool --chip esp32s3 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
  0x0 bootloader.bin \
  0x8000 partition-table.bin \
  0xd000 ota_data_initial.bin \
  0x20000 stack-chan.bin
```

Verify checksums against `checksums.txt` in the release if desired.

> **Building from source?** See [SETUP.md](../SETUP.md) for the full
> IDF build procedure.

## 3. Clone and set up the server

```bash
git clone --recursive https://github.com/BrettKinny/stackchan-infra.git
cd stackchan-infra
make setup
```

The interactive wizard prompts for your server IP, robot name, timezone,
and LLM provider. It downloads the ASR and TTS models, substitutes
placeholders, and starts the Docker container.

Verify everything is healthy:

```bash
make doctor
```

All checks should pass (green). If any fail, see
[Troubleshooting](troubleshooting.md).

## 4. Configure the robot

1. Power on the StackChan (USB-C or battery).
2. On the device screen, navigate to **Settings → Advanced Options**.
3. Enter the OTA URL: `http://<your-server-ip>:8003/xiaozhi/ota/`
4. The robot connects via WebSocket and shows a face.
5. **Tap the screen** to enter voice mode.

> The touchscreen can be intermittent — you may need a few taps.

## 5. Talk to it

Say hello. You should see:

| LED colour | State |
|-----------|-------|
| 🟢 Green | Listening — you're speaking |
| 🟠 Orange | Thinking — waiting for LLM response (~5s) |
| 🔵 Blue | Talking — playing the response |

The face expression changes to match the response emoji (smile, laugh,
thinking, etc.). First-turn latency is ~5 seconds, dominated by the LLM
round-trip.

## 6. Next steps

- **Change the voice:** Edit `.config.yaml` — see
  [README → Changing voice](../README.md#changing-voice)
- **Change the personality:** Edit the persona file or system prompt —
  see [README → Changing persona](../README.md#changing-persona-the-robots-personality)
- **Run fully local:** See the all-in-one compose profile
  (`compose.all-in-one.yml`) and the
  [fully-local backend](../README.md) section
- **Understand the architecture:** [docs/architecture.md](architecture.md)
- **Check safety guardrails:** [docs/child-safety.md](child-safety.md)

### Troubleshooting

```bash
# Health check
make doctor

# Tail server logs
make logs

# Test the bridge directly
curl http://<your-server-ip>:8080/health
```

See [docs/troubleshooting.md](troubleshooting.md) for common issues.
