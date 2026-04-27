"""Perception consumer: text-only security capture loop.

When the firmware enters the ``security`` State, this consumer starts a
per-device interval timer. Each tick:

  1. Asks the firmware to take a still photo (via the existing
     ``self.camera.take_photo`` MCP tool relayed through xiaozhi-server).
     The image lands at the bridge's ``/api/vision/explain`` endpoint,
     gets run through the OpenRouter VLM, and the JPEG bytes are
     **discarded** — only the textual description survives.
  2. Asks the firmware to capture a short audio clip (via a hypothetical
     ``self.audio.capture_clip`` MCP tool). The clip is run through ASR
     (and an ambient classifier when available) and the raw bytes are
     **discarded** — only the transcript / classification labels survive.
  3. Appends one NDJSON line per cycle to
     ``$CONVO_LOG_DIR/security-YYYY-MM-DD.ndjson``. Mode 0600.

Persistence is deliberately text-only — see the user requirement that
"neither should be stored, just cached and run through a model to extract
content". A small in-memory ring buffer (``RECENT_CYCLES``) holds the
last ``RING_BUFFER_SIZE`` cycles so the dashboard can surface a recent-
events panel.

Firmware dependency notes
-------------------------
Photo capture: ``self.camera.take_photo`` is already shipped (used by
``receiveAudioHandle._capture_room_description_async``). The bridge
reaches it via a new ``/xiaozhi/admin/take-photo`` admin route on
xiaozhi-server. If that route is not yet present (404), the consumer
logs a one-shot warning and skips the cycle.

Audio capture: ``self.audio.capture_clip`` does **not** exist in the
firmware as of 2026-04-27. The consumer attempts the call anyway and
gracefully degrades to "photo only" cycles when the relay returns 404
or 503. Add the firmware tool + the relay route to enable the audio leg.

ASR: when the audio bytes are received via ``/api/audio/explain``, this
module forwards them to xiaozhi-server's existing SenseVoice provider
through a new admin route. Until that lands, the consumer records an
``audio_capture_pending`` error string in the NDJSON record.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("zeroclaw-bridge.security_watch")

# Pin fire-and-forget tasks so asyncio's weakref doesn't GC them
# mid-flight. Same pattern as purr_player / bridge.py.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro, *, name: str | None = None) -> asyncio.Task:
    t = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


# ---------------------------------------------------------------------------
# Tunables — env at import time so test fixtures can patch before import.
# ---------------------------------------------------------------------------

SECURITY_CAPTURE_INTERVAL_SEC: float = float(
    os.environ.get("SECURITY_CAPTURE_INTERVAL_SEC", "20")
)
SECURITY_AUDIO_DURATION_MS: int = int(
    os.environ.get("SECURITY_AUDIO_DURATION_MS", "5000")
)
SECURITY_VLM_PROMPT: str = os.environ.get(
    "SECURITY_VLM_PROMPT",
    (
        "Describe everything visible in this image. Note any people, "
        "movements, or items of interest. Be concise."
    ),
)
SECURITY_LOG_DIR: Path = Path(
    os.environ.get("CONVO_LOG_DIR", "/root/zeroclaw-bridge/logs")
)
SECURITY_VLM_TIMEOUT_SEC: float = float(
    os.environ.get("SECURITY_VLM_TIMEOUT_SEC", "20")
)
SECURITY_AUDIO_TIMEOUT_SEC: float = float(
    os.environ.get("SECURITY_AUDIO_TIMEOUT_SEC", "20")
)
RING_BUFFER_SIZE: int = int(os.environ.get("SECURITY_RING_BUFFER_SIZE", "60"))
LOCAL_TZ = ZoneInfo(os.environ.get("DOTTY_LOCAL_TZ", "Australia/Brisbane"))

_XIAOZHI_HOST: str = os.environ.get("XIAOZHI_HOST", "")
_XIAOZHI_HTTP_PORT: int = int(os.environ.get("XIAOZHI_OTA_PORT", "8003"))
_BRIDGE_INTERNAL_URL: str = os.environ.get(
    "BRIDGE_INTERNAL_URL", "http://127.0.0.1:8000"
)


# ---------------------------------------------------------------------------
# In-memory ring buffer — last N cycles for the dashboard to surface.
# ---------------------------------------------------------------------------

RECENT_CYCLES: "collections.deque[dict]" = collections.deque(maxlen=RING_BUFFER_SIZE)


def get_recent_cycles(limit: int | None = None) -> list[dict]:
    """Return the most-recent cycles (newest first), oldest dropped."""
    items = list(RECENT_CYCLES)
    items.reverse()
    if limit is not None:
        items = items[:limit]
    return items


# ---------------------------------------------------------------------------
# Vision-cache writer — bridge.py registers a setter so security cycles can
# push the captured JPEG + description into the SAME in-memory dict the
# /api/vision/explain pipeline uses. The dashboard's
# /ui/host/robot/photo/{mac} endpoint reads from that cache, so once the
# xiaozhi-server take-photo relay route lands and we start receiving photo
# bytes here, they will surface in the dashboard's "Latest view" thumbnail
# automatically. Today this is a no-op because dispatch_take_photo always
# returns 404 — the writer registers a hook that will start firing the day
# the relay does.
#
# Hard constraint: only the JPEG bytes go into the in-memory dict. No disk
# persistence path is added (the bridge's existing _vision_cache is purely
# in-memory + 60s TTL).
# ---------------------------------------------------------------------------

# Callable signature: writer(device_id, *, jpeg_bytes, description, source)
VisionCacheWriterFn = Callable[..., None]
_vision_cache_writer: Optional[VisionCacheWriterFn] = None


def set_vision_cache_writer(writer: VisionCacheWriterFn | None) -> None:
    """Register the bridge-side hook that mutates ``bridge._vision_cache``.

    Exposed as a setter (rather than importing _vision_cache directly) to
    keep this module decoupled from bridge.py — same seam that
    poll_vision_description uses for the read side.
    """
    global _vision_cache_writer
    _vision_cache_writer = writer


def _publish_vision_capture(
    device_id: str,
    *,
    jpeg_bytes: bytes,
    description: str,
    source: str = "security_capture",
) -> None:
    """Push a security-cycle photo into the bridge's in-memory vision cache.

    Safe to call when no writer is registered (no-op). Never raises — a
    failed cache write must not break the security cycle.
    """
    writer = _vision_cache_writer
    if writer is None or not jpeg_bytes:
        return
    try:
        writer(
            device_id,
            jpeg_bytes=jpeg_bytes,
            description=description,
            source=source,
        )
    except Exception:
        log.warning("vision cache writer raised (ignored)", exc_info=True)


# ---------------------------------------------------------------------------
# Firmware MCP relay — outbound dispatchers
# ---------------------------------------------------------------------------

# Set to True after the first 404 / connection error so we only nag once
# in the journal. Cleared on the next successful dispatch (lets the
# operator confirm the route was added).
_PHOTO_RELAY_MISSING_LOGGED: bool = False
_AUDIO_RELAY_MISSING_LOGGED: bool = False


async def dispatch_take_photo(
    device_id: str,
    *,
    question: str,
    xiaozhi_host: str | None = None,
    xiaozhi_port: int | None = None,
) -> bool:
    """POST to the xiaozhi-server admin route that relays a
    ``self.camera.take_photo`` MCP frame to the named device.

    The image arrives back at the bridge via the firmware's normal HTTPS
    POST to ``/api/vision/explain`` (same path used by
    ``receiveAudioHandle._capture_room_description_async``). The caller
    is expected to long-poll the bridge's vision cache after this returns.

    Returns True on 2xx, False otherwise. Never raises.

    NOTE: ``/xiaozhi/admin/take-photo`` is the assumed route name; if the
    xiaozhi-server side is not yet patched to expose it, the call returns
    False and a one-shot warning is logged. Adding the relay endpoint
    mirrors the pattern of ``set-state`` / ``set-toggle`` in
    ``custom-providers/xiaozhi-patches/http_server.py``.
    """
    global _PHOTO_RELAY_MISSING_LOGGED
    host = xiaozhi_host if xiaozhi_host is not None else _XIAOZHI_HOST
    port = xiaozhi_port if xiaozhi_port is not None else _XIAOZHI_HTTP_PORT
    if not host:
        log.warning("security: XIAOZHI_HOST not set; cannot dispatch take_photo")
        return False

    import requests as _req

    url = f"http://{host}:{port}/xiaozhi/admin/take-photo"
    payload = {"device_id": device_id, "question": question}

    def _post() -> bool:
        global _PHOTO_RELAY_MISSING_LOGGED
        try:
            r = _req.post(url, json=payload, timeout=3)
            if r.status_code == 404:
                if not _PHOTO_RELAY_MISSING_LOGGED:
                    log.warning(
                        "security: /xiaozhi/admin/take-photo not present on "
                        "xiaozhi-server (404). Photo capture skipped this "
                        "cycle. Add the relay route to enable security "
                        "photo capture; see security_watch.py for the spec."
                    )
                    _PHOTO_RELAY_MISSING_LOGGED = True
                return False
            if r.status_code >= 400:
                log.warning(
                    "security take-photo %s: %s",
                    r.status_code, r.text[:200],
                )
                return False
            _PHOTO_RELAY_MISSING_LOGGED = False
            return True
        except Exception as exc:
            log.warning("security take-photo failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


async def dispatch_capture_audio(
    device_id: str,
    *,
    duration_ms: int,
    xiaozhi_host: str | None = None,
    xiaozhi_port: int | None = None,
) -> bool:
    """POST to the xiaozhi-server admin route that relays a
    ``self.audio.capture_clip`` MCP frame to the named device.

    The captured PCM/Opus clip is expected to arrive back at the bridge
    via ``POST /api/audio/explain`` (analogous to ``/api/vision/explain``).

    Returns True on 2xx, False otherwise. As of 2026-04-27 the firmware
    does not implement ``self.audio.capture_clip`` — this dispatcher is
    expected to fail gracefully (404 or 503) and the security cycle
    continues with photo-only data.
    """
    global _AUDIO_RELAY_MISSING_LOGGED
    host = xiaozhi_host if xiaozhi_host is not None else _XIAOZHI_HOST
    port = xiaozhi_port if xiaozhi_port is not None else _XIAOZHI_HTTP_PORT
    if not host:
        log.warning("security: XIAOZHI_HOST not set; cannot dispatch capture_audio")
        return False

    import requests as _req

    url = f"http://{host}:{port}/xiaozhi/admin/capture-audio"
    payload = {"device_id": device_id, "duration_ms": duration_ms}

    def _post() -> bool:
        global _AUDIO_RELAY_MISSING_LOGGED
        try:
            r = _req.post(url, json=payload, timeout=3)
            if r.status_code == 404:
                if not _AUDIO_RELAY_MISSING_LOGGED:
                    log.warning(
                        "security: audio capture MCP tool / relay not yet "
                        "present in firmware + xiaozhi-server (404). "
                        "Photo-only this cycle. Tracked as a deferred "
                        "follow-up: add self.audio.capture_clip MCP tool "
                        "in firmware and /xiaozhi/admin/capture-audio "
                        "relay route in xiaozhi-patches/http_server.py."
                    )
                    _AUDIO_RELAY_MISSING_LOGGED = True
                return False
            if r.status_code >= 400:
                log.warning(
                    "security capture-audio %s: %s",
                    r.status_code, r.text[:200],
                )
                return False
            _AUDIO_RELAY_MISSING_LOGGED = False
            return True
        except Exception as exc:
            log.warning("security capture-audio failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


# ---------------------------------------------------------------------------
# Vision-cache poll helpers — mirror the receiveAudioHandle pattern.
# ---------------------------------------------------------------------------

async def poll_vision_description(
    device_id: str,
    *,
    poll_url_base: str | None = None,
    timeout_sec: float | None = None,
) -> Optional[str]:
    """Long-poll the bridge's own ``/api/vision/latest/{device_id}`` until
    a description appears. Returns the description string on success or
    None on miss/timeout. Never raises.

    This intentionally hits the bridge over loopback HTTP (rather than
    reaching into the in-process ``_vision_cache``) so the call stays
    decoupled from bridge.py internals — the public HTTP contract is
    the seam, same as what xiaozhi-server already polls.
    """
    base = poll_url_base if poll_url_base is not None else _BRIDGE_INTERNAL_URL
    timeout = timeout_sec if timeout_sec is not None else SECURITY_VLM_TIMEOUT_SEC
    url = f"{base.rstrip('/')}/api/vision/latest/{device_id}"

    import requests as _req

    def _get() -> Optional[str]:
        try:
            r = _req.get(url, timeout=timeout)
        except Exception as exc:
            log.warning("security vision poll failed: %s", exc)
            return None
        if r.status_code != 200:
            log.info(
                "security vision poll miss device=%s status=%s",
                device_id, r.status_code,
            )
            return None
        try:
            body = r.json() or {}
        except Exception:
            log.warning("security vision poll: non-JSON body")
            return None
        desc = (body.get("description") or "").strip()
        return desc or None

    return await asyncio.to_thread(_get)


# ---------------------------------------------------------------------------
# NDJSON persistence (text-only — no media bytes)
# ---------------------------------------------------------------------------

def _ensure_log_dir(log_dir: Path) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_dir.chmod(0o700)
    except OSError:
        log.warning("security log dir creation failed: %s", log_dir)


def write_security_record(
    record: dict,
    *,
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Append one NDJSON line to ``security-YYYY-MM-DD.ndjson``.

    The schema is:
      {
        "ts": iso8601,
        "device": mac,
        "photo_desc": str,
        "audio_transcript": str|null,
        "audio_classification": str|null,
        "errors": [str],
      }

    Returns the file path on success, or None on failure. Never raises.
    """
    target_dir = log_dir if log_dir is not None else SECURITY_LOG_DIR
    _ensure_log_dir(target_dir)
    when = now if now is not None else datetime.now(LOCAL_TZ)
    path = target_dir / f"security-{when.strftime('%Y-%m-%d')}.ndjson"
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        path.chmod(0o600)
        return path
    except Exception:
        log.warning("security log write failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Per-device timer state + run loop
# ---------------------------------------------------------------------------

# Map device_id -> the running asyncio.Task for that device's capture loop.
# Cleared when the device leaves the `security` state or on cancellation.
_DEVICE_TIMERS: dict[str, asyncio.Task] = {}


async def _run_capture_cycle(
    device_id: str,
    *,
    photo_dispatch: Callable[..., Awaitable[bool]] = dispatch_take_photo,
    audio_dispatch: Callable[..., Awaitable[bool]] = dispatch_capture_audio,
    vision_poll: Callable[..., Awaitable[Optional[str]]] = poll_vision_description,
    write_record: Callable[..., Path | None] = write_security_record,
    vlm_prompt: str | None = None,
    audio_duration_ms: int | None = None,
) -> dict:
    """Run one capture cycle for ``device_id``. Returns the record dict
    (also persisted to NDJSON + ring buffer)."""
    prompt = vlm_prompt if vlm_prompt is not None else SECURITY_VLM_PROMPT
    audio_dur = (
        audio_duration_ms if audio_duration_ms is not None
        else SECURITY_AUDIO_DURATION_MS
    )
    errors: list[str] = []
    photo_desc: str = ""
    audio_transcript: Optional[str] = None
    audio_classification: Optional[str] = None

    # ----- Photo leg ---------------------------------------------------
    photo_ok = await photo_dispatch(device_id, question=prompt)
    if not photo_ok:
        errors.append("photo_dispatch_failed")
    else:
        # Bridge's /api/vision/latest long-polls until the device POSTS
        # the JPEG and the VLM returns a description; ~15 s budget.
        desc = await vision_poll(device_id)
        if desc:
            photo_desc = desc
            # Tag the bridge's vision_cache entry with source=security_capture
            # so the dashboard can tell apart room_view captures (face-
            # triggered) from security captures. The JPEG bytes are
            # already sitting in _vision_cache from the device's POST to
            # /api/vision/explain — we don't have them in this process,
            # so we pass empty bytes; the writer is a no-op when bytes
            # are empty (see _publish_vision_capture). The source-tagging
            # is therefore done by the bridge-side writer at the time
            # the entry is first written. For now this hook is the seam
            # for future enhancements (e.g. echoing the desc back into
            # the cache to coalesce with the room_view path).
            _publish_vision_capture(
                device_id,
                jpeg_bytes=b"",
                description=photo_desc,
                source="security_capture",
            )
        else:
            errors.append("photo_poll_miss")

    # ----- Audio leg ---------------------------------------------------
    audio_ok = await audio_dispatch(device_id, duration_ms=audio_dur)
    if not audio_ok:
        # Distinct error code so downstream consumers can tell "firmware
        # tool not yet implemented" apart from "fired but ASR returned
        # nothing".
        errors.append("audio_capture_pending")
    else:
        # TODO: poll a /api/audio/latest/{device_id} endpoint analogous
        # to the vision one. Until /api/audio/explain exists on the
        # bridge AND firmware can deliver the clip, we record the OK
        # dispatch but flag the missing poll surface.
        errors.append("audio_poll_endpoint_pending")

    record: dict[str, Any] = {
        "ts": datetime.now(LOCAL_TZ).isoformat(),
        "device": device_id,
        "photo_desc": photo_desc,
        "audio_transcript": audio_transcript,
        "audio_classification": audio_classification,
        "errors": errors,
    }
    write_record(record)
    RECENT_CYCLES.append(record)
    log.info(
        "security cycle device=%s desc_len=%d errors=%s",
        device_id, len(photo_desc), errors,
    )
    return record


async def _device_capture_loop(
    device_id: str,
    *,
    interval_sec: float | None = None,
    cycle_runner: Callable[..., Awaitable[dict]] = _run_capture_cycle,
) -> None:
    """Per-device cycle driver: wakes on interval, runs one capture
    cycle, sleeps. Cancelled by ``stop_device_timer``."""
    period = interval_sec if interval_sec is not None else SECURITY_CAPTURE_INTERVAL_SEC
    log.info(
        "security capture loop started device=%s interval=%.0fs",
        device_id, period,
    )
    try:
        # First cycle fires immediately so the operator gets a record
        # within the first second of entering security state, not after
        # a 20 s wait. Subsequent cycles honour the interval.
        while True:
            try:
                await cycle_runner(device_id)
            except Exception:
                log.exception("security cycle crashed device=%s", device_id)
            await asyncio.sleep(period)
    except asyncio.CancelledError:
        log.info("security capture loop cancelled device=%s", device_id)
        raise


def start_device_timer(
    device_id: str,
    *,
    interval_sec: float | None = None,
    cycle_runner: Callable[..., Awaitable[dict]] | None = None,
) -> asyncio.Task:
    """Idempotently start the capture loop for ``device_id``. If a loop
    is already running for the device, returns the existing task."""
    existing = _DEVICE_TIMERS.get(device_id)
    if existing is not None and not existing.done():
        return existing
    runner = cycle_runner if cycle_runner is not None else _run_capture_cycle
    t = _spawn(
        _device_capture_loop(
            device_id,
            interval_sec=interval_sec,
            cycle_runner=runner,
        ),
        name=f"security_capture[{device_id}]",
    )
    _DEVICE_TIMERS[device_id] = t
    return t


def stop_device_timer(device_id: str) -> bool:
    """Cancel the per-device capture loop. Returns True if a timer was
    cancelled, False if none was running."""
    t = _DEVICE_TIMERS.pop(device_id, None)
    if t is None or t.done():
        return False
    t.cancel()
    return True


def stop_all_timers() -> None:
    """Cancel every active per-device timer. Used at shutdown."""
    for device_id in list(_DEVICE_TIMERS):
        stop_device_timer(device_id)


# ---------------------------------------------------------------------------
# Perception subscriber — this is what bridge.py schedules.
# ---------------------------------------------------------------------------

SubscribeFn = Callable[[], "asyncio.Queue[dict]"]
UnsubscribeFn = Callable[["asyncio.Queue[dict]"], None]


async def run_security_consumer(
    subscribe_fn: SubscribeFn,
    unsubscribe_fn: UnsubscribeFn,
    *,
    interval_sec: float | None = None,
    cycle_runner: Callable[..., Awaitable[dict]] | None = None,
) -> None:
    """Subscribe to perception events; on ``state_changed`` toggle the
    per-device capture loop in/out of life.

    Mirrors the structure of ``_perception_face_greeter`` /
    ``_perception_purr_player`` in bridge.py — accepts the
    subscribe/unsubscribe pair so tests can inject a fake queue without
    touching the real perception bus.
    """
    log.info(
        "security capture consumer started (interval=%.0fs audio_dur=%dms prompt=%r)",
        SECURITY_CAPTURE_INTERVAL_SEC, SECURITY_AUDIO_DURATION_MS,
        SECURITY_VLM_PROMPT[:80],
    )
    _ensure_log_dir(SECURITY_LOG_DIR)
    q = subscribe_fn()
    try:
        while True:
            event = await q.get()
            if event.get("name") != "state_changed":
                continue
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue
            data = event.get("data") or {}
            new_state = (data.get("state") or "").strip().lower()
            if new_state == "security":
                start_device_timer(
                    device_id,
                    interval_sec=interval_sec,
                    cycle_runner=cycle_runner,
                )
                log.info("security: started capture loop for device=%s", device_id)
            else:
                if stop_device_timer(device_id):
                    log.info(
                        "security: stopped capture loop for device=%s "
                        "(new state=%s)", device_id, new_state,
                    )
    except asyncio.CancelledError:
        log.info("security capture consumer cancelled")
        stop_all_timers()
        raise
    except Exception:
        log.exception("security capture consumer crashed")
        stop_all_timers()
    finally:
        unsubscribe_fn(q)
