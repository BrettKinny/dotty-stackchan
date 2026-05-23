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
    DanceReflector,
    FaceGreeter,
    FaceIdentifiedRefresher,
    FaceLostAborter,
    IdlePhotographer,
    PurrPlayer,
    SceneSynthesisLoop,
    SecurityCycle,
    SleepDreamer,
    SoundTurner,
    WakeWordTurner,
)
from dispatch import (
    AudioCaptionClient,
    NarrativeLLMClient,
    VLMClient,
    XiaozhiAdminClient,
)
from greeter import CalendarFacade, ProactiveGreeter
from household import HouseholdRegistry
from logs import NdjsonWriter
from perception import PerceptionState
import re

from calendar_ import CalendarCache
from calendar_.poll import CalendarPollLoop
from routes import audio as audio_routes
from routes import calendar as calendar_routes
from routes import health as health_routes
from routes import perception as perception_routes
from routes import scene_synthesis as scene_synthesis_routes
from routes import vision as vision_routes
from routes import voice as voice_routes


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
    # /xiaozhi/admin/* surface, the llama-swap narrative LLM, and the
    # OpenRouter VLM.
    xiaozhi = XiaozhiAdminClient(config.XIAOZHI_HOST, config.XIAOZHI_HTTP_PORT)
    narrative = NarrativeLLMClient(
        config.NARRATIVE_LLM_URL,
        config.NARRATIVE_MODEL,
        timeout_s=config.NARRATIVE_TIMEOUT_SEC,
    )
    vlm = VLMClient(
        config.VLM_API_URL,
        config.VLM_MODEL,
        api_key=config.VLM_API_KEY,
        timeout_s=config.VISION_TIMEOUT_SEC,
    )
    audio_caption = AudioCaptionClient(
        config.AUDIO_CAPTION_API_URL,
        config.AUDIO_CAPTION_MODEL,
        api_key=config.AUDIO_CAPTION_API_KEY,
        timeout_s=config.AUDIO_CAPTION_TIMEOUT_SEC,
    )
    app.state.xiaozhi = xiaozhi
    app.state.narrative = narrative
    app.state.vlm = vlm
    app.state.audio_caption = audio_caption

    # Household registry — YAML-backed, hot-reload on mtime change.
    # Empty when household.yaml is missing (valid state — resolves
    # everyone to `_household`).
    household = HouseholdRegistry(path=config.HOUSEHOLD_YAML_PATH)
    app.state.household = household

    # Weather + calendar cache. Compile the person-prefix regex once
    # (with fallback to default if the env override is invalid).
    try:
        person_prefix_re = re.compile(config.CALENDAR_PERSON_PREFIX_RE)
    except re.error:
        log.warning(
            "invalid CALENDAR_PERSON_PREFIX_RE=%r; using default",
            config.CALENDAR_PERSON_PREFIX_RE,
        )
        person_prefix_re = re.compile(
            r"^\s*\[(?P<person>[A-Za-z][A-Za-z0-9_-]{0,31})\]\s*(?P<rest>.+)$"
        )
    calendar_cache = CalendarCache()
    app.state.calendar_cache = calendar_cache
    app.state.calendar_person_prefix_re = person_prefix_re
    # kid-mode default — flipped by the dashboard's kid-mode toggle
    # (deferred slice). vision_explain reads this via get_kid_mode().
    app.state.kid_mode = False

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

    if config.DREAMER_ENABLED:
        consumers.append(
            SleepDreamer(
                state,
                narrative,
                NdjsonWriter(config.LOG_DIR, "dreams", config.LOCAL_TZ),
                window_seconds=config.DREAM_WINDOW_SECONDS,
                count_per_night=config.DREAM_COUNT_PER_NIGHT,
                inspirations=config.DREAM_INSPIRATIONS,
            )
        )
    else:
        log.info("sleep dreamer disabled by DREAMER_ENABLED=0")

    if config.DANCE_REFLECTOR_ENABLED:
        consumers.append(
            DanceReflector(
                state,
                narrative,
                NdjsonWriter(config.LOG_DIR, "dances", config.LOCAL_TZ),
            )
        )
    else:
        log.info("dance reflector disabled by DANCE_REFLECTOR_ENABLED=0")

    if config.IDLE_PHOTOGRAPHER_ENABLED:
        consumers.append(
            IdlePhotographer(
                state,
                xiaozhi,
                NdjsonWriter(config.LOG_DIR, "perception", config.LOCAL_TZ),
                sleep_min_sec=config.IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC,
                sleep_max_sec=config.IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC,
                result_wait_sec=config.IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC,
                notable_jaccard=config.IDLE_PHOTOGRAPHER_NOTABLE_JACCARD,
                question=config.IDLE_WANDER_PROMPT,
            )
        )
    else:
        log.info("idle photographer disabled by IDLE_PHOTOGRAPHER_ENABLED=0")

    if config.SCENE_SYNTHESIS_ENABLED:
        consumers.append(
            SceneSynthesisLoop(
                state,
                NdjsonWriter(
                    config.LOG_DIR, "scene-synthesis", config.LOCAL_TZ
                ),
                interval_sec=config.SCENE_SYNTHESIS_INTERVAL_SEC,
                min_gap_sec=config.SCENE_SYNTHESIS_MIN_GAP_SEC,
                trigger_events=config.SCENE_SYNTHESIS_TRIGGER_EVENTS,
                trigger_states=config.SCENE_SYNTHESIS_TRIGGER_STATES,
                vision_ttl_sec=config.VISION_CACHE_TTL_SEC,
                audio_ttl_sec=config.AUDIO_CACHE_TTL_SEC,
                face_identity_ttl_sec=config.FACE_IDENTITY_TTL_SEC,
                tz=config.LOCAL_TZ,
            )
        )
    else:
        log.info("scene synthesis loop disabled by SCENE_SYNTHESIS_ENABLED=0")

    consumers.append(
        FaceGreeter(
            state,
            xiaozhi,
            household,
            bare_greet_text=config.FACE_GREET_TEXT,
            bare_min_interval_sec=config.FACE_GREET_MIN_INTERVAL_SEC,
            bare_hour_start=config.FACE_GREET_HOUR_START,
            bare_hour_end=config.FACE_GREET_HOUR_END,
            name_template=config.FACE_NAME_GREET_TEMPLATE,
            name_min_interval_sec=config.FACE_NAME_GREET_MIN_INTERVAL_SEC,
            name_quiet_after_chat_sec=config.FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC,
            tz=config.LOCAL_TZ,
        )
    )

    if config.SECURITY_CYCLE_ENABLED:
        security = SecurityCycle(
            state,
            xiaozhi,
            NdjsonWriter(config.LOG_DIR, "security", config.LOCAL_TZ),
            interval_sec=config.SECURITY_CAPTURE_INTERVAL_SEC,
            audio_duration_ms=config.SECURITY_AUDIO_DURATION_MS,
            vlm_prompt=config.SECURITY_VLM_PROMPT,
            vlm_wait_sec=config.SECURITY_VLM_WAIT_SEC,
            ring_buffer_size=config.SECURITY_RING_BUFFER_SIZE,
        )
        # Surface to the dashboard via app.state so the recent-cycles
        # panel can read from it.
        app.state.security_cycle = security
        consumers.append(security)
    else:
        log.info("security cycle disabled by SECURITY_CYCLE_ENABLED=0")

    # Calendar poll loop alongside the consumers. No-op when no
    # calendars are configured (CALENDAR_IDS empty).
    if config.CALENDAR_IDS:
        consumers.append(
            CalendarPollLoop(
                calendar_cache,
                weather_location=config.WEATHER_LOCATION,
                weather_ttl_sec=config.WEATHER_TTL_SEC,
                calendar_ids=config.CALENDAR_IDS,
                calendar_ttl_sec=config.CALENDAR_TTL_SEC,
                calendar_sa_path=config.CALENDAR_SA_PATH,
                gws_bin=config.GWS_BIN,
                local_tz=config.LOCAL_TZ,
                household_bucket=config.CALENDAR_HOUSEHOLD_BUCKET,
                person_prefix_re=person_prefix_re,
            )
        )
    else:
        log.info("calendar disabled (CALENDAR_ID env empty)")

    # Layer 6 proactive greeter — narrative LLM + household-aware
    # contextual greetings on face_recognized events. Honours
    # GREETER_ENABLED.
    async def _greeter_llm(prompt: str) -> str:
        out = await narrative.chat(prompt, system_prompt="")
        return out or ""

    async def _greeter_tts(device_id: str, text: str) -> None:
        await xiaozhi.say(device_id, text)

    greeter = ProactiveGreeter(
        state,
        _greeter_llm,
        CalendarFacade(
            calendar_cache,
            household_bucket=config.CALENDAR_HOUSEHOLD_BUCKET,
        ),
        _greeter_tts,
        lambda: bool(getattr(app.state, "kid_mode", False)),
        household_registry=household,
        tz=config.LOCAL_TZ,
        state_path=config.GREETER_STATE_PATH,
    )
    app.state.greeter = greeter
    consumers.append(greeter)

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
app.include_router(vision_routes.router)
app.include_router(audio_routes.router)
app.include_router(voice_routes.router)
app.include_router(calendar_routes.router)
app.include_router(scene_synthesis_routes.router)
