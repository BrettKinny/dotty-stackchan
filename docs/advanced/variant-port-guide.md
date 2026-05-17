---
title: Variant Port Guide
description: How to run Dotty's voice stack on an ESP32-S3 board other than the M5Stack CoreS3.
---

# Variant port guide

Dotty's server stack (xiaozhi-server, bridge, ZeroClaw) is protocol-agnostic — it doesn't care which ESP32-S3 board is on the other end of the WebSocket. All the interesting porting work is in the firmware.

This guide explains how to bring up the voice pipeline on a different ESP32-S3 board, and what hardware adaptation is needed to get the robot-body features (servos, LEDs, display) working.

## TL;DR

| Goal | Firmware path | Effort |
|---|---|---|
| Voice only (ASR / TTS / LLM) | `78/xiaozhi-esp32` with your board's config | Low — add board config + flash |
| Full robot body (servos, LEDs, avatar) | Port `m5stack/StackChan` to your board | Medium–high — display + servo + LED adaptation |

---

## Server side: nothing to change

xiaozhi-server, the bridge, and ZeroClaw run on the server host — not on the device. They communicate over the Xiaozhi WebSocket protocol, which is board-agnostic.

The only server-side value that varies per board is the OTA firmware filename, which you set in the device's `sdkconfig` before flashing.

---

## Firmware path decision

Two codebases speak the Xiaozhi protocol:

| Firmware | Board support | Robot body | Use when |
|---|---|---|---|
| [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32) | 70+ ESP32-S3 targets | No — generic voice assistant | You want voice quickly on a custom board, no servo/avatar |
| [`m5stack/StackChan`](https://github.com/m5stack/StackChan) | CoreS3 out of the box | Yes — servos, avatar, LEDs, MCP tools | You have a StackChan-like body and want full robot integration |

Both firmwares are vendored in this repo under `firmware/` (a git submodule pointing to `BrettKinny/StackChan`). The StackChan firmware pulls in `78/xiaozhi-esp32` v2.2.4 at build time via `fetch_repos.py`.

---

## Option A — voice pipeline on a new ESP32-S3 board

Use `78/xiaozhi-esp32` directly. You get ASR / TTS / LLM but no servo or avatar control.

### 1. Check if your board already has a config

After running `fetch_repos.py`, the upstream firmware is cloned into `firmware/firmware/xiaozhi-esp32/`. Board configs live under `boards/`:

```bash
ls firmware/firmware/xiaozhi-esp32/boards/
```

If your board is listed (search by chipset, e.g. `esp32s3_*`), you can build directly:

```bash
idf.py set-target esp32s3
idf.py -D SDKCONFIG_DEFAULTS="boards/<your-board>/sdkconfig.defaults" build
```

### 2. Create a new board definition

If your board isn't in the list, add one. Each board directory needs at minimum an `sdkconfig.defaults` that sets:

- Flash size and PSRAM type (`CONFIG_ESPTOOLPY_FLASHSIZE`, `CONFIG_SPIRAM_*`)
- Audio codec I2S pins and clock rates
- Microphone channel configuration
- Display interface pins (if using the avatar renderer)

Use a similar existing board as your starting point. `boards/m5stack_core_s3/sdkconfig.defaults` is the closest reference for any M5Stack product.

```
firmware/firmware/xiaozhi-esp32/boards/
  m5stack_core_s3/
    sdkconfig.defaults       ← reference config
  your_board_name/
    sdkconfig.defaults       ← create this
```

### 3. Build and flash

See [CLAUDE.md](../../CLAUDE.md#firmware-iteration) for the full build and flash commands. In short:

```bash
cd firmware/firmware

# Fetch upstream + apply patches, then build
docker run --rm -v "$PWD:/project" -w /project \
  espressif/idf:v5.5.4 bash -lc \
  'git config --global --add safe.directory "*" && python fetch_repos.py && idf.py build'

# Flash (adjust the port if needed)
docker run --rm -v "$PWD:/project" -w /project \
  --device=/dev/ttyACM0 espressif/idf:v5.5.4 \
  bash -lc 'idf.py -p /dev/ttyACM0 -b 921600 flash'
```

### 4. Verify the WebSocket connection

Once flashed, the device should connect and negotiate the handshake. Check the server logs:

```bash
docker logs xiaozhi-esp32-server | grep -E '(hello|tools/list|connected)'
```

A `tools/list` response confirms the device is advertising its MCP tools and the voice pipeline is ready.

---

## Option B — porting m5stack/StackChan to a new board

If you have servo hardware and want the full robot-body MCP tools, adapt the StackChan firmware. It targets the CoreS3 explicitly in several places.

### Adaptation checklist

**Display (avatar renderer)**

The M5Stack Avatar library assumes an ILI9342C display at 320×240 over SPI. If your display uses a different controller:

1. Update the `DisplayDevice` typedef and initialization in the display driver.
2. Adjust resolution constants if your panel differs from 320×240.
3. Test face animations independently before wiring the audio pipeline.

**Audio codec (ASR input)**

The CoreS3 uses the ES7210 codec for mic input via I2S. If your board uses a different codec:

1. Find and update the codec init sequence in the board-specific audio driver.
2. Update I2S clock, sample rate, and codec register writes.
3. The Xiaozhi protocol expects 16 kHz mono input — resample in firmware if your codec runs at a different rate.

**Servos**

The StackChan kit uses SG90-class feedback servos on a dedicated bus. If your board uses a different servo controller or different pins:

1. Update pin definitions in the servo driver.
2. Update the physical angle limits (min/max) for your mechanism.
3. The spring-physics motion system (`motion.h`) is board-agnostic above the servo layer and does not need changing.

**RGB LEDs**

The kit has 12 NeoPixel-compatible LEDs. If your board has a different count or layout:

1. Update `LED_COUNT` and the layout mapping in the LED driver.
2. LED color patterns are defined in `bridge.py` server-side — changing them is a config change, not a firmware change.

**MCP tool registration**

Each hardware peripheral exposed to the LLM is registered via `McpServer::AddTool`. If your board lacks a peripheral (e.g. no NFC), the tool still registers but returns an error when called. Guard missing hardware with a build-time config check:

```cpp
#if CONFIG_YOUR_BOARD_HAS_NFC
  McpServer::AddTool("self.nfc.read_tag", /* ... */);
#endif
```

### Patch workflow

This repo carries changes to the upstream `78/xiaozhi-esp32` as a patch:

```
firmware/firmware/patches/xiaozhi-esp32.patch
```

After editing the local `xiaozhi-esp32/` working tree, regenerate:

```bash
git -C firmware/firmware/xiaozhi-esp32 diff HEAD > firmware/firmware/patches/xiaozhi-esp32.patch
```

Verify the patch applies cleanly to a fresh v2.2.4 checkout before committing.

Changes to `m5stack/StackChan`-specific code go directly into the submodule (tracked on the `dotty` branch of `BrettKinny/StackChan`).

---

## Testing your port

Once the device connects, run through:

1. **WebSocket handshake** — `tools/list` in the server logs should list all advertised MCP tools.
2. **Voice round-trip** — speak a simple phrase and confirm ASR → LLM → TTS returns audio to the device.
3. **MCP tool call** — send a test instruction through the bridge:
   ```bash
   curl -X POST http://<ZEROCLAW_HOST>:8080/api/message \
     -H 'Content-Type: application/json' \
     -d '{"content":"Turn your head to the right"}'
   ```
4. **LED feedback** — confirm the three-state pattern (listening / thinking / speaking) works on your LED hardware.

---

## See also

- [hardware-support.md](../hardware-support.md) — verified / build-only / out-of-scope tier matrix.
- [hardware.md](../hardware.md) — CoreS3 specs and MCP tool catalog.
- [protocols.md](../protocols.md) — Xiaozhi WebSocket protocol reference.
- [`78/xiaozhi-esp32 boards/`](https://github.com/78/xiaozhi-esp32/tree/main/boards) — upstream board definitions.
- [`m5stack/StackChan`](https://github.com/m5stack/StackChan) — the firmware we vendor and build from.

Last verified: 2026-05-17.
