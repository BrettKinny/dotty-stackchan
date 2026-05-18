"""Vision endpoints — POST /api/vision/explain + GET /api/vision/latest.

The explain endpoint accepts a JPEG upload, base64-encodes it, asks
the VLM to describe it (using the kid-mode-aware system prompt), and
caches the description under perception_state.vision_cache[device_id].
The latest endpoint blocks until a fresh result lands.

Wire-compatible with bridge.py's /api/vision/{explain,latest/...} so
xiaozhi-patches can retarget by URL swap. The room_view roster path
(opt-in via the _ROOM_VIEW_SENTINEL question + household roster
substitution + face_recognized broadcast) is intentionally NOT ported
in this slice — it depends on the household registry which lands in
a later slice. When household lands, the room_view path lights up by
checking `getattr(request.app.state, "household", None)`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from time import perf_counter

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

import config
from dispatch import VLMClient
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.routes.vision")


def get_perception_state(request: Request) -> PerceptionState:
    state = getattr(request.app.state, "perception", None)
    if state is None:
        raise RuntimeError("PerceptionState not attached to app.state")
    return state


def get_vlm_client(request: Request) -> VLMClient:
    vlm = getattr(request.app.state, "vlm", None)
    if vlm is None:
        raise RuntimeError("VLMClient not attached to app.state")
    return vlm


def get_kid_mode(request: Request) -> bool:
    """Live kid-mode reader. Set on app.state by the kid-mode toggle
    handler (deferred slice). Defaults to False until the dashboard
    plumbing lands."""
    return bool(getattr(request.app.state, "kid_mode", False))


router = APIRouter()


@router.post("/api/vision/explain")
async def vision_explain(
    request: Request,
    question: str = Form("What do you see?"),
    file: UploadFile = File(...),
    state: PerceptionState = Depends(get_perception_state),
    vlm: VLMClient = Depends(get_vlm_client),
    kid_mode: bool = Depends(get_kid_mode),
) -> dict:
    device_id = request.headers.get("device-id", "unknown")
    jpeg_bytes = await file.read()
    log.info(
        "vision device=%s question=%s bytes=%d",
        device_id, question[:80], len(jpeg_bytes),
    )
    b64_image = base64.b64encode(jpeg_bytes).decode("ascii")

    system_prompt = config.build_vision_system_prompt(kid_mode)
    description = await vlm.describe_image(
        b64_image,
        question,
        system_prompt=system_prompt,
        timeout_s=config.VISION_TIMEOUT_SEC,
    )

    now_perf = perf_counter()
    now_wall = time.time()
    state.vision_cache[device_id] = {
        "description": description,
        "timestamp": now_perf,
        "wall_ts": now_wall,
        "jpeg_bytes": jpeg_bytes,
        "question": question,
        "room_match_person_id": None,
        "source": "v1",
    }
    state.signal_vision_waiters(device_id)

    # Evict stale cache entries (other devices) so the cache doesn't
    # grow unbounded on a long-running daemon.
    stale = [
        k
        for k, v in state.vision_cache.items()
        if now_perf - v.get("timestamp", 0) > config.VISION_CACHE_TTL_SEC
    ]
    for k in stale:
        state.vision_cache.pop(k, None)

    log.info(
        "vision result device=%s desc=%s",
        device_id, description[:120],
    )
    return {"description": description}


@router.get("/api/vision/latest/{device_id}")
async def vision_latest(
    device_id: str,
    state: PerceptionState = Depends(get_perception_state),
):
    # Drop any stale entry first so the waiter won't immediately
    # return last-turn's cache.
    state.vision_cache.pop(device_id, None)
    event = state.register_vision_waiter(device_id)
    try:
        await asyncio.wait_for(event.wait(), timeout=15.0)
        entry = state.vision_cache.get(device_id)
        if entry:
            return {
                "description": entry["description"],
                "room_match_person_id": entry.get("room_match_person_id"),
            }
        return JSONResponse(
            status_code=500,
            content={"error": "vision processing failed"},
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=404,
            content={"error": "no vision result in time"},
        )
    finally:
        state.unregister_vision_waiter(device_id, event)
