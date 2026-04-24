---
title: Troubleshooting
description: Symptom-first lookup table for common and obscure failure modes.
---

# Troubleshooting

Symptom-first lookup table. The [README.md Troubleshooting section](../README.md#troubleshooting) covers the most common issues; this page goes deeper and covers additional failure modes observed during development.

---

## No audio / empty TTS response

**Symptom:** The robot appears to process the utterance (logs show ASR text and an LLM response), but no audio plays back. The TTS stage produces zero-length or near-zero-length audio.

**Cause:** Language mismatch between the TTS voice and the response text. EdgeTTS `en-*` voices return empty audio when given non-English text (Chinese, Japanese, etc.). This is not a throttle or rate limit — it's a silent failure in the EdgeTTS service.

**Fix:**
1. Check the bridge logs for the LLM response text. If it contains non-English characters, the LLM is ignoring the English enforcement.
2. Verify the sandwich enforcement in `bridge.py` is active — the `_ensure_emoji_prefix` and per-turn English wrapping should be preventing this.
3. Check `data/.config.yaml` to confirm the TTS voice matches the expected response language (e.g., `en-AU-WilliamNeural` for English).
4. If using Piper TTS instead of EdgeTTS, confirm the selected voice model matches the response language.

---

## Robot responds in Chinese / Japanese instead of English

**Symptom:** The robot speaks, but in the wrong language. Logs show Chinese or Japanese text in the LLM response.

**Cause:** The LLM (Qwen3) is ignoring the system prompt's language constraint. This is a known weakness — Qwen3 tends to leak Chinese on long-context English-only prompts, especially mid-session.

**Fix:**
1. Confirm the bridge's per-turn sandwich enforcement is active. Static system prompts alone are not enough — the bridge must wrap every turn with explicit English+emoji instructions. This is the `_build_sandwich_prompt` logic in `bridge.py`.
2. Check that the bridge is actually being called (not bypassed). Tail the bridge logs while testing.
3. If the leak happens on the first turn, the ZeroClaw persona files may contain non-English text. Check `SOUL.md` and `IDENTITY.md` in `~/.zeroclaw/workspace/`.
4. As a last resort, the ASR may be mis-transcribing English as another language. Check the `ASR.FunASR.language` key in `data/.config.yaml` is set to `en` (not `auto`).

---

## Audio choppy or cutting out

**Symptom:** The robot responds but the audio is choppy, stuttery, or cuts off mid-sentence.

**Possible causes:**

- **WiFi signal.** The StackChan's ESP32-S3 is 2.4 GHz only. Check RSSI — anything below -70 dBm will cause packet loss on the WebSocket stream. Move the robot closer to the access point, or reduce 2.4 GHz interference.
- **WebSocket abnormal close.** Check xiaozhi-server logs for WS disconnect/reconnect events. The device will silently reconnect, but audio in flight is lost.
- **TTS chunk timing.** If using EdgeTTS (cloud), network jitter between the Docker host and Microsoft's edge servers can cause uneven audio delivery. Switching to Piper (local) eliminates this variable entirely.
- **Unraid CPU contention.** If other containers are competing for CPU during the ASR or TTS stages, audio processing can stall. Check `docker stats` on the Unraid host.

---

## Robot not responding after OTA / firmware update

**Symptom:** The StackChan boots and connects to WiFi, but never responds to voice. May show a face but no indication of listening.

**Fix:**
1. Check the bridge health endpoint: `curl http://<RPI_IP>:8080/health`. If the bridge is down, restart it.
2. Check xiaozhi-server logs: `docker logs -f xiaozhi-esp32-server` on Unraid. Look for connection attempts from the device.
3. Verify the device's OTA URL hasn't changed. After a firmware update, re-enter the OTA URL (`http://<UNRAID_IP>:8003/xiaozhi/ota/`) in the device's Advanced Options if needed.
4. Open the browser test page (`repo/main/xiaozhi-server/test/test_page.html`) and point it at `ws://<UNRAID_IP>:8000/xiaozhi/v1/`. If the browser page works but the device doesn't, it's a device-side configuration issue.

---

## ModuleNotFoundError on docker compose up

**Symptom:** The xiaozhi-server container starts but immediately fails with a Python `ModuleNotFoundError` in the logs.

**Cause:** Custom providers are not mounted correctly into the container. The volume mounts in `docker-compose.yml` map host-side files into specific paths inside the container. If the host path is wrong, the file doesn't arrive and the import fails.

**Fix:**
1. Check `docker logs xiaozhi-esp32-server` for the exact missing module name.
2. Verify the volume mounts in `docker-compose.yml` match the expected paths. The custom providers must land at:
   - `custom-providers/zeroclaw/` -> `/opt/xiaozhi-esp32-server/core/providers/llm/zeroclaw/`
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
1. Check bridge logs to see the raw response from ZeroClaw. The bridge has a `_ensure_emoji_prefix` fallback that prepends `😐` if no emoji is detected — if the response still has no emoji, the fallback isn't firing.
2. If the response has an emoji but the face doesn't change, it may be an unsupported emoji. Only the nine listed above are mapped to animations.
3. The three enforcement layers are: (a) ZeroClaw's agent prompt, (b) the `prompt:` key in `data/.config.yaml`, (c) the bridge fallback. If all three fail, something is fundamentally wrong with the response path.

---

## Servo snaps violently / startling head movement

**Symptom:** The robot's head jerks abruptly when changing position, instead of moving smoothly.

**Cause:** Known limitation. The current firmware does not implement a velocity or acceleration cap on servo commands. The feedback servos move at their maximum speed, which can be startling — especially in a household with kids.

**Workaround:** There is no software workaround at this time. This is tracked as a firmware-level fix. See [hardware.md](./hardware.md#safety-relevant-hardware-facts) for context.

---

## 5+ taps needed to enter voice mode

**Symptom:** After booting, the robot takes many taps on the display before it enters listening mode. Sometimes 5 or more.

**Cause:** Pre-existing firmware startup issue. The StackChan firmware's initialization sequence doesn't reliably register touch events until all subsystems are ready, but the display and touch controller come up before the audio subsystem and WiFi.

**Workaround:** Wait a few seconds after the face appears before tapping. If it still doesn't respond, try a single long press rather than repeated taps.

---

## Bridge unreachable / "(no response)" in the robot's voice

**Symptom:** The robot says something like "no response" or goes silent after you speak. xiaozhi-server logs show a failed HTTP POST to the bridge.

**Fix:**
1. Check bridge status on the RPi:
   - Bare metal: `systemctl status zeroclaw-bridge`
   - Docker: `docker ps | grep zeroclaw-bridge`
2. Test the health endpoint: `curl http://<RPI_IP>:8080/health`
3. If the bridge is running but unreachable, check firewall rules on the RPi. Port 8080 must be open for LAN traffic.
4. If the bridge crashes on startup, check logs for a ZeroClaw binary issue: the bridge spawns `zeroclaw acp` as a child process. If the binary is missing or the config is invalid, the bridge won't start.

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

- [README.md Troubleshooting](../README.md#troubleshooting) — the quick-reference version.
- [voice-pipeline.md](./voice-pipeline.md) — details on ASR, TTS, VAD tuning.
- [protocols.md](./protocols.md) — WebSocket and ACP wire format for debugging.
- [hardware.md](./hardware.md) — hardware specs and safety notes.

Last verified: 2026-04-24.
