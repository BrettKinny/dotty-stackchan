"""Audio endpoints — POST /api/audio/explain.

Mirrors the vision route pattern: multipart upload → base64 →
AudioCaptionClient → cache under perception_state.audio_cache → fan
out a synthetic `audio_captioned` perception event so the dashboard
SSE feed refreshes without waiting for a polling tick.

Raw audio bytes are NOT cached — only the textual caption. Phase B
(firmware capture relay → security_cycle consumer) lands separately.
"""

from __future__ import annotations

import base64
import logging
import time
from time import perf_counter

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

import config
from dispatch import AudioCaptionClient
from perception import PerceptionEvent, PerceptionState

log = logging.getLogger("dotty-behaviour.routes.audio")


_AUDIO_FMT_BY_CONTENT_TYPE = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "opus",
    "audio/opus": "opus",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
}


def audio_format_from_upload(file: UploadFile) -> str:
    """Best-effort fmt guess from content-type or filename. Defaults
    to "wav" — what the firmware capture path will produce when it
    lands."""
    ct = (file.content_type or "").lower().strip()
    if ct in _AUDIO_FMT_BY_CONTENT_TYPE:
        return _AUDIO_FMT_BY_CONTENT_TYPE[ct]
    name = (file.filename or "").lower()
    for ext, fmt in (
        (".wav", "wav"),
        (".mp3", "mp3"),
        (".ogg", "opus"),
        (".opus", "opus"),
        (".flac", "flac"),
    ):
        if name.endswith(ext):
            return fmt
    return "wav"


def get_perception_state(request: Request) -> PerceptionState:
    state = getattr(request.app.state, "perception", None)
    if state is None:
        raise RuntimeError("PerceptionState not attached to app.state")
    return state


def get_audio_client(request: Request) -> AudioCaptionClient:
    client = getattr(request.app.state, "audio_caption", None)
    if client is None:
        raise RuntimeError("AudioCaptionClient not attached to app.state")
    return client


router = APIRouter()


@router.post("/api/audio/explain")
async def audio_explain(
    request: Request,
    question: str = Form("Describe what you hear."),
    file: UploadFile = File(...),
    state: PerceptionState = Depends(get_perception_state),
    client: AudioCaptionClient = Depends(get_audio_client),
) -> dict:
    device_id = request.headers.get("device-id", "unknown")
    audio_bytes = await file.read()
    fmt = audio_format_from_upload(file)
    log.info(
        "audio device=%s question=%s bytes=%d format=%s",
        device_id, question[:80], len(audio_bytes), fmt,
    )
    b64_audio = base64.b64encode(audio_bytes).decode("ascii")
    description = await client.caption(
        b64_audio,
        fmt,
        question,
        system_prompt=config.AUDIO_CAPTION_SYSTEM_PROMPT,
        timeout_s=config.AUDIO_CAPTION_TIMEOUT_SEC,
    )

    now_perf = perf_counter()
    now_wall = time.time()
    state.audio_cache[device_id] = {
        "description": description,
        "timestamp": now_perf,
        "wall_ts": now_wall,
        "question": question,
        "source": "audio_explain",
    }
    state.signal_audio_waiters(device_id)

    # Evict stale entries — same TTL cleanup pattern as vision.
    stale = [
        k
        for k, v in state.audio_cache.items()
        if now_perf - v.get("timestamp", 0) > config.AUDIO_CACHE_TTL_SEC
    ]
    for k in stale:
        state.audio_cache.pop(k, None)

    # Nudge the dashboard via the perception SSE feed so the new
    # caption shows up without a polling tick. Mirrors bridge.py.
    state.broadcast(
        PerceptionEvent(
            device_id=device_id,
            name="audio_captioned",
            data={
                "source": "audio_explain",
                "preview": description[:80],
            },
            ts=now_wall,
        )
    )

    log.info(
        "audio result device=%s desc=%s",
        device_id, description[:120],
    )
    return {"description": description}


@router.get("/api/audio/cache")
async def audio_cache_route(
    state: PerceptionState = Depends(get_perception_state),
) -> dict[str, dict]:
    """Per-device audio_cache (caption + timestamps).

    Consumed by the bridge dashboard's audio tile via the Tile 4 (#115)
    HTTP rewire. Returns the same fields the explain handler writes
    (description, timestamp, wall_ts, question, source)."""
    return state.audio_cache
