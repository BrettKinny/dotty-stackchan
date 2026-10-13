---
title: Troubleshooting
description: Symptom-first lookup table for common and obscure failure modes.
---

# Troubleshooting

Symptom-first lookup table covering common and obscure failure modes. Pair with [quickstart.md](./quickstart.md) for happy-path commands and [voice-pipeline.md](./voice-pipeline.md) for ASR/TTS internals.

---

## No audio / empty TTS response

**Symptom:** The robot appears to process the utterance (logs show ASR text and an LLM response), but no audio plays back. The TTS stage produces zero-length or near-zero-length audio.

**Cause:** Language mismatch between the TTS voice and the response text. EdgeTTS `en-*` voices return empty audio when given non-English text (Chinese, Japanese, etc.). This is not a throttle or rate limit — it's a silent failure in the EdgeTTS service.

**Fix:**
1. Check the xiaozhi-server logs for the LLM response text. On PiVoiceLLM, non-English text means the model ignored the per-turn `build_turn_suffix()` policy.
2. Confirm `custom-providers/pi_voice/pi_voice.py` appends `build_turn_suffix()` to the prompt. PiVoiceLLM uses `--no-context-files` and does not forward xiaozhi's system dialogue, so persona and `.config.yaml` prompt edits do not diagnose this provider.
3. Check `data/.config.yaml` to confirm the TTS voice matches the expected response language (e.g., `en-AU-WilliamNeural` for English).
4. If using Piper TTS instead of EdgeTTS, confirm the selected voice model matches the response language.

---

## Robot responds in Chinese / Japanese instead of English

**Symptom:** The robot speaks, but in the wrong language. Logs show Chinese or Japanese text in the LLM response.

**Cause:** The LLM (Qwen3) is ignoring the per-turn language constraint. Qwen3 can leak Chinese despite the English-only suffix; English has no deterministic output validator.

**Fix:**
1. Confirm the per-turn sandwich enforcement is active on the live `PiVoiceLLM` path. Static system prompts alone are not enough — every turn is wrapped with an explicit English+emoji suffix. This is `build_turn_suffix()` in `custom-providers/textUtils.py`, applied by `custom-providers/pi_voice/pi_voice.py` (`_wrap_with_sandwich`). (The old `bridge.py::_build_sandwich_prompt` was part of the retired ZeroClaw bridge and no longer exists — the bridge is not in the voice path.)
2. Inspect the prompt captured by PiVoiceLLM or its unit tests; do not use persona files or the top-level `.config.yaml` prompt as evidence for this path.
3. Watch the actual voice path in xiaozhi-server logs. `docker logs dotty-pi` is normally empty because Pi runs as a `docker exec` child; PiClient owns that child's stdio.
4. As a last resort, the ASR may be mis-transcribing English as another language. Check the configured ASR language is `en` rather than `auto`.

---

## Audio choppy or cutting out

**Symptom:** The robot responds but the audio is choppy, stuttery, or cuts off mid-sentence.

**Possible causes:**

- **WiFi signal.** The robot's ESP32-S3 is 2.4 GHz only. Check RSSI — anything below -70 dBm will cause packet loss on the WebSocket stream. Move the robot closer to the access point, or reduce 2.4 GHz interference.
- **WebSocket abnormal close.** Check xiaozhi-server logs for WS disconnect/reconnect events. The device will silently reconnect, but audio in flight is lost.
- **TTS chunk timing.** If using EdgeTTS (cloud), network jitter between the server and Microsoft's edge servers can cause uneven audio delivery. Switching to Piper (local) eliminates this variable entirely.
- **Server CPU contention.** If other containers are competing for CPU during the ASR or TTS stages, audio processing can stall. Check `docker stats` on the server.

---

## "No bootable app partitions" boot loop after flashing

**Symptom:** After flashing the firmware the screen is frozen or black. A serial monitor shows the bootloader looping on `E boot: No bootable app partitions in the partition table`, or `Image length ... doesn't fit in partition length ...`.

**Cause:** The device was flashed without a partition table, so it kept the layout left by whatever firmware was on it before (the M5Burner StackChan demo, Home Assistant Voice, etc.). That layout doesn't match Dotty's images.

**Fix:** Re-flash with the **full six-file command** in [Quickstart step 1](quickstart.md#1-flash-the-firmware). It writes `bootloader.bin` at `0x0` and `partition-table.bin` at `0x8000` — with those in place the partition offsets line up. If your downloaded release is missing either file, grab the latest `fw-v` release, which ships all six binaries.

---

## Robot not responding after OTA / firmware update

**Symptom:** The robot boots and connects to WiFi, but never responds to voice. May show a face but no indication of listening.

**Fix:**
1. Check the bridge health endpoint: `curl http://<XIAOZHI_HOST>:8081/health`. If the bridge is down, restart it.
2. Check xiaozhi-server logs: `docker logs -f xiaozhi-esp32-server` on the server. Look for connection attempts from the robot.
3. Verify which OTA URL the firmware was built with. The StackChan's on-device Settings app has no OTA URL field. For another self-hosted server, rebuild with `CONFIG_OTA_URL="http://<XIAOZHI_HOST>:8003/xiaozhi/ota/"` and re-flash using [SETUP.md](../SETUP.md#2-build-and-flash-open-firmware). If the device is currently exposing the optional Xiaozhi SoftAP captive portal, its browser-based **Advanced** tab can persist an `ota_url` override instead.
4. Open the browser test page (`repo/main/xiaozhi-server/test/test_page.html`) and point it at `ws://<XIAOZHI_HOST>:8000/xiaozhi/v1/`. If the browser page works but the robot doesn't, it's a robot-side configuration issue.

---

## ModuleNotFoundError on docker compose up

**Symptom:** The xiaozhi-server container starts but immediately fails with a Python `ModuleNotFoundError` in the logs.

**Cause:** Custom providers are not mounted correctly into the container. The volume mounts in `docker-compose.yml` map host-side files into specific paths inside the container. If the host path is wrong, the file doesn't arrive and the import fails.

**Fix:**
1. Check `docker logs xiaozhi-esp32-server` for the exact missing module name.
2. Verify the volume mounts in `docker-compose.yml` match the expected paths. The custom providers must land at:
   - `custom-providers/pi_voice/` -> `/opt/xiaozhi-esp32-server/core/providers/llm/pi_voice/`
   - `custom-providers/edge_stream/` -> `/opt/xiaozhi-esp32-server/core/providers/tts/edge_stream/`
   - `custom-providers/asr/fun_local.py` -> `/opt/xiaozhi-esp32-server/core/providers/asr/fun_local.py`
3. If the missing module is a Python dependency (e.g., `pydub`, `edge-tts`), it may not be in the base image. Add it via the compose file's environment or bake a custom image layer.
4. After fixing mounts, restart the container: `docker compose restart` (not `docker compose down` + `up`, which marks the container as stopped and changes reboot behavior).

---

## No facial expression change on the robot

**Symptom:** The robot speaks but its face stays neutral. No smile, laugh, or other expression.

**Cause:** The LLM response doesn't start with a supported emoji. The xiaozhi firmware parses the leading emoji to select a face animation. If the first character isn't a recognized emoji, no animation triggers.

**Supported emoji map:**

| Emoji | Expression |
|---|---|
| `😊` | Smile |
| `😆` | Laugh |
| `😢` | Sad |
| `😮` | Surprise |
| `🤔` | Thinking |
| `😠` | Angry |
| `😐` | Neutral |
| `😍` | Love |
| `😴` | Sleepy |

**Fix:**
1. Check the xiaozhi-server logs for the raw LLM response. PiVoiceLLM's per-turn suffix requests an allowed emoji and `_enforce_leading_emoji()` prepends neutral `😐` when it is absent. A prefix-free response therefore indicates the deployed provider is stale or bypassed.
2. If the response has an emoji but the face doesn't change, it may be an unsupported emoji. Only the nine listed above are mapped to animations.
3. Confirm the selected provider is PiVoiceLLM and the deployed `pi_voice.py` contains `_enforce_leading_emoji()`. The retired bridge fallback is unrelated.

---

## Servo snaps violently / startling head movement

**Symptom:** The robot's head jerks abruptly when changing position, instead of moving smoothly.

**Cause:** Known limitation. The current firmware does not implement a velocity or acceleration cap on servo commands. The feedback servos move at their maximum speed, which can be startling — especially in a household with kids.

**Workaround:** There is no software workaround at this time. This is tracked as a firmware-level fix. See [hardware.md](./hardware.md#safety-relevant-hardware-facts) for context.

---

## Bridge unreachable / "(no response)" in the robot's voice

**Symptom:** The robot says something like "no response" or goes silent after you speak. xiaozhi-server logs show a failed HTTP POST or a failed `docker exec` call.

**Fix:**
1. Check that the `bridge.py` dashboard container and the `dotty-pi` brain container are running: `docker ps | grep -E 'bridge|dotty-pi'`
2. Test the bridge dashboard health endpoint: `curl http://<XIAOZHI_HOST>:8081/health`
3. For `PiVoiceLLM` failures, check that the `dotty-pi` container is healthy and the Docker socket is bind-mounted into the xiaozhi container. `docker exec -i dotty-pi echo ok` should return `ok`.
4. If the bridge container crashes on startup, check its logs: `docker logs bridge`

---

## Docker image upgrade breaks things

**Symptom:** After pulling a new xiaozhi-esp32-server image, the container fails to start or behaves differently.

**Fix:**
1. Pin the image tag in `docker-compose.yml` before upgrading. The `server_latest` tag is a moving target.
2. Check the upstream changelog for breaking config changes — `data/.config.yaml` keys may have been renamed or removed.
3. If custom providers fail after an upgrade, the upstream Python module structure may have changed. Check that the mount target paths still exist inside the new image.
4. Roll back by specifying the previous image tag in `docker-compose.yml` and running `docker compose up -d`.

---

## See also

- [quickstart.md](./quickstart.md) — happy-path setup + common ops + reboot survival.
- [voice-pipeline.md](./voice-pipeline.md) — details on ASR, TTS, VAD tuning.
- [protocols.md](./protocols.md) — WebSocket wire format for debugging.
- [hardware.md](./hardware.md) — hardware specs and safety notes.

Last verified: 2026-05-17.
