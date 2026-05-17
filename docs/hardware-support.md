---
title: Hardware Support
description: Verified and untested hardware configurations for the voice stack.
---

# Hardware support

## TL;DR

- **One verified configuration**: M5Stack CoreS3 + StackChan servo kit.
- Other ESP32-S3 boards supported by the vendored xiaozhi-esp32 firmware will likely build and boot, but robot-body features (servos, avatar, LEDs) need board-specific adaptation.
- Non-S3 ESP32 boards and the older M5Stack Core2 are out of scope.

---

## Support tiers

### Verified

The only hardware this stack has been tested end-to-end on.

| Component | Detail |
|---|---|
| **Main board** | M5Stack CoreS3 |
| **SoC** | ESP32-S3, dual-core Xtensa LX7 @ 240 MHz |
| **Memory** | 8 MB PSRAM (Quad), 16 MB flash |
| **Display** | 2.0" IPS 320x240, capacitive touch (ILI9342C) |
| **Camera** | GC0308, 0.3 MP |
| **Microphone** | MSM261S4030H0R (dual-mic, via ES7210 codec) |
| **Speaker** | AW88298 amplifier, 16-bit I2S, 1 W |
| **Wi-Fi** | 2.4 GHz only (no 5 GHz) |
| **Body kit** | M5Stack StackChan servo kit |
| **Servos** | 2x SG90-class feedback servos (pan: 360 deg yaw, tilt: 90 deg pitch) |
| **Additional** | 12x RGB LEDs, 3-zone touch panel, NFC, IR tx/rx, 700 mAh supplementary battery |
| **Firmware** | Built from [`m5stack/StackChan`](https://github.com/m5stack/StackChan) (Arduino C++) |

This is the configuration described throughout the rest of the docs. The servo kit provides the head-pan and head-tilt movement that makes StackChan look like a robot rather than a screen on a desk.

**Servo note.** The StackChan kit uses SG90-class feedback servos. There is currently no firmware-side velocity or acceleration cap, which means head movements can be abrupt. This is a known limitation documented in [hardware.md](./hardware.md#safety-relevant-hardware-facts).

For the full BOM and 3D-printed chassis STLs, see the upstream repo: [m5stack/StackChan](https://github.com/m5stack/StackChan).

### Build-only (untested)

The vendored [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32) firmware (the upstream protocol reference, not the firmware we flash) supports 70+ ESP32-S3 target boards. Any ESP32-S3 board in that list should:

- **Build** successfully from source.
- **Boot** and connect to xiaozhi-esp32-server over WebSocket.
- **Run ASR/TTS** through the voice pipeline (audio in, audio out).

What will likely **not** work without board-specific adaptation:

- Servo control (the StackChan firmware's servo code targets the kit's specific servo bus and feedback protocol).
- Avatar display (the M5Stack Avatar library assumes a 320x240 ILI9342C display and the CoreS3's touch controller).
- LED patterns (hardcoded to the kit's 12-LED layout).
- MCP tools that touch kit-specific peripherals (head yaw/pitch, LED color, NFC, IR).

If you want to run this stack on a different ESP32-S3 board, you are signing up for firmware-level porting work. The server-side infrastructure (xiaozhi-esp32-server, bridge, ZeroClaw) doesn't care what board is on the other end of the WebSocket.

### Out of scope

These are explicitly not supported and are unlikely to work without significant effort:

| Hardware | Why |
|---|---|
| **M5Stack Core2** | Older StackChan hardware. Different SoC (ESP32, not ESP32-S3), different display controller, different audio codec. The `m5stack/StackChan` firmware targets CoreS3 only. You would need to port the firmware or use the original `meganetaaan/stack-chan` Moddable JS firmware, which is a completely different codebase. |
| **ESP32 (non-S3)** | Insufficient PSRAM for the voice pipeline. The S3's 8 MB PSRAM is load-bearing for audio buffering. |
| **Non-ESP32 boards** | The firmware is Arduino C++ targeting the ESP-IDF toolchain. ARM, RISC-V, x86, etc. boards are a different universe. |

---

## See also

- [hardware.md](./hardware.md) — full CoreS3 specs, firmware lineage, on-device MCP tool catalog.
- [references.md](./references.md#hardware) — upstream hardware links.
- [m5stack/StackChan](https://github.com/m5stack/StackChan) — hardware BOM, chassis STLs, firmware source.

Last verified: 2026-05-17.
