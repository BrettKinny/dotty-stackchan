---
title: Hardware
description: M5Stack StackChan hardware specs, CoreS3 ESP32-S3 SoC, and servo chassis.
---

# Hardware — M5Stack StackChan

## TL;DR

- The robot body is the **M5Stack StackChan** kit: an M5Stack **CoreS3** (ESP32-S3) head on a 2-servo chassis.
- The CoreS3 supplies the SoC, display, camera, mic array, speaker, IMU, proximity, microSD — all integrated. NFC and the IR tx/rx pair physically live on the kit body, not the CoreS3 (see the kit table below).
- The *StackChan kit* adds the head-yaw servo, head-pitch servo, 12 RGB LEDs, 3-zone touch panel, 550 mAh supplementary battery, USB-C, NFC, an IR tx/rx pair, an IO expander, a dedicated battery monitor, and the 3D-printed body. A separate handheld ESP-NOW remote controller ships in the same box (M5Stack's product page describes it as an ESP-NOW wireless remote).
- Firmware on the device is built from [`m5stack/StackChan`](https://github.com/m5stack/StackChan) — an Arduino C++ codebase that bundles the **XiaoZhi AI agent** client. It is **not** the same codebase as `meganetaaan/stack-chan` (the original Moddable/JS project) or `78/xiaozhi-esp32` (generic voice-assistant firmware).
- The device advertises itself over the Xiaozhi WebSocket protocol and exposes **on-device tools via MCP** (see [protocols.md](./protocols.md)).
- Canonical hardware reference: [`docs.m5stack.com/en/StackChan`](https://docs.m5stack.com/en/StackChan) (kit-level) and [`docs.m5stack.com/en/core/CoreS3`](https://docs.m5stack.com/en/core/CoreS3) (head unit). See [references.md](./references.md#hardware).

## The SoC and board: M5Stack CoreS3

All values from [`docs.m5stack.com/en/core/CoreS3`](https://docs.m5stack.com/en/core/CoreS3) (see [references.md](./references.md#hardware)).

| Component | Spec |
|---|---|
| SoC | ESP32-S3, dual-core Xtensa LX7 @ 240 MHz |
| Flash | 16 MB |
| PSRAM | 8 MB Quad |
| Display | 2.0″ IPS, 320×240, ILI9342C, capacitive touch |
| Camera | GC0308, 0.3 MP (built-in) |
| Proximity / ambient-light | LTR-553ALS-WA |
| IMU | BMI270 (6-axis accel + gyro) |
| Magnetometer | BMM150 (3-axis) — gives 9-axis combined with BMI270 |
| Mic codec | ES7210, dual-mic input |
| Speaker amp | AW88298, 16-bit I2S, 1 W |
| PMU | AXP2101 |
| Battery (internal) | 500 mAh Li-ion |
| RTC | BM8563 |
| microSD | Supported, up to 16 GB |
| Wi-Fi | 2.4 GHz, IEEE 802.11 b/g/n |
| BLE | Bluetooth 5 LE |
| USB | USB-C (USB CDC + full-speed OTG, power + data) |
| Touch (display) | FT6336U capacitive multi-touch (over the 320×240 panel) |
| Dimensions | 54.0 × 54.0 × 15.5 mm (CoreS3 unit only) |
| Weight | 72.7 g (CoreS3 unit only) |

## What the StackChan kit adds on top

Values from the [M5Stack StackChan product docs](https://docs.m5stack.com/en/StackChan) and the [`m5stack/StackChan` firmware README](https://github.com/m5stack/StackChan) (see [references.md](./references.md#hardware)):

| Component | Spec |
|---|---|
| Head-yaw servo (X axis) | Feedback servo, 360° continuous horizontal rotation |
| Head-pitch servo (Y axis) | SCS0009 feedback servo, 90° vertical movement — **recommended operating range 5°–85°** |
| Front-panel LEDs | 12 × WS2812C RGB, arranged in two rows |
| 3-zone touch panel | Si12T driver (separate from the CoreS3 display's FT6336U) |
| NFC | ST25R3916 reader/writer (I2C `0x50`) |
| IR | IRM56384 transmitter + receiver pair |
| Battery monitor | INA226AIDGSR coulomb counter (I2C `0x41`) |
| IO expander | PY32L020 (I2C `0x6F` or `0x71`; drives IO1, IO14, VM_EN, RGB power) |
| Supplementary battery | 550 mAh (per M5Stack product docs) |
| USB | USB-C (power + data) |
| Buttons | Power, reset; power-indicator LED |
| Wireless extras | ESP-NOW supported (peer-to-peer over the 2.4 GHz radio, no AP needed); companion **StackChan World** mobile app (iOS / Android) |
| Chassis | 3D-printed body, base, feet (STL published) |
| Dimensions (assembled) | 54.0 × 70.5 × 61.5 mm, **187.2 g** |
| Remote controller | Ships in-box, 37.6 g. M5Stack's product page describes it as an ESP-NOW wireless remote control. |

**Pin map (CoreS3 → body):**

| Function | Pin(s) |
|---|---|
| Servo bus (UART) | G6 TX, G7 RX |
| IR | G5 transmit, G10 receive |
| I2C (NFC, touch, battery monitor, IO expander) | G11 SCL, G12 SDA |

**Note on battery size.** The CoreS3's internal cell is 500 mAh; the canonical M5Stack StackChan product page documents a **550 mAh** supplementary cell in the body (earlier internal notes had this as 700 mAh — corrected against `docs.m5stack.com/en/StackChan`). Either way, total runtime depends on which cell is in circuit at a given moment — bench-measure before quoting runtime numbers.

## Firmware lineage

Three related codebases — do not confuse them:

| Repo | Language | Purpose | Runs on StackChan? |
|---|---|---|---|
| [`meganetaaan/stack-chan`](https://github.com/meganetaaan/stack-chan) | TypeScript / JavaScript on Moddable SDK | Original open-source Stack-chan (Shinya Ishikawa) | Yes (but not what we run) |
| [`m5stack/StackChan`](https://github.com/m5stack/StackChan) | Arduino C++ | M5Stack's official firmware — bundles XiaoZhi AI agent, targets CoreS3 | **Yes — this is what we flash** |
| [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32) | Arduino C++ | Generic multi-board voice assistant firmware (70+ target boards) | Runs on the same ESP32-S3 but is a different application — you pick one, not both |

Our pipeline uses `m5stack/StackChan` because it comes with the robot-body integration (servos, avatar rendering, LED patterns, MCP tools mapped to peripherals) already done. `78/xiaozhi-esp32` is the upstream *protocol* reference — the voice channel speaks the same WebSocket protocol regardless.

## On-device MCP tools

The device acts as an **MCP server** — after the WS `hello` handshake, it advertises its tools to xiaozhi-server via `tools/list` (JSON-RPC 2.0 inside `type: mcp` messages). See [protocols.md](./protocols.md#mcp-tools-over-ws) for the exact wire format.

Tool names follow the dotted-namespace convention from the `78/xiaozhi-esp32` MCP protocol doc (e.g. `self.audio_speaker.set_volume`, `self.get_device_status`). The **registration sites** in the firmware use `McpServer::AddTool` for public tools and `McpServer::AddUserOnlyTool` for privileged/hidden ones.

Per internal deployment observation, the live firmware advertises **11 tools**. The mapping below is from that observation plus the `m5stack/StackChan` README's feature list — **verify against the handshake logs** (`docker logs xiaozhi-esp32-server | grep tools/list`) before relying on exact tool names:

| # | Tool (functional) | Hardware touched |
|---|---|---|
| 1 | Head yaw | Yaw feedback servo |
| 2 | Head pitch | Pitch feedback servo |
| 3 | LED color | 12× RGB LEDs |
| 4 | Camera — `take_photo` | GC0308 camera |
| 5 | Reminders / timer | RTC (BM8563) + software |
| 6 | Volume | AW88298 amp |
| 7 | Display brightness | ILI9342C backlight |
| 8 | Screen theme | Avatar renderer |
| 9 | Face expression | Avatar renderer (see [protocols.md](./protocols.md#emotion-protocol)) |
| 10 | Get device status | All (battery, RSSI, uptime) |
| 11 | Reboot | MCU |

**Action item** to make this table canonical: capture a real `tools/list` response and commit the tool-name column verbatim. Tracked in [latent-capabilities.md](./latent-capabilities.md#observability) as an observability gap.

## Peripherals the firmware could expose but doesn't (per current observation)

These are real hardware features with no documented MCP tool in the default firmware today. See [latent-capabilities.md](./latent-capabilities.md#hardware-unused) for prioritization.

| Peripheral | Capability | Why it'd matter |
|---|---|---|
| BMI270 + BMM150 (9-axis IMU) | Shake / gesture / orientation detection | Tap-to-activate, shake-to-reset; orientation-aware responses |
| LTR-553 proximity sensor | Hand-approach detection, ambient light | Wake-on-approach; auto-dim at night |
| NFC module | Tag read/write | Tap an NFC card/toy to trigger a scripted interaction |
| IR tx/rx | Learn + replay IR codes | Universal-remote mode for legacy appliances |
| microSD slot | Offline asset storage | Pre-bundled sound packs, offline fallback voices |
| 3-zone touch panel | Multi-zone tap/swipe | Gesture controls without using the display's touch |
| Camera (beyond `take_photo`) | Video streaming / on-device vision preprocessing | Privacy-preserving local vision before sending to a VLLM |

## Safety-relevant hardware facts

- **Mic is I2S via ES7210.** Hot whenever the firmware chooses — there is no hardware mic-mute. The privacy-indicator LED item in [`ROADMAP.md`](ROADMAP.md) exists because of this.
- **Servos can move fast.** Feedback servos in a kids' environment can startle. The StackChan kit uses the M5Stack Avatar library's ease functions; the velocity cap is a firmware-side choice, not a hardware limit. See the "Servo speed caps" item in [`ROADMAP.md`](ROADMAP.md).
- **Y-axis (pitch) servo angle limit: 5°–85°.** M5Stack's product page explicitly recommends keeping the vertical servo inside this range. Commanding 0° or 90° risks mechanical bind / gear damage on the SCS0009. Any firmware path that parks the head (sleep state's "face-down + centred" included — see [modes.md](./modes.md)) must clamp to this window.
- **Camera has no shutter.** Software-only enable. The `take_photo` MCP tool should always co-activate a distinct LED state (see child-safety task).
- **Default stock wake word is "Hi, StackChan".** Our deployment overrides ASR + wake-word handling via xiaozhi-esp32-server, so this only matters if a unit boots stock firmware (e.g. before first flash).

## See also

- [protocols.md](./protocols.md#mcp-tools-over-ws) — how the device advertises these tools.
- [latent-capabilities.md](./latent-capabilities.md#hardware-unused) — what to do with the unused peripherals.
- [references.md](./references.md#hardware) — all upstream hardware links.

Last verified: 2026-05-18 (against `docs.m5stack.com/en/StackChan`).
