"""dotty-behaviour FastAPI app entrypoint.

Run via uvicorn (`python -m uvicorn main:app …`). The Dockerfile pins
the invocation; in dev tests use ``fastapi.testclient.TestClient(app)``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import asyncio

import config
from consumers import (
    FaceIdentifiedRefresher,
    FaceLostAborter,
    PurrPlayer,
    SoundTurner,
    WakeWordTurner,
)
from dispatch import NarrativeLLMClient, XiaozhiAdminClient
from perception import PerceptionState
from routes import health as health_routes
from routes import perception as perception_routes


# Configure root logger early — uvicorn replaces handlers, this is just
# a fallback for direct-import contexts (pytest, REPL).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("dotty-behaviour")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("dotty-behaviour starting (version=%s)", config.VERSION)
    log.info(
        "config: port=%d xiaozhi=%s narrative_model=%s state_dir=%s log_dir=%s",
        config.PORT,
        config.XIAOZHI_HOST or "(disabled)",
        config.NARRATIVE_MODEL,
        config.STATE_DIR,
        config.LOG_DIR,
    )

    # Singleton perception state — bus + caches + per-device dicts.
    # Stored on app.state so routes/consumers can retrieve it via
    # FastAPI's Request.app.state.
    state = PerceptionState()
    app.state.perception = state

    # Singleton dispatch clients — outbound HTTP to xiaozhi-server's
    # /xiaozhi/admin/* surface and the llama-swap narrative LLM.
    xiaozhi = XiaozhiAdminClient(config.XIAOZHI_HOST, config.XIAOZHI_HTTP_PORT)
    narrative = NarrativeLLMClient(
        config.NARRATIVE_LLM_URL,
        config.NARRATIVE_MODEL,
        timeout_s=config.NARRATIVE_TIMEOUT_SEC,
    )
    app.state.xiaozhi = xiaozhi
    app.state.narrative = narrative

    # Filesystem prep — best-effort; missing bind mounts are an
    # operator error but the daemon should not crash before logging it.
    for path in (config.STATE_DIR, config.LOG_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("failed to create %s: %s", path, exc)

    # Spawn perception consumers — each is a long-lived asyncio task
    # subscribed to the bus. Optional consumers (e.g. wake-word turner
    # under WAKE_TURN_ENABLED=0) are skipped at construction time.
    consumers: list = [
        FaceLostAborter(
            state,
            xiaozhi,
            window_sec=config.FACE_LOST_ABORT_WINDOW_SEC,
            grace_sec=config.FACE_LOST_ABORT_GRACE_SEC,
        ),
        SoundTurner(
            state,
            xiaozhi,
            cooldown_sec=config.SOUND_TURN_COOLDOWN_SEC,
            yaw_deg=config.SOUND_TURN_YAW_DEG,
            speed=config.SOUND_TURN_SPEED,
            quiet_after_chat_sec=config.SOUND_TURN_QUIET_AFTER_CHAT_SEC,
        ),
        FaceIdentifiedRefresher(
            state,
            xiaozhi,
            interval_sec=config.FACE_IDENTITY_REFRESH_INTERVAL_SEC,
            ttl_sec=config.FACE_IDENTITY_TTL_SEC,
            quiet_after_lost_sec=config.FACE_IDENTITY_REFRESH_QUIET_SEC,
        ),
        PurrPlayer(
            state,
            xiaozhi,
            asset_path=config.PURR_AUDIO_PATH,
            cooldown_sec=config.PURR_COOLDOWN_SEC,
            duration_sec=config.PURR_DURATION_SEC,
        ),
    ]
    if config.WAKE_TURN_ENABLED:
        consumers.append(
            WakeWordTurner(
                state,
                xiaozhi,
                yaw_deg=config.WAKE_TURN_YAW_DEG,
                speed=config.WAKE_TURN_SPEED,
            )
        )
    else:
        log.info("wake-word turner disabled by WAKE_TURN_ENABLED=0")

    tasks = [
        asyncio.create_task(c.run(), name=type(c).__name__) for c in consumers
    ]
    app.state.consumer_tasks = tasks

    log.info(
        "dotty-behaviour ready on port %d (%d consumers running)",
        config.PORT,
        len(tasks),
    )
    try:
        yield
    finally:
        log.info("dotty-behaviour shutting down — cancelling consumers")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="dotty-behaviour",
    version=config.VERSION,
    description=(
        "Unraid-resident behaviour daemon for Dotty: perception event "
        "bus, 9 consumers, vision/audio explain, dashboard, greeter. "
        "Successor to RPi zeroclaw-bridge."
    ),
    lifespan=lifespan,
)
app.include_router(health_routes.router)
app.include_router(perception_routes.router)
