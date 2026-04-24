# Hardware — M5Stack StackChan (Dotty's body)

## TL;DR

- The robot body is the **M5Stack StackChan** kit: an M5Stack **CoreS3** (ESP32-S3) head on a 2-servo chassis.
- The CoreS3 supplies the SoC, display, camera, mic array, speaker, IMU, proximity, microSD, NFC, IR — all integrated.
- The *StackChan kit* adds the head-yaw servo, head-pitch servo, 12 RGB LEDs, 3-zone touch panel, 700 mAh supplementary battery, USB-C, and the 3D-printed body.
- Firmware on the device is built from [`m5stack/StackChan`](https://github.com/m5stack/StackChan) — an Arduino C++ codebase that bundles the **XiaoZhi AI agent** client. It is **not** the same codebase as `meganetaaan/stack-chan` (the original Moddable/JS project) or `78/xiaozhi-esp32` (generic voice-assistant firmware).
- The device advertises itself over the Xiaozhi WebSocket protocol and exposes **on-device tools via MCP** (see [protocols.md](./protocols.md)).

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
| Wi-Fi | 2.4 GHz only |
| BLE | Yes |
| USB | USB-C (device + power) |
| Dimensions | 54.0 × 54.0 × 15.5 mm (unit only) |
| Weight | 72.7 g (unit only) |

## What the StackChan kit adds on top

Values from [`m5stack/StackChan` README](https://github.com/m5stack/StackChan) (see [references.md](./references.md#hardware)):

| Component | Spec |
|---|---|
| Head-yaw servo | Feedback servo, 360° horizontal rotation |
| Head-pitch servo | Feedback servo, 90° vertical movement |
| Front-panel LEDs | 12 × RGB, arranged in two rows |
| Touch panel | 3-zone (beyond the display's own touch) |
| NFC | Yes, reader + writer |
| IR | Transmitter + receiver |
| Supplementary battery | 700 mAh |
| Chassis | 3D-printed body, base, feet (STL published) |

**Note on battery size.** The CoreS3's internal battery is 500 mAh; the StackChan kit documents 700 mAh. The StackChan kit appears to bundle an external cell that supersedes or supplements the CoreS3 internal one. Check the physical kit to confirm before quoting battery life numbers.

## Firmware lineage

Three related codebases — do not confuse them:

| Repo | Language | Purpose | Runs on StackChan? |
|---|---|---|---|
| [`meganetaaan/stack-chan`](https://github.com/meganetaaan/stack-chan) | TypeScript / JavaScript on Moddable SDK | Original open-source Stack-chan (Shinya Ishikawa) | Yes (but not what we run) |
| [`m5stack/StackChan`](https://github.com/m5stack/StackChan) | Arduino C++ | M5Stack's official firmware — bundles XiaoZhi AI agent, targets CoreS3 | **Yes — this is what we flash** |
| [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32) | Arduino C++ | Generic multi-board voice assistant firmware (70+ target boards) | Runs on the same ESP32-S3 but is a different application — you pick one, not both |

Our pipeline uses `m5stack/StackChan` because it comes with the robot-body integration (servos, avatar rendering, LED patterns, MCP tools mapped to peripherals) already done. `78/xiaozhi-esp32` is the upstream *protocol* reference — the voice channel speaks the same WebSocket protocol regardless.

## On-device MCP tools (Dotty's hand-holds)

The device acts as an **MCP server** — after the WS `hello` handshake, it advertises its tools to xiaozhi-server via `tools/list` (JSON-RPC 2.0 inside `type: mcp` messages). See [protocols.md](./protocols.md#mcp-tools-over-ws) for the exact wire format.

Tool names follow the dotted-namespace convention from the `78/xiaozhi-esp32` MCP protocol doc (e.g. `self.audio_speaker.set_volume`, `self.get_device_status`). The **registration sites** in the firmware use `McpServer::AddTool` for public tools and `McpServer::AddUserOnlyTool` for privileged/hidden ones.

Per `tasks.md`, the live firmware advertises **11 tools**. The mapping below is from internal deployment observation plus the `m5stack/StackChan` README's feature list — **verify against the handshake logs** (`docker logs xiaozhi-esp32-server | grep tools/list`) before relying on exact tool names:

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

These are real hardware features with no documented MCP tool on Dotty today. See [latent-capabilities.md](./latent-capabilities.md#hardware-unused) for prioritization.

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

- **Mic is I2S via ES7210.** Hot whenever the firmware chooses — there is no hardware mic-mute. The privacy-LED task in `tasks.md` exists because of this.
- **Servos can move fast.** Feedback servos in a kids' environment can startle. The StackChan kit uses the M5Stack Avatar library's ease functions; the velocity cap is a firmware-side choice, not a hardware limit. See `tasks.md` "Tame violent servo motion".
- **Camera has no shutter.** Software-only enable. The `take_photo` MCP tool should always co-activate a distinct LED state (see child-safety task).

## See also

- [protocols.md](./protocols.md#mcp-tools-over-ws) — how the device advertises these tools.
- [latent-capabilities.md](./latent-capabilities.md#hardware-unused) — what to do with the unused peripherals.
- [references.md](./references.md#hardware) — all upstream hardware links.

Last verified: 2026-04-24.
