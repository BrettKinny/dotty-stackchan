"""Perception consumer: cat-purr audio on head_pet_started events.

Extracted from bridge.py for testability. The parent bridge.py imports
``run_purr_consumer`` and schedules it alongside the perception event bus.

Environment variables (read at import time so test fixtures can patch
``os.environ`` before importing this module):

  PURR_AUDIO_PATH     — path to the pre-rendered purr clip
                        (default: bridge/assets/purr.opus)
  PURR_COOLDOWN_SEC   — per-device repeat-suppression window in seconds
                        (default: 5)
  PURR_DURATION_SEC   — approximate playback length; used to extend
                        ``last_chat_t`` so the sound-localiser stays quiet
                        during playback (default: 2.0)
  UNRAID_HOST         — hostname/IP of the xiaozhi-server HTTP admin API
                        (no default; empty string disables dispatch)
  UNRAID_OTA_PORT     — xiaozhi-server HTTP port (default: 8003)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("zeroclaw-bridge.purr_player")

# Pin fire-and-forget tasks so asyncio's weakref doesn't GC them
# mid-flight. See bridge.py for the same pattern in production hot path.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro, *, name: str | None = None) -> asyncio.Task:
    t = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t

PURR_AUDIO_PATH: Path = Path(
    os.environ.get("PURR_AUDIO_PATH", "bridge/assets/purr.opus")
)
PURR_COOLDOWN_SEC: float = float(os.environ.get("PURR_COOLDOWN_SEC", "5"))
PURR_DURATION_SEC: float = float(os.environ.get("PURR_DURATION_SEC", "2.0"))

_XIAOZHI_HOST: str = os.environ.get("UNRAID_HOST", "")
_XIAOZHI_HTTP_PORT: int = int(os.environ.get("UNRAID_OTA_PORT", "8003"))


async def dispatch_purr_audio(
    device_id: str,
    *,
    purr_path: Path | None = None,
    unraid_host: str | None = None,
    unraid_port: int | None = None,
) -> bool:
    """POST the purr asset path to xiaozhi-server's /play-asset admin route.

    Returns True on 2xx, False on any failure. Never raises.

    Keyword overrides (purr_path, unraid_host, unraid_port) allow tests to
    supply controlled values without patching environment variables.
    """
    path = purr_path if purr_path is not None else PURR_AUDIO_PATH
    host = unraid_host if unraid_host is not None else _XIAOZHI_HOST
    port = unraid_port if unraid_port is not None else _XIAOZHI_HTTP_PORT

    if not host:
        log.warning("purr: UNRAID_HOST not set; cannot reach xiaozhi-server")
        return False
    if not path.exists():
        log.warning(
            "purr: asset missing at %s (drop a purr.opus to enable; "
            "see bridge/assets/README.md)",
            path,
        )
        return False

    import requests as _req

    url = f"http://{host}:{port}/xiaozhi/admin/play-asset"
    payload = {"device_id": device_id, "asset": str(path)}

    def _post() -> bool:
        try:
            r = _req.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning(
                    "purr play-asset %s: %s", r.status_code, r.text[:200]
                )
                return False
            return True
        except Exception as exc:
            log.warning("purr play-asset failed: %s", exc)
            return False

    try:
        return await asyncio.to_thread(_post)
    except Exception:
        log.exception("purr dispatch raised")
        return False


SubscribeFn = Callable[[], "asyncio.Queue[dict]"]  # type: ignore[type-arg]
DispatchFn = Callable[[str], Awaitable[bool]]


async def run_purr_consumer(
    subscribe_fn: SubscribeFn,
    perception_state: dict,
    *,
    cooldown_sec: float | None = None,
    duration_sec: float | None = None,
    dispatch_fn: DispatchFn | None = None,
) -> None:
    """Subscribe to the perception event bus and purr on head_pet_started.

    Each ``head_pet_started`` event dispatches purr audio for the
    originating device, subject to a per-device cooldown. After dispatch,
    ``last_chat_t`` in ``perception_state`` is extended by ``duration_sec``
    so the sound-localiser (``_perception_sound_turner``) skips head-turn
    commands while the purr plays.

    Parameters
    ----------
    subscribe_fn:
        Zero-argument callable that returns an ``asyncio.Queue`` delivering
        perception event dicts (keys: ``name``, ``device_id``, ``ts``).
    perception_state:
        Shared per-device state dict; mutated in place.
    cooldown_sec:
        Per-device cooldown between purrs. Defaults to PURR_COOLDOWN_SEC.
    duration_sec:
        Purr playback duration for ``last_chat_t`` suppression.
        Defaults to PURR_DURATION_SEC.
    dispatch_fn:
        Async callable ``(device_id) -> bool`` that sends the audio.
        Defaults to ``dispatch_purr_audio``. Inject a mock in tests.
    """
    cooldown = cooldown_sec if cooldown_sec is not None else PURR_COOLDOWN_SEC
    duration = duration_sec if duration_sec is not None else PURR_DURATION_SEC
    dispatch = dispatch_fn if dispatch_fn is not None else dispatch_purr_audio

    log.info(
        "purr consumer started (cooldown=%.0fs asset=%s)", cooldown, PURR_AUDIO_PATH
    )
    q = subscribe_fn()
    try:
        while True:
            event = await q.get()
            if event.get("name") != "head_pet_started":
                continue
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue
            now = float(event.get("ts") or time.time())
            state = perception_state.setdefault(device_id, {})
            last_purr = state.get("last_purr_t", 0.0)
            if now - last_purr < cooldown:
                continue
            state["last_purr_t"] = now
            state["last_chat_t"] = now + duration
            log.info("head_pet_started → purr: device=%s", device_id)
            _spawn(dispatch(device_id), name="purr_dispatch")
    except asyncio.CancelledError:
        log.info("purr consumer cancelled")
        raise
    except Exception:
        log.exception("purr consumer crashed")
