# First-Boot Setup — Bringing Up Your StackChan

Step-by-step for taking a fresh M5Stack StackChan from the box to a working,
fully-self-hosted voice robot. The backend (xiaozhi-server on Unraid, ZeroClaw
bridge on the RPi) is assumed to already be deployed — if you're starting
fresh, skim `README.md` first.

> This guide assumes you've already substituted the placeholders from
> `README.md`'s "Configuring for your environment" section with the real
> values for your setup.

---

## 0. Pre-flight checks (do these with the robot still in the box)

Run these from any LAN-connected machine — should all succeed within a
second or two:

```bash
curl -s http://<UNRAID_IP>:8003/xiaozhi/ota/
# Expect:  OTA接口运行正常，向设备发送的websocket地址是：ws://<UNRAID_IP>:8000/xiaozhi/v1/

curl -s http://<RPI_IP>:8080/health
# Expect:  {"status":"ok","service":"zeroclaw-bridge","acp_running":true}

curl -s -X POST http://<RPI_IP>:8080/api/message \
  -H 'content-type: application/json' \
  -d '{"content":"hi"}' | jq .
# Expect:  {"response":"<emoji> <short reply>","session_id":"..."}
```

If any fails, fix the backend before dealing with the robot:

- OTA down → `ssh <UNRAID_USER>@<UNRAID_IP> 'docker logs --tail 40 xiaozhi-esp32-server'`
- Bridge down → `ssh <RPI_USER>@<RPI_IP> 'sudo journalctl -u zeroclaw-bridge -n 40 --no-pager'`

---

## 1. About the firmware situation

The StackChan ships from M5Stack with **stock firmware** that:

- Shows a **pairing-code screen** on first boot
- Uses **BLE-based WiFi provisioning** via M5Stack's StackChanWorld phone app
- Registers the device to M5Stack's cloud for device management

There is **no SoftAP captive portal** in stock firmware (some older
third-party xiaozhi builds had one; the shipped firmware does not).

To run the StackChan **fully self-hosted** (no phone-app account, no vendor cloud,
your own xiaozhi-server as the endpoint), you need to **reflash the device
with firmware built from the open source tree**.

The upstream firmware lives at **https://github.com/m5stack/StackChan**:
- `firmware/` — M5Stack's patches + ESP-IDF project wrapper
- `firmware/fetch_repos.py` — pulls `78/xiaozhi-esp32` as a dependency and
  applies StackChan-specific patches, adding the board target
  `CONFIG_BOARD_TYPE_M5STACK_STACK_CHAN`

---

## 2. Build and flash open firmware

> **Note — build flow documented from first-pass session findings; not yet
> end-to-end verified. Will be updated after a successful first flash.**

Requires **ESP-IDF v5.5.4**. Easiest path: the official
`espressif/idf:v5.5.4` Docker image.

### 2a. Clone and configure

```bash
git clone https://github.com/m5stack/StackChan.git
cd StackChan/firmware
```

Point the firmware at your xiaozhi-server for OTA. Edit
`firmware/sdkconfig.defaults` and add (or modify) the line:

```
CONFIG_OTA_URL="http://<UNRAID_IP>:8003/xiaozhi/ota/"
```

Trailing slash matters — that's the path the server exposes.

### 2b. Build inside the IDF container

```bash
docker run --rm -it -v "$PWD/..":/project -w /project/firmware \
  espressif/idf:v5.5.4 \
  bash -c "python3 fetch_repos.py && idf.py build"
```

`fetch_repos.py` pulls the xiaozhi-esp32 dependency and applies the
StackChan patches; `idf.py build` produces `build/*.bin`.

### 2c. Flash with device passthrough

Plug the device in via USB-C. It typically appears as `/dev/ttyACM0` on
Linux (`/dev/cu.usbmodem*` on macOS — adapt the `--device` flag).

```bash
docker run --rm -it --device=/dev/ttyACM0 \
  -v "$PWD/..":/project -w /project/firmware \
  espressif/idf:v5.5.4 \
  idf.py -p /dev/ttyACM0 flash
```

If the build reported a non-standard flash command (e.g. a merged-bin
flow), use that instead.

### 2d. First boot after flash

- No pairing-code screen
- No BLE provisioning step
- The device boots, loads WiFi credentials compiled into the firmware
  (or, if you left WiFi unconfigured, whatever fallback the upstream
  build offers — consult the xiaozhi-esp32 README for the current default
  behaviour)
- It POSTs to `http://<UNRAID_IP>:8003/xiaozhi/ota/`, gets back the
  WebSocket endpoint, and connects

Tail the server logs while the device boots so you can watch the
handshake happen (see step 4 below).

---

## 3. WiFi credentials

Two options, depending on what the upstream xiaozhi-esp32 build exposes at
the version you pulled:

- **Compile-time WiFi credentials** — set `CONFIG_WIFI_SSID` and
  `CONFIG_WIFI_PASSWORD` in `sdkconfig.defaults`. Simplest for a static
  home setup; easy to forget they're in the binary.
- **Fallback SoftAP or BLE provisioning** — some upstream builds include a
  fallback provisioning flow if no credentials are saved. Check the
  upstream README for what your commit supports.

Either way, the device must land on a **2.4 GHz** network. ESP32-S3 does
not do 5 GHz.

---

## 4. Watch the handshake

While the freshly flashed device boots, tail the server logs in one
terminal:

```bash
ssh <UNRAID_USER>@<UNRAID_IP> 'docker logs -f xiaozhi-esp32-server'
```

Within ~30s of reboot you should see (in order):

1. A `POST /xiaozhi/ota/` line — device asking for config
2. A WebSocket connect line with the device's MAC
3. A `vad` / `asr` init line when the device first starts listening

If the device isn't on the list after 60s:

- Confirm it actually joined your LAN: check your router's DHCP table for
  a new ESP32-looking MAC
- If it's on the LAN but not reaching OTA, try the OTA URL from a phone on
  the same WiFi — it should return plain text, not a connection error
- Power-cycle the device (hold power 3s, unplug, replug)

---

## 5. First voice test

1. Wait for the face to change from "connecting" to a neutral/idle
   expression. That means the WebSocket is up.
2. Say a wake phrase and then a short message (the default wake phrase
   depends on your xiaozhi-esp32 build — consult its README). Some builds
   also support a press-to-talk button on the side.
3. Watch the logs — you should see:
   - An ASR line with transcribed text
   - A `ZeroClawLLM` call (hits the bridge)
   - A TTS line with the response text
   - Face animation changes to match the leading emoji
4. The robot speaks. If you hear audio but no face change, check that the
   response starts with one of: 😊 😆 😢 😮 🤔 😠 😐 😍 😴.

Expected first-audio latency: **~2–4s** after you stop speaking. If it's
way slower, check `http://<RPI_IP>:8080/health` for `acp_running:true`
(a dead ACP child means the bridge is re-spawning on every request).

---

## 6. Tune if needed

All of these are edits to `data/.config.yaml` on Unraid followed by
`docker compose restart`, except the LLM model (lives on the RPi).

| Complaint | Edit | File |
|---|---|---|
| "It cuts me off mid-sentence" | raise `min_silence_duration_ms` from 700 to e.g. 1000 | `data/.config.yaml` → VAD.SileroVAD |
| "It waits forever after I stop talking" | lower `min_silence_duration_ms` to 400 | same |
| "I don't like the voice" | change `voice:` to any Edge Neural voice | `data/.config.yaml` → TTS.EdgeTTS / StreamingEdgeTTS |
| "Responses are too long" | add "Keep replies under 20 words." to the persona | `data/.config.yaml` → `prompt:` block |
| "Too slow to reply" | switch LLM model | `<RPI_ZEROCLAW_CFG>` on RPi → `default_model` |
| "No facial expression change" | check response actually starts with a supported emoji (tail logs) | — |

---

## 7. If you brick it

Hold the power button for 5s to force reboot. Hold for 10s+ to enter
download mode on some boards (used for firmware reflashing).

To recover, re-flash from step 2c. If the device won't enter download
mode via the button, hold the BOOT button (if present) while pressing
RESET, then release RESET, then release BOOT.

---

## 8. Legacy SoftAP flow (older firmware only)

> **This section may match older xiaozhi-esp32 builds that ship a SoftAP
> captive portal. Stock M5Stack firmware does NOT work this way — if
> you're on current stock, see sections 1–2 above.**

Some older xiaozhi builds expose a SoftAP captive portal on first boot:

1. Device creates an open SSID like `Xiaozhi-XXXX`, `ML307R-XXXX`, or
   `Device-XXXX`.
2. Join it from your phone; a captive portal typically auto-pops. If not,
   browse to `http://192.168.4.1`.
3. Pick your 2.4 GHz LAN SSID, enter the password.
4. Click "Advanced Options" and paste the OTA URL:
   `http://<UNRAID_IP>:8003/xiaozhi/ota/` (trailing slash required).
5. Save. Device reboots and connects.

If you have a build where this works, it's the fastest provisioning flow.
It just isn't what M5Stack ships today.

---

## 9. When it's working: bookmark these

- **Tail voice pipeline**: `ssh <UNRAID_USER>@<UNRAID_IP> 'docker logs -f xiaozhi-esp32-server'`
- **Tail bridge**: `ssh <RPI_USER>@<RPI_IP> 'sudo journalctl -u zeroclaw-bridge -f'`
- **Smoke test end-to-end**: `curl -X POST http://<RPI_IP>:8080/api/message -H 'content-type: application/json' -d '{"content":"test"}'`
- **ZeroClaw's web UI** (for tweaking the agent persona directly):
  `ssh -L 42617:127.0.0.1:42617 <RPI_USER>@<RPI_IP>` then open
  http://localhost:42617 in a browser — pair with the code printed by
  `sudo <RPI_ZEROCLAW_BIN> gateway get-paircode`.
