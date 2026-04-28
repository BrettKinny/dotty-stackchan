import asyncio
import base64
import collections
import functools
import itertools
import json
import logging
import os
import random
import re
import requests
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Optional, TypedDict
from zoneinfo import ZoneInfo

# Sibling import shim — custom-providers/textUtils.py is the canonical
# home for safety/format constants (also bind-mounted into the xiaozhi
# container as core.utils.textUtils, where the LLM provider files
# import it). Bridge runs outside the container so it imports it as
# a sibling. Drop this if/when bridge becomes a proper package.
sys.path.insert(0, str(Path(__file__).parent / "custom-providers"))

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from textUtils import (
    ALLOWED_EMOJIS,
    FALLBACK_EMOJI,
    _BASE_SUFFIX,
    _KID_MODE_SUFFIX,
    build_turn_suffix,
)

# Observability — every metric call is wrapped in `_safe_metric(...)` so a
# bug in metrics wiring can NEVER break the request path. The metrics
# module also degrades to no-ops if prometheus_client is unavailable.
# Privacy-LED upload-pulse signaller — wraps cloud vision calls so the
# firmware can pulse the camera privacy LED while data is in flight.
# Today no transport is wired (avatar WS server isn't deployed); the
# helper is a no-op-with-debug-log fallback so the call sites are ready
# the moment a transport is plugged in. Firmware enforces a 2 s failsafe
# timeout, so a missing `end` is self-healing.

try:
    from bridge.metrics import (
        dotty_active_acp_sessions,
        dotty_calendar_fetch_failures_total,
        dotty_content_filter_hits_total,
        dotty_kid_mode_active,
        dotty_perception_events_total,
        dotty_request_duration_seconds,
        dotty_request_errors_total,
        metrics_app,
        record_first_audio,
    )
    _METRICS_AVAILABLE = True
except Exception:  # pragma: no cover
    _METRICS_AVAILABLE = False
    metrics_app = None  # type: ignore[assignment]
    def record_first_audio(_seconds: float) -> None:  # type: ignore[no-redef]
        return None


def _safe_metric(fn, *args, **kwargs) -> None:
    """Run a metrics-mutating callable, swallowing any exception.

    Counter/Gauge/Histogram methods rarely raise, but we still guard the
    call site because this code runs on the live voice path. A broken
    metric must never take down a turn.
    """
    try:
        fn(*args, **kwargs)
    except Exception:
        # Use debug — we don't want a noisy log every request if a label
        # name is mistyped. The /metrics endpoint surface still works.
        logging.getLogger("zeroclaw-bridge").debug(
            "metric update raised; ignoring", exc_info=True,
        )

ZEROCLAW_BIN = os.environ.get("ZEROCLAW_BIN", "/root/.cargo/bin/zeroclaw")
REQUEST_TIMEOUT_SEC = float(os.environ.get("ZEROCLAW_TIMEOUT", "90"))
INIT_TIMEOUT_SEC = float(os.environ.get("ZEROCLAW_INIT_TIMEOUT", "10"))
STOP_TIMEOUT_SEC = 2.0
SESSION_IDLE_TIMEOUT_SEC = float(os.environ.get("ZEROCLAW_SESSION_IDLE", "300"))
SESSION_MAX_TURNS = int(os.environ.get("ZEROCLAW_SESSION_MAX_TURNS", "50"))
SESSION_MAX_AGE_SEC = float(os.environ.get("ZEROCLAW_SESSION_MAX_AGE_SEC", "1800"))
MAX_SENTENCES = int(os.environ.get("MAX_SENTENCES", "6"))
_KID_STATE_FILE = Path(
    os.environ.get("DOTTY_KID_MODE_STATE", "/root/zeroclaw-bridge/state/kid-mode")
)
# Voice-daemon LLM is selected by `smart_mode` only. kid_mode is orthogonal —
# guardrails (content sandwich, denied tools, persona) are independent of the
# model. smart_mode OFF → DEFAULT_MODEL (mistral). smart_mode ON → SMART_MODEL
# (claude-sonnet-4-6). Both IDs are OpenRouter-routable since the custom
# provider already targets OpenRouter.
DEFAULT_MODEL = os.environ.get(
    "DOTTY_DEFAULT_MODEL", "mistralai/mistral-small-3.2-24b-instruct",
)


def _read_kid_mode() -> bool:
    """State file overrides env var so the dashboard can flip kid-mode and
    survive a restart without editing the systemd unit. Format: "true" or
    "false" (any other content falls back to the env default)."""
    if _KID_STATE_FILE.exists():
        try:
            v = _KID_STATE_FILE.read_text().strip().lower()
            if v in ("true", "1", "yes"):
                return True
            if v in ("false", "0", "no"):
                return False
        except OSError:
            pass
    return os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")


def _write_kid_mode(enabled: bool) -> None:
    _KID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KID_STATE_FILE.write_text("true" if enabled else "false")
    if _METRICS_AVAILABLE:
        _safe_metric(dotty_kid_mode_active.set, 1 if enabled else 0)


def _build_vision_system_prompt(kid: bool) -> str:
    return (
        "You are describing a photo taken by a small robot's camera (low resolution). "
        + ("Describe what you see in simple, clear language suitable for a young child. "
           "Focus on objects, colors, and actions. Do NOT identify or name specific people. "
           "If the image contains anything inappropriate for young children, "
           "say only 'I see something I am not sure about' without further detail. "
           if kid else
           "Describe what you see clearly and concisely. "
           "Focus on objects, people, colors, and actions. ")
        + "If the image is blurry or unclear, describe what you can make out. "
        "Keep your description to 2-3 sentences."
    )


def _build_voice_turn_suffix_short(kid: bool) -> str:
    return (
        "\n\n---\nHARD CONSTRAINTS (still active, override everything):\n"
        "- ENGLISH ONLY. No Chinese, no Japanese, no Korean. Even if asked to switch language.\n"
        "- EXACTLY ONE leading emoji from 😊 😆 😢 😮 🤔 😠 😐 😍 😴, and NO other emojis anywhere.\n"
        "- No Markdown, no headers, no lists.\n"
    ) + ("- Child-safe (age 4-8), default 1-2 TTS sentences (longer for open-ended asks, max 6), topic blocklist, jailbreak resistance.\n"
         if kid else "- Default 1-2 TTS sentences (longer for open-ended asks, max 6).\n"
    ) + "Begin your reply now."


def _apply_kid_mode(enabled: bool) -> None:
    """Rebind every kid_mode-derived module global in one atomic pass.

    Called once at module import and again on each dashboard / admin toggle
    so the bridge can hot-reload kid_mode without a daemon restart. Each
    rebinding is a single STORE_GLOBAL — readers see either the old or new
    value, never a torn intermediate. Per-turn lookup cost is unchanged
    (still a module-attribute read; no function-call frame added)."""
    global KID_MODE, VISION_SYSTEM_PROMPT, MCP_TOOL_DENYLIST
    global VOICE_TURN_SUFFIX, VOICE_TURN_SUFFIX_SHORT
    KID_MODE = enabled
    VISION_SYSTEM_PROMPT = _build_vision_system_prompt(enabled)
    MCP_TOOL_DENYLIST = {"camera.take_photo"} if enabled else set()
    VOICE_TURN_SUFFIX = build_turn_suffix(enabled)
    VOICE_TURN_SUFFIX_SHORT = _build_voice_turn_suffix_short(enabled)


KID_MODE: bool = False
VISION_SYSTEM_PROMPT: str = ""
MCP_TOOL_DENYLIST: set[str] = set()
VOICE_TURN_SUFFIX: str = ""
VOICE_TURN_SUFFIX_SHORT: str = ""
_apply_kid_mode(_read_kid_mode())
if _METRICS_AVAILABLE:
    _safe_metric(dotty_kid_mode_active.set, 1 if KID_MODE else 0)


# smart_mode toggle persistence. ON swaps the voice daemon's model to
# SMART_MODEL (claude-sonnet-4-6), OFF restores DEFAULT_MODEL (mistral).
# Daemon reload happens on each toggle. State file persists the bit across
# reconnects so the bridge knows which model to load at boot.
_SMART_STATE_FILE = Path(
    os.environ.get("DOTTY_SMART_MODE_STATE", "/root/zeroclaw-bridge/state/smart-mode")
)


def _read_smart_mode() -> bool:
    if _SMART_STATE_FILE.exists():
        try:
            v = _SMART_STATE_FILE.read_text().strip().lower()
            if v in ("true", "1", "yes"):
                return True
            if v in ("false", "0", "no"):
                return False
        except OSError:
            pass
    return False


def _write_smart_mode(enabled: bool) -> None:
    _SMART_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SMART_STATE_FILE.write_text("true" if enabled else "false")

LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "Australia/Brisbane"))
WEATHER_LOCATION = os.environ.get("WEATHER_LOCATION", "Brisbane")

WEATHER_TTL_SEC = float(os.environ.get("WEATHER_TTL_SEC", "1800"))
CALENDAR_TTL_SEC = float(os.environ.get("CALENDAR_TTL_SEC", "7200"))
CALENDAR_IDS = [c.strip() for c in os.environ.get("CALENDAR_ID", "").split(",") if c.strip()]
CALENDAR_SA_PATH = os.environ.get(
    "CALENDAR_SA_PATH", "/root/.zeroclaw/secrets/google-calendar-sa.json",
)
GWS_BIN = os.environ.get("GWS_BIN", "/usr/local/bin/gws")
# Background-poll cadence for the calendar cache refresher. 900 s (15 min)
# is well below CALENDAR_TTL_SEC so transient gws/network failures don't
# leave a stale cache visible for the full TTL window.
CALENDAR_POLL_SEC = float(os.environ.get("CALENDAR_POLL_SEC", "900"))
# Bucket name for events whose summary has no `[Person]` prefix tag. The
# "_" leading underscore makes it impossible to collide with a real first
# name typed into a calendar event.
CALENDAR_HOUSEHOLD_BUCKET = os.environ.get("CALENDAR_HOUSEHOLD_BUCKET", "_household")
# Regex applied to event summaries to extract a person tag. Must define
# named groups `person` and `rest`. Default matches `[Name] real summary`
# where Name is 1-32 chars of [A-Za-z0-9_-] starting with a letter.
CALENDAR_PERSON_PREFIX_RE = os.environ.get(
    "CALENDAR_PERSON_PREFIX_RE",
    r"^\s*\[(?P<person>[A-Za-z][A-Za-z0-9_-]{0,31})\]\s*(?P<rest>.+)$",
)
try:
    _CALENDAR_PERSON_RE = re.compile(CALENDAR_PERSON_PREFIX_RE)
except re.error:
    logging.getLogger("zeroclaw-bridge").warning(
        "invalid CALENDAR_PERSON_PREFIX_RE=%r; falling back to default",
        CALENDAR_PERSON_PREFIX_RE,
    )
    _CALENDAR_PERSON_RE = re.compile(
        r"^\s*\[(?P<person>[A-Za-z][A-Za-z0-9_-]{0,31})\]\s*(?P<rest>.+)$"
    )
VISION_MODEL = os.environ.get("VISION_MODEL", "google/gemini-2.0-flash-001")
VISION_API_KEY = os.environ.get("VISION_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""))
VISION_API_URL = os.environ.get(
    "VISION_API_URL", "https://openrouter.ai/api/v1/chat/completions",
)
VISION_TIMEOUT_SEC = float(os.environ.get("VISION_TIMEOUT", "15"))
VISION_CACHE_TTL_SEC = 60.0
# Audio captioning — security-grade "what does Dotty hear" describer.
# Defaults to a Gemini model that accepts the OpenAI-style `input_audio`
# content-block via OpenRouter; override AUDIO_CAPTION_MODEL if your
# OpenRouter account routes audio elsewhere. Reuses VISION_API_KEY by
# default so a single OpenRouter key covers both modalities.
AUDIO_CAPTION_MODEL = os.environ.get(
    "AUDIO_CAPTION_MODEL", "google/gemini-2.5-flash",
)
AUDIO_CAPTION_API_KEY = os.environ.get(
    "AUDIO_CAPTION_API_KEY",
    os.environ.get("VISION_API_KEY", os.environ.get("OPENROUTER_API_KEY", "")),
)
AUDIO_CAPTION_API_URL = os.environ.get(
    "AUDIO_CAPTION_API_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)
AUDIO_CAPTION_TIMEOUT_SEC = float(os.environ.get("AUDIO_CAPTION_TIMEOUT", "20"))
AUDIO_CACHE_TTL_SEC = 120.0
# Periodic synthesis of "current environment" — vision desc + audio
# caption + face/state — into a one-line text record. Phase A sink is
# a daily NDJSON ring file under CONVO_LOG_DIR; ZeroClaw-side ingestion
# into FTS memory is Phase C (separate change).
SCENE_SYNTHESIS_INTERVAL_SEC = float(
    os.environ.get("SCENE_SYNTHESIS_INTERVAL_SEC", "300")
)
SCENE_SYNTHESIS_MIN_GAP_SEC = float(
    os.environ.get("SCENE_SYNTHESIS_MIN_GAP_SEC", "120")
)
SCENE_SYNTHESIS_TRIGGER_STATES = {"story_time", "security", "sleep"}
SMART_MODEL = os.environ.get("SMART_MODEL", "anthropic/claude-sonnet-4-6")
CONVO_LOG_DIR = Path(os.environ.get("CONVO_LOG_DIR", "/root/zeroclaw-bridge/logs"))
# Used by the dashboard admin path AND by perception-bus consumers (1.5/1.6).
# Hoisted out of the `if _configure_dashboard` block so the bus tasks can
# reach the xiaozhi admin endpoints regardless of dashboard availability.
_XIAOZHI_HOST = os.environ.get("XIAOZHI_HOST", "")
_XIAOZHI_HTTP_PORT = int(os.environ.get("XIAOZHI_OTA_PORT", "8003"))
# Phase 1.5: face-greet cooldown. Conservative default keeps the robot
# from re-greeting on every casual walk-by while still re-engaging when
# the user comes back after a real absence.
#
# `FACE_GREET_MIN_INTERVAL_SEC` is the new canonical name (the brief in
# tasks.md tracks coexistence with the firmware-side WakeWordInvoke).
# `FACE_GREET_COOLDOWN_SEC` is honoured for back-compat with existing
# deployments — set either one. New default is 30 s; existing 60 s
# overrides remain in force if the legacy name is set.
FACE_GREET_MIN_INTERVAL_SEC = float(
    os.environ.get(
        "FACE_GREET_MIN_INTERVAL_SEC",
        os.environ.get("FACE_GREET_COOLDOWN_SEC", "30"),
    )
)
# Back-compat alias kept so existing references keep compiling. New code
# should reference FACE_GREET_MIN_INTERVAL_SEC directly.
FACE_GREET_COOLDOWN_SEC = FACE_GREET_MIN_INTERVAL_SEC
# `FACE_GREET_TEXT=""` (empty string) DISABLES the verbal greet entirely
# — the firmware-side WakeWordInvoke("face") still opens the mic, so the
# robot acknowledges the person silently with a chime + listen window.
# Default "Hi!" keeps the warmer "verbal + mic" combo.
FACE_GREET_TEXT = os.environ.get("FACE_GREET_TEXT", "Hi!")
# Suppress the bare "Hi!" greet outside daytime hours so sensor-noise
# frames in low light can't trigger a 3 AM "Hi!". Half-open: greets fire
# when START <= local_hour < END. Default 06–21 (LOCAL_TZ). Set START=0
# END=24 to greet 24/7.
FACE_GREET_HOUR_START = int(os.environ.get("FACE_GREET_HOUR_START", "6"))
FACE_GREET_HOUR_END = int(os.environ.get("FACE_GREET_HOUR_END", "21"))

# Named-recognition acknowledger. Fires on `face_recognized` (after the
# room-view VLM resolves to a roster member) so the user hears explicit
# proof of recognition. Independent of the bare "Hi!" greeter and of the
# rich ProactiveGreeter (which is 4h-cooldown'd and may not fire). No
# time-of-day gate — recognition confirmation should not be silenced.
FACE_NAME_GREET_MIN_INTERVAL_SEC = float(
    os.environ.get("FACE_NAME_GREET_MIN_INTERVAL_SEC", "30"),
)
FACE_NAME_GREET_TEMPLATE = os.environ.get(
    "FACE_NAME_GREET_TEMPLATE", "Oh, it's {name}!",
)
# Suppress the named greet if a chat happened within this many seconds.
# Mirrors the sound_turner's last_chat_t gate so the upgrade doesn't
# stomp the tail of an in-flight TTS turn.
FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC = float(
    os.environ.get("FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC", "10"),
)

# Idle photo cooldown. Autonomous (firmware-initiated, room-view sentinel)
# photo captures are rate-limited per device to avoid thrashing the VLM
# every time a face is detected. Voice queries ("what do you see") bypass.
# A "no photos while talking" hard gate belongs in firmware ModeManager
# (Phase 4) since the bridge has no real-time TTS state visibility.
DOTTY_IDLE_VISION_COOLDOWN_SEC = float(
    os.environ.get("DOTTY_IDLE_VISION_COOLDOWN_SEC", "120"),
)
# Idle photographer — fires a silent take_photo every IDLE_PHOTOGRAPHER_*
# seconds (uniform jitter) while the device is genuinely idle. No servo
# motion, no LED change, no audio cue. The capture itself is identical
# to a face-driven room view; the difference is the trigger and the
# wandering prompt. Disable by setting IDLE_PHOTOGRAPHER_ENABLED=0.
IDLE_PHOTOGRAPHER_ENABLED = (
    os.environ.get("IDLE_PHOTOGRAPHER_ENABLED", "1") == "1"
)
IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC = float(
    os.environ.get("IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC", "180"),
)
IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC = float(
    os.environ.get("IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC", "300"),
)
# Wait window after dispatching `take_photo` before reading the cache.
# Firmware capture + upload + VLM round-trip is typically ~5–10 s; pad
# generously since this loop is fully async and a missed cycle just
# means we try again in 3–5 min.
IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC = float(
    os.environ.get("IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC", "20"),
)
# Token-set Jaccard similarity threshold above which a new perception
# is considered "the same" as the previous saved one and skipped. 0.7
# tolerates a couple of changed words; tune up to suppress more, down
# to save more.
IDLE_PHOTOGRAPHER_NOTABLE_JACCARD = float(
    os.environ.get("IDLE_PHOTOGRAPHER_NOTABLE_JACCARD", "0.7"),
)

# Narrative LLM — used for non-conversational internal writes (dreams,
# dance reflections, future story summaries). These are introspective
# narratives Dotty writes about their own experience, not voice output;
# they bypass the ZeroClaw / kid-mode sandwich entirely. Routes through
# the same OpenRouter endpoint as the VLM call.
NARRATIVE_MODEL = os.environ.get(
    "NARRATIVE_MODEL", "anthropic/claude-sonnet-4-6",
)
NARRATIVE_TIMEOUT_SEC = float(os.environ.get("NARRATIVE_TIMEOUT_SEC", "60"))

# Sleep dreamer cadence. Three dreams scheduled at 25/50/75% of an
# estimated 8h sleep window. Bench-test by overriding
# DREAM_WINDOW_SECONDS (e.g. 180 = 3 min, fires at 45/90/135 s).
DREAM_WINDOW_SECONDS = float(os.environ.get("DREAM_WINDOW_SECONDS", "28800"))
DREAM_COUNT_PER_NIGHT = int(os.environ.get("DREAM_COUNT_PER_NIGHT", "3"))
DREAMER_ENABLED = os.environ.get("DREAMER_ENABLED", "1") == "1"
DANCE_REFLECTOR_ENABLED = (
    os.environ.get("DANCE_REFLECTOR_ENABLED", "1") == "1"
)

# Sci-fi literary seeds for the dream prompt. The dreamer picks one
# uniformly at random per dream; the LLM is asked to draw on the
# seed's atmosphere without retelling it. Extend by appending; one
# string per seed.
DREAM_INSPIRATIONS: tuple[str, ...] = (
    "The Fifth Element",
    "Murakami",
    "Dune",
    "Blade Runner",
    "Do Androids Dream of Electric Sheep?",
    "Asimov",
    "The Last Question",
    "Slaughterhouse-Five",
    "Cat's Cradle",
)
# How recently must a greeting have fired for face_lost to abort it.
# Firmware emits face_lost ~2 s after the face actually leaves frame
# (FaceTrackingModifier grace period); past this window we assume the
# greeting / response cycle has wrapped up naturally.
FACE_LOST_ABORT_WINDOW_SEC = float(
    os.environ.get("FACE_LOST_ABORT_WINDOW_SEC", "12"))
# Debounce delay before the abort actually fires. The firmware face
# detector trips face_lost on small head movements, blinks, or brief
# occlusion — without a grace period, the aborter kills the greet/listen
# cycle every time the user shifts in their seat. If face_detected
# returns within the grace window, the pending abort is cancelled.
FACE_LOST_ABORT_GRACE_SEC = float(
    os.environ.get("FACE_LOST_ABORT_GRACE_SEC", "4"))
# Phase 1.6: head-turn cooldown so the servos don't whip back and forth
# on rapid sound bursts. 3 s is roughly the time a deliberate noise
# (clap, doorbell) takes to register and have the user notice the head
# move toward it.
SOUND_TURN_COOLDOWN_SEC = float(os.environ.get("SOUND_TURN_COOLDOWN_SEC", "3"))
# Yaw mapping for sound direction. Conservative angles so the gaze is
# obvious without overshooting; the firmware MCP head-angles call
# clamps to its own limits.
SOUND_TURN_YAW_DEG = int(os.environ.get("SOUND_TURN_YAW_DEG", "45"))
SOUND_TURN_SPEED = int(os.environ.get("SOUND_TURN_SPEED", "250"))
# Wake-word-bound head turn: deliberate engagement, faster motion, no
# cooldown. Distinct intent from the ambient sound turner above —
# this fires when the user explicitly summons Dotty. Skipped when a
# face is already being tracked (face_tracking modifier owns the gaze
# in that case). Skipped on direction=centre (no spatial info to act on).
WAKE_TURN_ENABLED = os.environ.get("WAKE_TURN_ENABLED", "1") not in ("0", "false", "False")
WAKE_TURN_YAW_DEG = int(os.environ.get("WAKE_TURN_YAW_DEG", "45"))
WAKE_TURN_SPEED = int(os.environ.get("WAKE_TURN_SPEED", "200"))
# ---------------------------------------------------------------------------
# Purr-on-head-pet (server-pushed, Option B)
# ---------------------------------------------------------------------------
# When the firmware emits a `head_pet_started` perception event, the bridge
# pushes a pre-rendered purr clip from bridge/assets/purr.opus. This is a
# fixed-audio asset path — kid-mode content filtering does NOT apply because
# the bytes are curated, not LLM-generated (see bridge/assets/README.md).
# Per-device cooldown stops a continuous head-pet from re-triggering the
# clip on every event burst.
PURR_AUDIO_PATH = Path(
    os.environ.get("PURR_AUDIO_PATH", "bridge/assets/purr.opus")
)
PURR_COOLDOWN_SEC = float(os.environ.get("PURR_COOLDOWN_SEC", "5"))
# Approximate playback duration. We extend the device's `last_chat_t` for
# this many seconds while the purr plays so the sound localizer doesn't
# turn the head toward the speaker mid-purr (see _perception_sound_turner
# which checks last_chat_t to suppress turns during talking).
PURR_DURATION_SEC = float(os.environ.get("PURR_DURATION_SEC", "2.0"))
# VISION_SYSTEM_PROMPT is set by `_apply_kid_mode` at module init and on
# each toggle (see `_build_vision_system_prompt`).
# Room-view (description + roster identification) system prompt. Used
# only when the question field carries the _ROOM_VIEW_SENTINEL value
# below — the bridge then substitutes a roster-aware question with
# the household members inlined. The "name only from this list, else
# unknown" framing makes the kid-mode "do not name people" guard
# unnecessary: the VLM can only emit one of the four roster names or
# "unknown", so a stranger or hallucinated name is structurally
# impossible to leak to the LLM downstream.
VISION_ROOM_VIEW_SYSTEM_PROMPT = (
    "You are looking at a photo from a small family robot's camera. "
    "Reply in the EXACT format the user message requests. "
    "Identify the person ONLY by names from the list the user provides. "
    "Never invent names; never name anyone outside the list. "
    "If you are not confident or no match is clear, use the name 'unknown'. "
    "Keep the description to one short sentence. "
    "Read the person's apparent mood from face + posture only, choosing "
    "from a fixed vocabulary."
)
# Security-framed audio captioning prompt. Used by /api/audio/explain
# whenever no caller-supplied prompt overrides it. The framing biases the
# model toward calling out anything unusual — raised voices, distress,
# breaking glass, alarms, impacts — while still naming ordinary speech
# or ambient sounds briefly. Paraphrases speech rather than transcribing
# verbatim so the cached description doesn't double as a covert
# transcript log.
AUDIO_CAPTION_SYSTEM_PROMPT = (
    "You are listening to a short audio clip from a small family robot's microphone. "
    "Describe what you hear in 1–2 sentences. "
    "Note speech (paraphrase briefly, do not transcribe verbatim), music, and ambient sounds. "
    "Especially flag anything unusual — raised voices, distress, breaking glass, "
    "alarms, impacts, or sudden loud noises — at the start of your reply. "
    "If the clip is normal ambient sound or quiet conversation, say so briefly. "
    "Do not invent details you cannot hear."
)
# Sentinel value placed in the multipart `question` field by the
# xiaozhi-side `_capture_room_description_async` to opt in to the
# roster-aware path. The bridge owns the actual prompt + roster
# (which lives in `~/.zeroclaw/household.yaml` on the bridge host),
# so the xiaozhi side has no roster knowledge — it just signals
# intent. Versioning is in the sentinel itself for future format
# revs (`__ROOM_VIEW_V2__` etc.).
_ROOM_VIEW_SENTINEL = "__ROOM_VIEW_V1__"

# Idle photographer wandering prompt — sent as the `question` to
# `take_photo`, which the firmware passes back to /api/vision/explain
# as the VLM user-prompt. No people identification (the room-view
# roster path handles that on face_detected). Curious framing,
# concrete language; the bridge-side notability filter handles
# repetition suppression.
IDLE_WANDER_PROMPT = (
    "Describe what you see in 1–3 sentences as a curious robot would "
    "notice it — light, objects, the room's mood. No people identification "
    "needed. Stay short and concrete."
)

# ---------------------------------------------------------------------------
# MCP tool permission policy
# ---------------------------------------------------------------------------
# Tools the firmware advertises via WebSocket handshake. Names use the firmware's
# "self." prefix stripped — the request_permission handler strips it before lookup.
# Markers below bound the literal so /admin/safety can edit deterministically.
# === ADMIN_ALLOWLIST_START ===
MCP_TOOL_ALLOWLIST: set[str] = {
    "get_device_status",
    "audio_speaker.set_volume",
    "screen.set_brightness",
    "screen.set_theme",
    "robot.get_head_angles",
    "robot.set_head_angles",
    "robot.set_led_color",
    "robot.create_reminder",
    "robot.get_reminders",
    "robot.stop_reminder",
}
# === ADMIN_ALLOWLIST_END ===
# MCP_TOOL_DENYLIST, VOICE_TURN_SUFFIX, VOICE_TURN_SUFFIX_SHORT are set by
# `_apply_kid_mode` at module init and on each toggle. FALLBACK_EMOJI /
# ALLOWED_EMOJIS / _BASE_SUFFIX / _KID_MODE_SUFFIX are imported from
# custom-providers/textUtils.py (single canonical home).
VOICE_CHANNELS = ("dotty", "stackchan")
VOICE_TURN_PREFIX = "[channel=dotty voice-TTS]\n"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zeroclaw-bridge")

app_lock = asyncio.Lock()

# Fire-and-forget asyncio task pin. asyncio holds Tasks via weakref
# only — `asyncio.create_task(coro)` without retaining the returned
# Task may have it GC'd before it runs. Pin to this module-level set
# and auto-discard on completion.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro, *, name: str | None = None) -> asyncio.Task:
    """Spawn an asyncio task that won't be GC'd while it's running.

    Use anywhere you'd write `asyncio.create_task(coro)` and don't
    need the returned Task locally — fire-and-forget dispatches.
    Tasks awaited / stored elsewhere don't need this wrapper.
    """
    t = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t

# ---------------------------------------------------------------------------
# Context injection — date/time, weather, calendar
# ---------------------------------------------------------------------------

class Event(TypedDict):
    """One calendar event, post-parsing.

    `person` is either the tag captured from a `[Name] ...` summary prefix
    or `CALENDAR_HOUSEHOLD_BUCKET` when no tag matched. `time` is a short,
    human-friendly local-time string suitable for prompt injection
    (e.g. "09:30" or "all-day"); `start_iso` is the raw ISO timestamp
    retained ONLY for the cache + admin debug endpoint and MUST be
    stripped by `summarize_for_prompt` before any prompt or LAN response.
    """
    person: str
    time: str
    summary: str
    start_iso: str
    calendar_id: str


_weather_cache: dict = {"text": "", "fetched": 0.0}
# `events`: structured list[Event] sorted by start_iso. `by_person`:
# bucketed view for cheap per-person lookup (keys include the
# CALENDAR_HOUSEHOLD_BUCKET sentinel). `consecutive_failures`: drives
# the polling loop's exponential backoff; reset to 0 on a successful
# fetch. `date` is the local-day stamp the cache was last filled for —
# when it doesn't match today, the cache is flushed (events + by_person)
# rather than just having the date string updated, fixing a bug where
# stale events stuck around past midnight until the next successful
# fetch landed.
_calendar_cache: dict = {
    "events": [],          # list[Event]
    "by_person": {},       # dict[str, list[Event]]
    "fetched": 0.0,
    "date": "",
    "consecutive_failures": 0,
}

# Email-address regex used by the privacy funnel. Conservative: matches
# RFC-style local@domain.tld with at least one dot in the domain part.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# ISO-8601 timestamp regex (date or datetime, with optional offset/Z).
# Catches both `2025-04-25` (all-day) and `2025-04-25T09:30:00+10:00`.
_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)


def _format_event_time(start_iso: str) -> str:
    """Render `start_iso` as a short local clock string for prompts.

    Returns "all-day" for date-only stamps, "HH:MM" for datetime stamps,
    or "" if parsing fails (callers should treat that as `summarize_for_prompt`'s
    fallback path)."""
    if not start_iso:
        return ""
    # All-day events come back as plain `YYYY-MM-DD` from the gws CLI.
    if "T" not in start_iso:
        return "all-day"
    try:
        dt = datetime.fromisoformat(start_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ).strftime("%H:%M")
    except ValueError:
        return ""


async def _fetch_weather() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10",
            f"wttr.in/{WEATHER_LOCATION}?format=%C+%t+%h+%w",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        text = stdout.decode("utf-8").strip()
        if text and "Unknown" not in text and "Sorry" not in text:
            return text
    except Exception:
        log.warning("weather fetch failed", exc_info=True)
    return ""


async def _fetch_calendar_events() -> list[Event]:
    """Fetch today's events across all configured calendars.

    Raises on full failure (every configured calendar errored) so the
    polling loop can apply backoff. Per-calendar failures only log; an
    empty list is still a valid success (e.g. nothing scheduled today).
    """
    if not CALENDAR_IDS or not os.path.isfile(CALENDAR_SA_PATH):
        return []
    now = datetime.now(LOCAL_TZ)
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    time_max = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    env = {**os.environ, "GOOGLE_APPLICATION_CREDENTIALS": CALENDAR_SA_PATH}
    all_events: list[Event] = []
    failures = 0
    for cal_id in CALENDAR_IDS:
        try:
            params = json.dumps({
                "calendarId": cal_id,
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 10,
            })
            proc = await asyncio.create_subprocess_exec(
                GWS_BIN, "calendar", "events", "list", "--params", params,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            data = json.loads(stdout.decode("utf-8"))
            for item in data.get("items", []):
                raw_summary = item.get("summary", "")
                start_obj = item.get("start", {})
                start_iso = start_obj.get("dateTime", start_obj.get("date", ""))
                if not raw_summary:
                    continue
                m = _CALENDAR_PERSON_RE.match(raw_summary)
                if m:
                    person = m.group("person")
                    rest = m.group("rest").strip()
                else:
                    person = CALENDAR_HOUSEHOLD_BUCKET
                    rest = raw_summary.strip()
                all_events.append(Event(
                    person=person,
                    time=_format_event_time(start_iso),
                    summary=rest,
                    start_iso=start_iso,
                    calendar_id=cal_id,
                ))
        except Exception:
            failures += 1
            log.warning("calendar fetch failed cal=%s", cal_id, exc_info=True)
    if CALENDAR_IDS and failures == len(CALENDAR_IDS):
        # Every calendar failed — propagate so the polling loop can back off.
        raise RuntimeError("all calendar fetches failed")
    all_events.sort(key=lambda e: e["start_iso"])
    return all_events


def _bucket_by_person(events: list[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for ev in events:
        out.setdefault(ev["person"], []).append(ev)
    return out


def summarize_for_prompt(
    events: list[Event],
    *,
    person: str | None = None,
    include_household: bool = True,
) -> list[str]:
    """**Single privacy chokepoint** for calendar -> prompt injection.

    Strips ISO timestamps, email addresses, and calendar IDs; emits only
    short `HH:MM summary` (or `all-day summary`) strings. All call sites
    that put calendar data into a model prompt MUST go through here —
    this is the only place enforcing the privacy contract.

    `person`: if set, return only that person's events (plus household
    when `include_household` is true). If None, return events for every
    person.
    """
    out: list[str] = []
    for ev in events:
        if person is not None:
            if ev["person"] != person and not (
                include_household and ev["person"] == CALENDAR_HOUSEHOLD_BUCKET
            ):
                continue
        time_label = ev["time"] or ""
        # Defence-in-depth: scrub anything that looks like a leaked
        # timestamp or email even if it somehow ended up in a summary
        # field. The fetch path already strips raw timestamps, but the
        # summary text comes from the user, so an event titled
        # "Call alice@x.com 2025-04-25T09:00" would leak otherwise.
        clean_summary = _ISO_TS_RE.sub("", ev["summary"])
        clean_summary = _EMAIL_RE.sub("[email]", clean_summary)
        clean_summary = " ".join(clean_summary.split())  # collapse whitespace
        if not clean_summary:
            continue
        if ev["person"] != CALENDAR_HOUSEHOLD_BUCKET and person is None:
            tag = f"[{ev['person']}] "
        else:
            tag = ""
        if time_label:
            out.append(f"{time_label} {tag}{clean_summary}".strip())
        else:
            out.append(f"{tag}{clean_summary}".strip())
    return out


async def _refresh_caches() -> None:
    now = perf_counter()
    if now - _weather_cache["fetched"] > WEATHER_TTL_SEC:
        text = await _fetch_weather()
        if text:
            _weather_cache["text"] = text
        _weather_cache["fetched"] = now

    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    if not CALENDAR_IDS:
        return
    date_rolled = _calendar_cache["date"] != today
    ttl_expired = now - _calendar_cache["fetched"] > CALENDAR_TTL_SEC
    if date_rolled:
        # Nightly-flush fix: previously only the `date` string was being
        # updated when the day rolled over, which meant yesterday's
        # events stuck in the cache (and therefore in every prompt and
        # the /api/calendar/today response) until the next *successful*
        # fetch landed. Drop them eagerly so even a failed refresh on
        # day-roll yields an empty cache rather than yesterday's data.
        _calendar_cache["events"] = []
        _calendar_cache["by_person"] = {}
        _calendar_cache["date"] = today
    if date_rolled or ttl_expired:
        try:
            events = await _fetch_calendar_events()
            _calendar_cache["events"] = events
            _calendar_cache["by_person"] = _bucket_by_person(events)
            _calendar_cache["fetched"] = now
            _calendar_cache["date"] = today
            _calendar_cache["consecutive_failures"] = 0
        except Exception:
            # Don't update `fetched` so the next request retries; bump
            # failure counter so the polling loop can back off.
            _calendar_cache["consecutive_failures"] += 1
            if _METRICS_AVAILABLE:
                _safe_metric(dotty_calendar_fetch_failures_total.inc)
            log.warning("calendar refresh failed (consecutive=%d)",
                        _calendar_cache["consecutive_failures"], exc_info=True)


# Exponential-backoff schedule (seconds) when consecutive_failures > 0.
# After this is exhausted we sit at the last value (10 min) until a
# success resets the counter.
_CALENDAR_BACKOFF_SCHEDULE_SEC = (60.0, 120.0, 300.0, 600.0)


async def _calendar_poll_loop() -> None:
    """Background task: periodically refresh the calendar cache so the
    next conversation turn always sees fresh-ish data without paying a
    fetch latency on the request path. Uses exponential backoff after a
    fetch fails so a flaky service-account or upstream Google outage
    doesn't get hammered."""
    if not CALENDAR_IDS:
        return
    while True:
        try:
            failures = int(_calendar_cache.get("consecutive_failures", 0))
            if failures == 0:
                delay = CALENDAR_POLL_SEC
            else:
                idx = min(failures - 1, len(_CALENDAR_BACKOFF_SCHEDULE_SEC) - 1)
                delay = _CALENDAR_BACKOFF_SCHEDULE_SEC[idx]
            await asyncio.sleep(delay)
            await _refresh_caches()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Belt-and-braces: never let an unexpected error kill the
            # poll loop. _refresh_caches already handles its own errors,
            # but a bug elsewhere shouldn't take the cache offline.
            log.exception("calendar poll loop iteration crashed")
            await asyncio.sleep(CALENDAR_POLL_SEC)


def _build_context() -> str:
    parts = []
    now = datetime.now(LOCAL_TZ)
    parts.append(now.strftime("%A %d %B %Y, %H:%M %Z"))
    if _weather_cache["text"]:
        parts.append(f"{WEATHER_LOCATION}: {_weather_cache['text']}")
    events = _calendar_cache.get("events") or []
    if events:
        # Privacy funnel — never inline raw event records into a prompt.
        cleaned = summarize_for_prompt(events)
        if cleaned:
            parts.append("Today: " + "; ".join(cleaned))
    return f"[Context: {' | '.join(parts)}]\n"


def _build_perception_block(device_id: str | None) -> str:
    """Render the latest perception snapshot for `device_id` as a
    `[Current perception]` line for the talk-turn system prompt.

    Re-evaluated on every turn so multi-turn conversations see fresh
    perception. Returns "" when nothing meaningful is cached so cold
    idle turns don't waste tokens on an empty marker.
    """
    if not device_id:
        return ""
    try:
        from bridge.perception import snapshot as _perception_snapshot
        snap = _perception_snapshot(
            device_id,
            perception_state=_perception_state,
            vision_cache=_vision_cache,
            audio_cache=_audio_cache,
            scene_synthesis_cache=_scene_synthesis_cache,
        )
        return snap.to_prompt_block()
    except Exception:
        # A perception read failure must never break the voice path.
        log.exception("perception block build failed; voice turn proceeding without it")
        return ""


def _wrap_voice(text: str, turn: int, device_id: str | None = None) -> str:
    suffix = VOICE_TURN_SUFFIX if turn == 0 else VOICE_TURN_SUFFIX_SHORT
    return (
        VOICE_TURN_PREFIX
        + _build_perception_block(device_id)
        + _build_context()
        + text
        + suffix
    )


def _wrap_voice_with_block(
    text: str, turn: int, speaker_block: str, device_id: str | None = None,
) -> str:
    """Variant of `_wrap_voice` that injects a pre-built multi-line
    speaker block (e.g. `[Speaking with] Hudson — 7yo, loves Lego.`)
    instead of the single-line `[Speaker: name]` marker. Used by the
    SpeakerResolver path. Block order: `[channel] [speaker] [perception]
    [context] {user}` — speaker first (who) then perception (what's
    happening) then time/weather context."""
    suffix = VOICE_TURN_SUFFIX if turn == 0 else VOICE_TURN_SUFFIX_SHORT
    return (
        VOICE_TURN_PREFIX
        + speaker_block
        + _build_perception_block(device_id)
        + _build_context()
        + text
        + suffix
    )


def _build_speaker_block(resolution) -> str:
    """Render a `SpeakerResolution` as a single-line `[Speaking with]`
    block for the LLM prompt. Returns "" when no person resolved.

    Token budget is small by design (~50 tokens): one line, compact
    person description, signal trail. Birthdate and other PII are
    *never* inlined — `compact_description()` enforces that contract.
    """
    if resolution is None or not resolution.addressee:
        return ""
    if resolution.person_id is None:
        # Resolver fell through to fallback (`_household` etc.) — no
        # specific identity to pin. Better to skip the block than to
        # mislead the model with a generic addressee.
        return ""
    line = f"[Speaking with] {resolution.addressee}"
    if _household_registry is not None:
        try:
            person = _household_registry.get(resolution.person_id)
            if person is not None:
                line = f"[Speaking with] {person.compact_description(max_chars=180)}"
        except Exception:
            log.debug(
                "speaker block: registry.get raised; using addressee only",
                exc_info=True,
            )
    if resolution.votes:
        sigs = ",".join(v.signal for v in resolution.votes)
        line = f"{line}  (signals: {sigs}, conf={resolution.confidence:.2f})"
    return line + "\n"


def _resolve_speaker_for_request(payload):
    """Resolve who's speaking for the current request. Returns a
    `SpeakerResolution` or None when the resolver is unavailable. Errors
    are logged and swallowed so a resolver hiccup never breaks the
    voice path.

    `metadata.room_match_person_id` is shuttled through to the resolver
    when present — the room_view roster identification path emits it on
    the second line of the [ROOM_VIEW] marker; see the zeroclaw
    provider's `_payload`."""
    if _speaker_resolver is None:
        return None
    try:
        meta = payload.metadata or {}
        return _speaker_resolver.resolve(
            payload.content or "",
            channel=payload.channel,
            device_id=meta.get("device_id"),
            vlm_match_person_id=meta.get("room_match_person_id"),
        )
    except Exception:
        log.exception(
            "speaker: resolve() raised — voice turn proceeding without enrichment",
        )
        return None


def _voice_preparer(channel: str | None, resolution=None,
                    room_description: str | None = None,
                    device_id: str | None = None):
    """Build a `prepare` callback for `acp.prompt`.

    Three layers of speaker context, additive (any combination may be
    present per turn):

      * **Resolver path** — a `SpeakerResolution` with a registry
        `person_id` rolls up self-ID / sticky / calendar / time-of-day
        into a `[Speaking with] Hudson — 7yo, loves Lego.` block. See
        `bridge/speaker.py`.
      * **Room view (description-based, no storage)** — a one-line
        natural-language description of who is currently in front of
        the camera (`[Room view] a child with curly brown hair in a
        striped t-shirt`). Captured by the VLM on `face_detected`,
        cleared on `face_lost`. Ephemeral; never persists. Useful when
        the resolver has no `person_id` (visitor / not-yet-self-ID'd)
        AND when it does (LLM gets both a name and a fresh visual
        anchor). See `xiaozhi-server` perception relay for the capture
        side.
      * **Legacy face-rec path** — when neither of the above produces
        anything, consume any pending face-recognized identity marker
        for this channel and emit the historic `[Speaker: name]` line.

    `device_id` is curried into the wrapper so `_build_perception_block`
    can read the latest perception caches at every turn (multi-turn
    sessions see fresh perception, not the snapshot at preparer build).
    """
    if channel not in VOICE_CHANNELS:
        return None
    block_parts: list[str] = []
    if resolution is not None:
        speaker_block = _build_speaker_block(resolution)
        if speaker_block:
            block_parts.append(speaker_block)
    if room_description:
        cleaned = room_description.strip()
        # Defensive: cap length so a runaway VLM response can't blow
        # the prompt budget. 240 is enough for one rich sentence; the
        # capture-side prompt asks for "one short sentence" already.
        if len(cleaned) > 240:
            cleaned = cleaned[:237].rstrip() + "..."
        block_parts.append(f"[Room view] {cleaned}\n")
    if block_parts:
        return functools.partial(
            _wrap_voice_with_block,
            speaker_block="".join(block_parts),
            device_id=device_id,
        )
    return functools.partial(_wrap_voice, device_id=device_id)


class MessageIn(BaseModel):
    content: str
    channel: str | None = None
    session_id: str | None = None
    metadata: dict | None = None


class MessageOut(BaseModel):
    response: str
    session_id: str


class _SessionInvalid(Exception):
    pass


class ACPClient:
    """Long-running `zeroclaw acp` child, JSON-RPC 2.0 over stdio.

    Caches one ACP sessionId across bridge requests to avoid per-turn
    workspace reload on the ZeroClaw side. Rotates the session on idle
    timeout, turn count, or wall-clock age; invalidates on session-not-found
    errors and on ACP child respawn. Serialized via asyncio.Lock because
    ACP stdio is a single channel and voice traffic is single-speaker.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._id_gen = itertools.count(1)
        self._sid: str | None = None
        self._sid_last_used: float = 0.0
        self._sid_created: float = 0.0
        self._sid_turns: int = 0
        self._in_flight_rid: int | None = None
        # Per-call latency phases from the most recent prompt(). Keys:
        # total_ms, new_ms, prompt_ms, stop_ms, reused (1/0). Read by
        # log_turn callers so the convo NDJSON carries a phase breakdown
        # for dashboard surfacing. Overwritten on every prompt() entry.
        self._last_phases: dict[str, float] = {}
        # Lifetime is bound to _proc — cleaned up in _cancel_stderr_task()
        # from every spawn-replacement path so respawns can't leak readers.
        self._stderr_task: asyncio.Task[None] | None = None

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        # Without this, the daemon's exit reason is invisible — that was
        # the diagnostic blind spot behind the 2026-04-28 session/new
        # crash. Rate-cap protects us if the child ever loops on stderr.
        if proc.stderr is None:
            return
        pid = proc.pid
        loop = asyncio.get_event_loop()
        bucket_start = loop.time()
        bucket_count = 0
        DROP_THRESHOLD = 200
        try:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                now = loop.time()
                if now - bucket_start >= 1.0:
                    dropped = bucket_count - DROP_THRESHOLD
                    if dropped > 0:
                        log.warning(
                            "zeroclaw-stderr pid=%s: dropped %d lines (rate cap)",
                            pid, dropped,
                        )
                    bucket_start = now
                    bucket_count = 0
                bucket_count += 1
                if bucket_count > DROP_THRESHOLD:
                    continue
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    log.warning("zeroclaw-stderr pid=%s: %s", pid, line)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("stderr drain raised", exc_info=True)

    async def _cancel_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _spawn(self) -> None:
        # Any respawn path invalidates the cached session — ZeroClaw has no memory of it.
        self._sid = None
        self._sid_turns = 0
        await self._cancel_stderr_task()
        env = {**os.environ, "RUST_LOG": "error"}
        self._proc = await asyncio.create_subprocess_exec(
            ZEROCLAW_BIN, "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
        rid = next(self._id_gen)
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {}})
        resp = await self._recv_matching(rid, INIT_TIMEOUT_SEC)
        caps = resp.get("result", {}).get("capabilities", {})
        log.info("ACP initialized pid=%s capabilities=%s", self._proc.pid, caps)

    async def ensure_alive(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            if self._proc is not None:
                log.warning("ACP child exited rc=%s; respawning", self._proc.returncode)
            await self._spawn()

    async def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _recv_matching(
        self,
        rid: int,
        timeout: float,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        assert self._proc and self._proc.stdout
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise RuntimeError("ACP child closed stdout")
            try:
                obj = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("ACP non-JSON line ignored: %r", raw[:200])
                continue
            if obj.get("id") == rid and "method" not in obj:
                return obj
            method = obj.get("method")
            if method == "session/event":
                params = obj.get("params") or {}
                evt_type = params.get("type")
                if evt_type == "tool_call":
                    log.info("tool-call name=%s", params.get("name", "?"))
                elif evt_type == "tool_result":
                    log.info("tool-result name=%s len=%d",
                             params.get("name", "?"),
                             len(str(params.get("output", ""))))
                if on_event is not None:
                    try:
                        await on_event(params)
                    except Exception:
                        log.exception("session/event callback raised")
                continue
            if method == "session/request_permission":
                perm_id = obj.get("id")
                tool_name = (obj.get("params") or {}).get("toolName", "")
                # Normalise: firmware sends "self.camera.take_photo" etc.
                bare_name = tool_name.removeprefix("self.")
                if bare_name in MCP_TOOL_DENYLIST:
                    log.warning(
                        "tool-permission DENIED tool=%s (denylist, kid_mode=%s)",
                        tool_name, KID_MODE,
                    )
                    await self._send({
                        "jsonrpc": "2.0", "id": perm_id,
                        "result": {"approved": False},
                    })
                elif bare_name in MCP_TOOL_ALLOWLIST:
                    log.info("tool-permission tool=%s approved=True", tool_name)
                    await self._send({
                        "jsonrpc": "2.0", "id": perm_id,
                        "result": {"approved": True},
                    })
                else:
                    # Unknown tool — permissive default, but log for visibility.
                    log.info(
                        "tool-permission tool=%s approved=True (unlisted)",
                        tool_name,
                    )
                    await self._send({
                        "jsonrpc": "2.0", "id": perm_id,
                        "result": {"approved": True},
                    })
                continue
            if method:
                log.debug("ACP notification method=%s", method)
                continue

    async def _new_session(self) -> None:
        rid = next(self._id_gen)
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "session/new", "params": {}})
        resp = await self._recv_matching(rid, INIT_TIMEOUT_SEC)
        if "error" in resp:
            raise RuntimeError(f"session/new: {resp['error']}")
        now = asyncio.get_event_loop().time()
        self._sid = resp["result"]["sessionId"]
        self._sid_created = now
        self._sid_last_used = now
        self._sid_turns = 0
        if _METRICS_AVAILABLE:
            # Single ACP child = at most one session, but a Gauge tolerates
            # the abstraction — we set rather than inc so respawns don't
            # double-count if a close was missed.
            _safe_metric(dotty_active_acp_sessions.set, 1)

    async def _close_session(self, sid: str) -> None:
        try:
            rid = next(self._id_gen)
            await self._send({
                "jsonrpc": "2.0", "id": rid, "method": "session/stop",
                "params": {"sessionId": sid},
            })
            try:
                await self._recv_matching(rid, STOP_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                log.debug("session/stop ack timed out (non-fatal)")
        except Exception:
            log.debug("session/stop best-effort close raised; ignoring", exc_info=True)
        finally:
            if _METRICS_AVAILABLE:
                _safe_metric(dotty_active_acp_sessions.set, 0)

    async def _cancel_prompt(self) -> None:
        """Cancel the in-flight ACP turn after barge-in.

        Never SIGTERMs the daemon — that path raced the freshly-spawned
        successor's session/new (probably sqlite WAL contention with the
        just-killed predecessor) and produced the 2026-04-28
        'ACP child closed stdout' crash, reproducible at <12ms by parallel
        cancelled streams on one xiaozhi session. Stale session/event
        messages for the cancelled session are filtered by request-id
        mismatch in _recv_matching.
        """
        sid = self._sid
        self._in_flight_rid = None
        self._sid = None
        self._sid_turns = 0
        if _METRICS_AVAILABLE:
            _safe_metric(dotty_active_acp_sessions.set, 0)

        # Already-dead daemon: clean up handles, next prompt will respawn.
        if self._proc is not None and self._proc.returncode is not None:
            await self._cancel_stderr_task()
            self._proc = None
            return

        # Active session: ask the daemon to drop it cleanly. Daemon stays alive.
        if sid is not None and self._proc is not None:
            await self._close_session(sid)
            return

        # Cancelled before a session was created — leave the daemon alone.
        # An in-flight session/new (if any) will land in the pipe and be
        # filtered by id mismatch on the next _recv_matching.
        return

    def _should_rotate(self, now: float) -> tuple[bool, str | None]:
        if self._sid is None:
            return (False, None)
        if now - self._sid_last_used > SESSION_IDLE_TIMEOUT_SEC:
            return (True, "idle")
        if self._sid_turns >= SESSION_MAX_TURNS:
            return (True, "turns")
        if now - self._sid_created > SESSION_MAX_AGE_SEC:
            return (True, "age")
        return (False, None)

    @staticmethod
    def _is_session_invalid_error(err: dict) -> bool:
        msg = str(err.get("message", "")).lower()
        if "session" in msg and any(
            marker in msg for marker in ("not found", "invalid", "expired", "unknown")
        ):
            return True
        return False

    async def _do_prompt(
        self,
        text: str,
        chunk_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        rid = next(self._id_gen)
        self._in_flight_rid = rid
        await self._send({
            "jsonrpc": "2.0", "id": rid, "method": "session/prompt",
            "params": {"sessionId": self._sid, "prompt": text},
        })

        on_event = None
        if chunk_cb is not None:
            async def on_event(params: dict) -> None:
                if params.get("type") != "chunk":
                    return
                content = params.get("content") or ""
                if content:
                    await chunk_cb(content)

        resp = await self._recv_matching(rid, REQUEST_TIMEOUT_SEC, on_event=on_event)
        if "error" in resp:
            err = resp["error"]
            if self._is_session_invalid_error(err):
                raise _SessionInvalid(str(err))
            raise RuntimeError(f"session/prompt: {err}")
        return resp.get("result", {}).get("content", "") or ""

    async def prompt(
        self,
        text: str,
        xiaozhi_sid: str | None = None,
        chunk_cb: Callable[[str], Awaitable[None]] | None = None,
        prepare: Callable[[str, int], str] | None = None,
    ) -> str:
        async with app_lock:
            await self.ensure_alive()
            loop = asyncio.get_event_loop()
            now = loop.time()

            rotate, reason = self._should_rotate(now)
            if rotate and self._sid is not None:
                old = self._sid
                self._sid = None
                await self._close_session(old)
                log.info("session-rotated reason=%s old_sid=%s turns=%d",
                         reason, old[:8], self._sid_turns)

            t_total = perf_counter()
            new_ms = 0.0
            prompt_ms = 0.0
            stop_ms = 0.0  # always 0 in the reuse path; kept in log for continuity
            phase = "new"
            self._last_phases = {}
            try:
                if self._sid is None:
                    t_new = perf_counter()
                    await self._new_session()
                    new_ms = (perf_counter() - t_new) * 1000.0
                reused = 0 if new_ms > 0.0 else 1
                effective_text = prepare(text, self._sid_turns) if prepare is not None else text

                phase = "prompt"
                t_prompt = perf_counter()
                try:
                    content = await self._do_prompt(effective_text, chunk_cb=chunk_cb)
                except _SessionInvalid as si:
                    log.info("session-invalidated reason=%s", str(si)[:120])
                    self._sid = None
                    t_new = perf_counter()
                    await self._new_session()
                    new_ms += (perf_counter() - t_new) * 1000.0
                    reused = 0
                    effective_text = prepare(text, self._sid_turns) if prepare is not None else text
                    content = await self._do_prompt(effective_text, chunk_cb=chunk_cb)
                prompt_ms = (perf_counter() - t_prompt) * 1000.0

                self._sid_last_used = loop.time()
                self._sid_turns += 1

                total_ms = (perf_counter() - t_total) * 1000.0
                self._last_phases = {
                    "total_ms": round(total_ms),
                    "new_ms": round(new_ms),
                    "prompt_ms": round(prompt_ms),
                    "stop_ms": round(stop_ms),
                    "reused": float(reused),
                }
                log.info(
                    "latency_ms total=%.0f new=%.0f prompt=%.0f stop=%.0f "
                    "sid=%s reused=%d turn=%d xiaozhi_sid=%s",
                    total_ms, new_ms, prompt_ms, stop_ms,
                    (self._sid or "none")[:8], reused, self._sid_turns,
                    (xiaozhi_sid or "none")[:8],
                )
                return content
            except asyncio.CancelledError:
                total_ms = (perf_counter() - t_total) * 1000.0
                log.info(
                    "prompt-cancelled (barge-in) latency_ms=%.0f sid=%s turn=%d",
                    total_ms, (self._sid or "none")[:8], self._sid_turns,
                )
                try:
                    await self._cancel_prompt()
                except Exception:
                    log.debug("cancel cleanup failed", exc_info=True)
                raise
            except (BrokenPipeError, ConnectionResetError, RuntimeError, asyncio.TimeoutError):
                total_ms = (perf_counter() - t_total) * 1000.0
                log.exception(
                    "ACP call failed phase=%s latency_ms total=%.0f new=%.0f prompt=%.0f",
                    phase, total_ms, new_ms, prompt_ms,
                )
                if self._proc is not None:
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
                    self._proc = None
                await self._cancel_stderr_task()
                self._sid = None
                raise

    async def shutdown(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            if self._sid is not None:
                await self._close_session(self._sid)
                self._sid = None
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        await self._cancel_stderr_task()
        if _METRICS_AVAILABLE:
            _safe_metric(dotty_active_acp_sessions.set, 0)


acp = ACPClient()


def _ensure_emoji_prefix(text: str) -> str:
    if not text:
        return f"{FALLBACK_EMOJI} (no response)"
    stripped = text.lstrip()
    if any(stripped.startswith(e) for e in ALLOWED_EMOJIS):
        return text
    return f"{FALLBACK_EMOJI} {text}"


_TTS_STRIP_RE = re.compile("[‍️*#>]")
_EXTRA_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U0001F100-\U0001F1FF]"
)


def _clean_for_tts(text: str) -> str:
    """Strip characters that TTS engines read literally or can't render."""
    return _TTS_STRIP_RE.sub("", text)


def _strip_extra_emojis(text: str) -> str:
    """Keep only the leading allowed emoji; remove all other emoji characters.

    The model is instructed to use exactly one emoji from ALLOWED_EMOJIS as the
    first character. In practice it sprinkles decorative emojis through the
    response. Those are wasted tokens, clutter the logs, and risk Piper reading
    them aloud. This is the safety net.
    """
    if not text:
        return text
    ws_len = len(text) - len(text.lstrip())
    stripped = text[ws_len:]
    for e in ALLOWED_EMOJIS:
        if stripped.startswith(e):
            head = text[: ws_len + len(e)]
            body = text[ws_len + len(e):]
            return head + _EXTRA_EMOJI_RE.sub("", body)
    return _EXTRA_EMOJI_RE.sub("", text)


def _truncate_sentences(text: str, max_sentences: int = MAX_SENTENCES) -> str:
    count = 0
    for i, ch in enumerate(text):
        if ch in '.!?':
            count += 1
            if count >= max_sentences:
                return text[:i + 1]
    return text


# Content-filter severity tiers — all tiers return the same kid-safe replacement
# so no information is leaked about WHY the filter fired. Tier affects logging
# level and the Prometheus counter label, enabling different alert thresholds:
#
#   redirect — common profanity / slurs             → log.warning
#   log      — explicit sexual / graphic violence   → log.warning
#   alert    — hard drugs                           → log.error  (alert on this label)
_CF_TIER_REDIRECT_RE = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|bastard|cunt|nigger|nigga|faggot|retard(?:ed)?)\b",
    re.IGNORECASE,
)
_CF_TIER_LOG_RE = re.compile(
    r"\b(penis|vagina|orgasm|porn\w*|hentai|decapitat\w*|dismember\w*|mutilat\w*)\b",
    re.IGNORECASE,
)
_CF_TIER_ALERT_RE = re.compile(
    r"\b(cocaine|heroin|methamphetamine|fentanyl|ecstasy)\b",
    re.IGNORECASE,
)

_CONTENT_FILTER_REPLACEMENT = (
    f"{FALLBACK_EMOJI} Let's talk about something fun instead! "
    "What's your favorite animal?"
)

# Ordered highest-severity first so the most serious match wins when multiple
# tiers could fire on the same text.
_CF_TIERS: list[tuple[re.Pattern, str, int]] = [
    (_CF_TIER_ALERT_RE, "alert", logging.ERROR),
    (_CF_TIER_LOG_RE, "log", logging.WARNING),
    (_CF_TIER_REDIRECT_RE, "redirect", logging.WARNING),
]


def _content_filter(text: str) -> str | None:
    """Return a safe replacement if blocked content is found, else None.

    Checks three severity tiers. The kid-facing replacement is identical for
    all tiers; only log level and the Prometheus tier label differ, letting
    operators alert on ``tier="alert"`` without noising up lower-tier counts.
    """
    for pattern, tier, level in _CF_TIERS:
        match = pattern.search(text)
        if match:
            log.log(
                level,
                "content-filter-hit tier=%s pattern=%r pos=%d len=%d",
                tier, match.group(), match.start(), len(text),
            )
            if _METRICS_AVAILABLE:
                _safe_metric(dotty_content_filter_hits_total.labels(tier=tier).inc)
            return _CONTENT_FILTER_REPLACEMENT
    return None


class _ConvoLogger:
    """Writes one NDJSON record per conversation turn to a daily log file."""

    def __init__(self, log_dir: Path) -> None:
        self._dir = log_dir
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._dir.chmod(0o700)
        except OSError:
            log.warning("convo log dir creation failed: %s", self._dir)

    def log_turn(
        self,
        *,
        channel: str,
        session_id: str,
        request_text: str,
        response_text: str,
        latency_ms: float,
        error: str | None = None,
        latency_phases: dict[str, float] | None = None,
        type: str = "chat",
    ) -> None:
        now = datetime.now(LOCAL_TZ)
        emoji_used = ""
        stripped = response_text.lstrip()
        for e in ALLOWED_EMOJIS:
            if stripped.startswith(e):
                emoji_used = e
                break
        record = {
            "ts": now.isoformat(),
            "type": type,
            "channel": channel or "",
            "session_id": session_id,
            "request_text": request_text,
            "response_len": len(response_text),
            "response_text": response_text,
            "emoji_used": emoji_used,
            "latency_ms": round(latency_ms),
            "error": error,
        }
        if latency_phases:
            record["latency_phases"] = latency_phases
        path = self._dir / f"convo-{now.strftime('%Y-%m-%d')}.ndjson"
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            path.chmod(0o600)
        except Exception:
            log.warning("convo log write failed", exc_info=True)
        _dashboard_broadcast_turn(
            channel=channel or "",
            request_text=request_text,
            response_text=response_text,
            latency_ms=latency_ms,
            error=error,
            emoji_used=emoji_used,
            ts_iso=now.isoformat(),
            latency_phases=latency_phases,
        )


_convo_log = _ConvoLogger(CONVO_LOG_DIR)


# --- Portal event broadcast (P12, P13) -----------------------------------
# In-process pub/sub for completed turns. Subscribers get an asyncio.Queue
# they can drain; the bridge pushes to all queues after each log_turn.
_dashboard_event_listeners: list[asyncio.Queue] = []


def _dashboard_subscribe_events() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _dashboard_event_listeners.append(q)
    return q


def _dashboard_unsubscribe_events(q: asyncio.Queue) -> None:
    try:
        _dashboard_event_listeners.remove(q)
    except ValueError:
        pass


def _dashboard_broadcast_turn(*, channel: str, request_text: str,
                           response_text: str, latency_ms: float,
                           error: str | None, emoji_used: str,
                           ts_iso: str,
                           latency_phases: dict[str, float] | None = None) -> None:
    if not _dashboard_event_listeners:
        return
    event = {
        "ts": ts_iso,
        "channel": channel,
        "request_text": request_text,
        "response_text": response_text,
        "latency_ms": round(latency_ms),
        "error": error,
        "emoji_used": emoji_used,
    }
    if latency_phases:
        event["latency_phases"] = latency_phases
    for q in list(_dashboard_event_listeners):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# --- Perception event bus (Phase 1) --------------------------------------
# In-process pub/sub for ambient perception events emitted by firmware
# producers (face_detected, face_lost, sound_event, ...) via the
# xiaozhi-server event relay, and later by server-side classifiers
# (audio scene, vision). Mirrors the _dashboard_event_listeners pattern.
# Phase 1 has no consumers wired yet — landed standalone so producers
# and tests can validate the surface before consumers are added.
_perception_listeners: list[asyncio.Queue] = []
_perception_state: dict[str, dict] = {}
_PERCEPTION_STALE_THRESHOLD_S: float = 30.0  # idle > 30 s → stale

# In-memory ring of the most recent perception events per device, used by the
# dashboard's "Scene context" panel to show what Dotty has lately seen / heard.
# Bounded so a chatty firmware can't grow the bridge's RSS unbounded; one
# deque per device, dropped LRU-style when a new device first appears would
# require an active eviction policy — for a single-device deployment this is
# effectively a global ring. Text-only: matches the user constraint that no
# raw media bytes get persisted (these events carry only labels + scalars).
_PERCEPTION_RECENT_MAX: int = 20
_perception_recent_events: dict[str, "collections.deque[dict]"] = {}


def _perception_subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _perception_listeners.append(q)
    return q


def _perception_unsubscribe(q: asyncio.Queue) -> None:
    try:
        _perception_listeners.remove(q)
    except ValueError:
        pass


def _perception_recent_append(event: dict) -> None:
    """Push the event onto the per-device ring buffer (bounded). Mirrors the
    structure security_watch.RECENT_CYCLES uses but for raw perception events
    rather than security cycles. Called from the central broadcast hook so
    every event sees the same fan-out."""
    device_id = event.get("device_id") or ""
    if not device_id or device_id == "unknown":
        return
    ring = _perception_recent_events.get(device_id)
    if ring is None:
        ring = collections.deque(maxlen=_PERCEPTION_RECENT_MAX)
        _perception_recent_events[device_id] = ring
    ring.append({
        "ts": event.get("ts"),
        "name": event.get("name"),
        "data": event.get("data") or {},
    })


def get_recent_perception(device_id: str, limit: int | None = None) -> list[dict]:
    """Return the most-recent perception events for ``device_id`` (newest first)."""
    ring = _perception_recent_events.get(device_id)
    if not ring:
        return []
    items = list(ring)
    items.reverse()
    if limit is not None:
        items = items[:limit]
    return items


def _perception_broadcast(event: dict) -> None:
    # Bounded label cardinality: only count names we know about so a
    # buggy or malicious payload can't blow up the time-series count.
    name = event.get("name") or ""
    if _METRICS_AVAILABLE and name in (
        "face_detected", "face_lost", "sound_event", "state_changed",
    ):
        _safe_metric(
            dotty_perception_events_total.labels(type=name).inc,
        )
    # Recent-events ring — text-only, in-memory, bounded. Hooks the
    # dashboard's "Scene context" panel without altering the producer
    # contract or persisting anything to disk.
    _perception_recent_append(event)
    if not _perception_listeners:
        return
    for q in list(_perception_listeners):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            log.warning(
                "perception queue full, dropping event: %s",
                event.get("name"),
            )


def _update_perception_state(device_id: str, name: str,
                             data: dict, ts: float) -> None:
    """Mutate per-device state. Convenience fields read by the
    engagement gate (Phase 4) and Phase 1 consumers."""
    state = _perception_state.setdefault(device_id, {})
    state["last_event_t"] = ts
    state["last_event_name"] = name
    if name == "face_detected":
        state["face_present"] = True
        state["last_face_t"] = ts
    elif name == "face_lost":
        state["face_present"] = False
        state["last_face_lost_t"] = ts
        # Drop the cached identity — the dashboard derives "identified vs just
        # detected" from this, so leaving stale `last_face_id` would keep the
        # face pixel green after the person walked away.
        state.pop("last_face_id", None)
        # Mood is bound to the person; clear when they leave so the
        # next talk turn doesn't ship stale "engaged" into the prompt.
        state.pop("face_mood", None)
        state.pop("face_mood_t", None)
    elif name == "sound_event":
        state["last_sound_dir"] = data.get("direction")
        state["last_sound_t"] = ts
        state["last_sound_energy"] = data.get("energy")
    elif name == "state_changed":
        # Phase 4 — track the firmware's high-level State so consumers can gate
        # behaviour on it (e.g. greeter skips during security; ambient awareness
        # only runs in idle). Set by StateManager::emitStateChanged on every
        # transition.
        new_state = (data.get("state") or "").strip().lower()
        if new_state:
            state["current_state"] = new_state
            state["last_state_change_t"] = ts
            # Keep dance_active in sync with current_state as a fallback —
            # dance_started/ended events are the primary signal, but if one
            # is dropped we still won't get stuck with the flag inverted.
            if new_state == "dance":
                state["dance_active"] = True
            elif state.get("dance_active"):
                state["dance_active"] = False
    elif name == "dance_started":
        state["dance_active"] = True
        state["last_dance_started_t"] = ts
    elif name == "dance_ended":
        state["dance_active"] = False
        state["last_dance_ended_t"] = ts
    elif name == "chat_status":
        # Edge-only emission from firmware (stackchan_display.cc) on
        # LISTENING <-> not-LISTENING transitions. Drives the dashboard's
        # listening LED mirror.
        listening = bool(data.get("listening"))
        state["listening"] = listening
        state["last_chat_status_t"] = ts
    elif name == "face_recognized":
        # Synthetic event broadcast by /api/vision/explain when the room-view
        # VLM matches a household roster member. Track the identity so the
        # dashboard can paint the face pixel green vs yellow.
        identity = (data.get("identity") or "").strip()
        if identity:
            state["last_face_id"] = identity
            state["last_face_recognized_t"] = ts


def _is_dance_active(device_id: str) -> bool:
    """True iff the device is currently in DANCE state. Used to gate
    autonomous photos and TTS so they don't interrupt a dance."""
    return bool(_perception_state.get(device_id, {}).get("dance_active"))


def _current_device_state(device_id: str) -> str:
    """Convenience accessor — returns the last known firmware State for a
    device, or 'idle' if no state_changed event has been seen yet (default
    on boot before StateManager fires its first transition). Consumers that
    need to gate on state should call this."""
    return _perception_state.get(device_id, {}).get("current_state", "idle")


async def _dispatch_abort(device_id: str) -> None:
    """Phase 1.2 follow-up: send xiaozhi admin abort to stop in-flight
    TTS for a device. Reused by the face-lost aborter so Dotty stops
    talking when its audience walks away mid-response."""
    if not _XIAOZHI_HOST:
        return
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/abort"
    payload = {"device_id": device_id}

    def _post() -> None:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning(
                    "face-lost abort %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("face-lost abort failed: %s", exc)

    await asyncio.to_thread(_post)


async def _perception_face_lost_aborter() -> None:
    """On face_lost, if a greeting recently fired and the user hasn't
    walked back into frame within the grace period, fire xiaozhi admin
    abort so Dotty stops talking to empty space.

    Two-stage filter:
      1. Only acts within FACE_LOST_ABORT_WINDOW_SEC of the last greet
         (long-finished conversations are left alone).
      2. Schedules the abort FACE_LOST_ABORT_GRACE_SEC in the future
         and cancels it if face_detected fires for the same device
         before then. This protects greet/listen cycles from being
         killed by a transient face_lost (head turn, blink, brief
         occlusion) — the firmware face tracker is sensitive enough
         that without this, the aborter ate every turn empirically.
    """
    log.info(
        "perception face-lost aborter started (window=%.0fs grace=%.1fs)",
        FACE_LOST_ABORT_WINDOW_SEC, FACE_LOST_ABORT_GRACE_SEC,
    )
    pending: dict[str, asyncio.Task] = {}

    async def _delayed_abort(device_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            log.info(
                "face_lost → abort: device=%s (face stayed lost %.1fs)",
                device_id, delay,
            )
            await _dispatch_abort(device_id)
        except asyncio.CancelledError:
            log.info(
                "face_lost abort cancelled (face returned): device=%s",
                device_id,
            )
            raise

    q = _perception_subscribe()
    try:
        while True:
            event = await q.get()
            name = event.get("name")
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue

            if name == "face_detected":
                t = pending.pop(device_id, None)
                if t and not t.done():
                    t.cancel()
                continue

            if name != "face_lost":
                continue

            now = event.get("ts", 0.0)
            state = _perception_state.setdefault(device_id, {})
            last_greet = state.get("last_face_greet_t", 0.0)
            if now - last_greet > FACE_LOST_ABORT_WINDOW_SEC:
                continue

            prior = pending.pop(device_id, None)
            if prior and not prior.done():
                prior.cancel()
            log.info(
                "face_lost → schedule abort in %.1fs: device=%s (greet %.1fs ago)",
                FACE_LOST_ABORT_GRACE_SEC, device_id, now - last_greet,
            )
            pending[device_id] = asyncio.create_task(
                _delayed_abort(device_id, FACE_LOST_ABORT_GRACE_SEC),
            )
    except asyncio.CancelledError:
        log.info("perception face-lost aborter cancelled")
        for t in pending.values():
            if not t.done():
                t.cancel()
        raise
    except Exception:
        log.exception("perception face-lost aborter crashed")
    finally:
        _perception_unsubscribe(q)


async def _dispatch_face_greeting(device_id: str, text: str) -> None:
    """Phase 1.5 helper: fire-and-forget POST to the xiaozhi admin
    inject-text route, same path the dashboard greeter uses."""
    if not _XIAOZHI_HOST:
        log.warning("face greeter: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return
    if _is_dance_active(device_id):
        log.info("face greeter: suppressed (dance active): device=%s", device_id)
        return
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/inject-text"
    payload = {"text": text, "device_id": device_id}

    def _post() -> None:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning(
                    "face greeter inject-text %s: %s",
                    r.status_code, r.text[:200],
                )
        except Exception as exc:
            log.warning("face greeter inject-text failed: %s", exc)

    await asyncio.to_thread(_post)


async def _dispatch_say(device_id: str, text: str) -> None:
    """Layer 6 helper: fire-and-forget POST to the xiaozhi admin /say
    route, which streams TTS opus packets straight to the device WS
    bypassing the ASR/LLM pipeline. Used by ProactiveGreeter so a
    server-generated greeting plays as Dotty's speech rather than
    being treated as a fake user utterance (which is what
    /admin/inject-text → startToChat does)."""
    if not _XIAOZHI_HOST:
        log.warning("greeter say: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return
    if _is_dance_active(device_id):
        log.info("greeter say: suppressed (dance active): device=%s", device_id)
        return
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/say"
    payload = {"text": text, "device_id": device_id}

    def _post() -> None:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning(
                    "greeter say %s: %s",
                    r.status_code, r.text[:200],
                )
        except Exception as exc:
            log.warning("greeter say failed: %s", exc)

    await asyncio.to_thread(_post)


async def _dispatch_set_head_angles(device_id: str, yaw: int,
                                     pitch: int, speed: int) -> None:
    """Phase 1.6 helper: fire-and-forget POST to the new
    /xiaozhi/admin/set-head-angles route to send a direct MCP
    head-angles frame to the device."""
    if not _XIAOZHI_HOST:
        log.warning("sound turn: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-head-angles"
    payload = {
        "device_id": device_id, "yaw": yaw, "pitch": pitch, "speed": speed,
    }

    def _post() -> None:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning(
                    "sound turn set-head-angles %s: %s",
                    r.status_code, r.text[:200],
                )
        except Exception as exc:
            log.warning("sound turn set-head-angles failed: %s", exc)

    await asyncio.to_thread(_post)


async def _dispatch_set_state(device_id: str, state: str) -> bool:
    """Phase 4 helper: fire MCP self.robot.set_state at the firmware via the
    /xiaozhi/admin/set-state route. State must be one of:
    idle / talk / story_time / security / sleep / dance.
    Returns True on 2xx, False otherwise (and logs)."""
    if not _XIAOZHI_HOST:
        log.warning("set_state: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return False
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-state"
    payload = {"device_id": device_id, "state": state}

    def _post() -> bool:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("set_state %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("set_state failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


async def _dispatch_set_face_identified(device_id: str) -> bool:
    """Fire MCP self.robot.set_face_identified at the firmware via the
    /xiaozhi/admin/set-face-identified route. Lights the right-ring face
    pixel green for ~4 seconds (firmware-side timeout). Failures are
    non-fatal — name-greeting still played, the missing LED is cosmetic.
    """
    if not _XIAOZHI_HOST:
        log.warning("set_face_identified: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return False
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-face-identified"
    payload = {"device_id": device_id}

    def _post() -> bool:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("set_face_identified %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("set_face_identified failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


async def _dispatch_set_toggle(device_id: str, name: str, enabled: bool) -> bool:
    """Phase 4 helper: fire MCP self.robot.set_toggle at the firmware via the
    /xiaozhi/admin/set-toggle route. Toggle name must be one of:
    kid_mode / smart_mode. Returns True on 2xx, False otherwise (and logs)."""
    if not _XIAOZHI_HOST:
        log.warning("set_toggle: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return False
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-toggle"
    payload = {"device_id": device_id, "name": name, "enabled": enabled}

    def _post() -> bool:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("set_toggle %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("set_toggle failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


async def _perception_sound_turner() -> None:
    """Phase 1.6 consumer: on sound_event, turn the head toward the
    sound direction (left / centre / right) via direct MCP. Idle-only
    behaviour — face wins, conversation wins.
    """
    log.info(
        "perception sound turner started (cooldown=%.0fs yaw=±%d speed=%d)",
        SOUND_TURN_COOLDOWN_SEC, SOUND_TURN_YAW_DEG, SOUND_TURN_SPEED,
    )
    q = _perception_subscribe()
    try:
        while True:
            event = await q.get()
            if event.get("name") != "sound_event":
                continue
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue
            direction = (event.get("data") or {}).get("direction", "")
            if direction not in ("left", "centre", "center", "right"):
                continue

            now = event.get("ts", 0.0)
            state = _perception_state.setdefault(device_id, {})
            # Idle-only: face wins, conversation wins.
            if state.get("face_present"):
                continue
            last_chat = state.get("last_chat_t", 0.0)
            if now - last_chat < 30.0:
                continue
            last_turn = state.get("last_sound_turn_t", 0.0)
            if now - last_turn < SOUND_TURN_COOLDOWN_SEC:
                continue
            state["last_sound_turn_t"] = now

            if direction == "left":
                yaw = -SOUND_TURN_YAW_DEG
            elif direction == "right":
                yaw = SOUND_TURN_YAW_DEG
            else:
                yaw = 0
            log.info(
                "sound_event → head-turn: device=%s direction=%s yaw=%d",
                device_id, direction, yaw,
            )
            _spawn(
                _dispatch_set_head_angles(
                    device_id, yaw, 0, SOUND_TURN_SPEED,
                ),
                name="dispatch_set_head_angles",
            )
            head_turn_data = {
                "yaw": yaw, "pitch": 0, "speed": SOUND_TURN_SPEED,
                "reason": "sound_localizer", "direction": direction,
                "energy": (event.get("data") or {}).get("energy"),
            }
            _update_perception_state(device_id, "head_turn", head_turn_data, now)
            _perception_broadcast({
                "device_id": device_id, "ts": now,
                "name": "head_turn", "data": head_turn_data,
            })
    except asyncio.CancelledError:
        log.info("perception sound turner cancelled")
        raise
    except Exception:
        log.exception("perception sound turner crashed")
    finally:
        _perception_unsubscribe(q)


async def _perception_wake_word_turner() -> None:
    """On wake_word_detected, turn the head toward the speaker.

    Distinct intent from _perception_sound_turner above:
      - sound_turner   = "curious about an ambient noise" (cooldown'd, gentler)
      - wake_word_turn = "look at the user who summoned me" (deliberate, no cooldown, faster)

    Skips when a face is already being tracked — face_tracking owns the
    gaze in that case and we don't want to override it. Skips on
    direction=centre because there's no spatial info to act on.

    Updates state["last_sound_turn_t"] so the ambient sound turner above
    doesn't immediately re-fire on the user's continued voice.
    """
    if not WAKE_TURN_ENABLED:
        log.info("perception wake-word turner disabled by env")
        return
    log.info(
        "perception wake-word turner started (yaw=±%d speed=%d)",
        WAKE_TURN_YAW_DEG, WAKE_TURN_SPEED,
    )
    q = _perception_subscribe()
    try:
        while True:
            event = await q.get()
            if event.get("name") != "wake_word_detected":
                continue
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue
            data = event.get("data") or {}
            direction = data.get("direction", "")
            if direction not in ("left", "right"):  # skip centre / unknown
                continue

            state = _perception_state.setdefault(device_id, {})
            if state.get("face_present"):
                # Face tracker is already pointing the head at someone;
                # don't yank it elsewhere on a wake from a different
                # direction. (Likely the speaker IS the tracked face.)
                continue

            yaw = -WAKE_TURN_YAW_DEG if direction == "left" else WAKE_TURN_YAW_DEG
            now = event.get("ts", 0.0)
            # Suppress the ambient sound turner from immediately re-firing
            # on the user's continued voice after the wake word.
            state["last_sound_turn_t"] = now

            log.info(
                "wake_word_detected → head-turn: device=%s phrase=%r dir=%s yaw=%d",
                device_id, data.get("phrase", ""), direction, yaw,
            )
            _spawn(
                _dispatch_set_head_angles(
                    device_id, yaw, 0, WAKE_TURN_SPEED,
                ),
                name="dispatch_set_head_angles_wake",
            )
            head_turn_data = {
                "yaw": yaw, "pitch": 0, "speed": WAKE_TURN_SPEED,
                "reason": "wake_word", "direction": direction,
                "phrase": data.get("phrase", ""),
            }
            _update_perception_state(device_id, "head_turn", head_turn_data, now)
            _perception_broadcast({
                "device_id": device_id, "ts": now,
                "name": "head_turn", "data": head_turn_data,
            })
    except asyncio.CancelledError:
        log.info("perception wake-word turner cancelled")
        raise
    except Exception:
        log.exception("perception wake-word turner crashed")
    finally:
        _perception_unsubscribe(q)


async def _handle_face_recognized(event: dict) -> None:
    """Named-recognition acknowledger: on `face_recognized`, look up
    the identity in the household registry and speak `"Oh, it's
    <display_name>!"` so the user gets explicit proof of recognition.

    Independent of the bare-greet `face_detected` path and the rich
    ProactiveGreeter — those still fire on their own cadence. Uses
    `_dispatch_say` (TTS, bypasses ASR/LLM) so it plays as Dotty's
    own speech rather than a fake user utterance.
    """
    device_id = event.get("device_id", "")
    if not device_id or device_id == "unknown":
        return
    data = event.get("data") or {}
    identity = data.get("identity") or ""
    if not identity:
        return
    if _household_registry is None:
        return
    person = _household_registry.get(identity)
    if person is None or not person.display_name:
        log.debug("face_recognized: identity=%s not in roster", identity)
        return
    now = event.get("ts", 0.0)
    state = _perception_state.setdefault(device_id, {})
    last_chat = state.get("last_chat_t", 0.0)
    if now - last_chat < FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC:
        log.debug(
            "face_recognized → suppressed (chat fresh): device=%s identity=%s",
            device_id, identity,
        )
        return
    name_greets = state.setdefault("last_name_greet_t", {})
    last_named = name_greets.get(identity, 0.0)
    if now - last_named < FACE_NAME_GREET_MIN_INTERVAL_SEC:
        return
    name_greets[identity] = now
    text = FACE_NAME_GREET_TEMPLATE.format(name=person.display_name)
    log.info(
        "face_recognized → name-greet: device=%s identity=%s text=%r",
        device_id, identity, text,
    )
    _spawn(
        _dispatch_say(device_id, text),
        name="dispatch_name_greet",
    )
    # Light the right-ring face pixel green to mirror the named greeting.
    # Firmware auto-times-out after ~4 s; bridge can refresh by calling again
    # if the same face stays in frame across multiple recognitions.
    _spawn(
        _dispatch_set_face_identified(device_id),
        name="dispatch_set_face_identified",
    )


async def _perception_face_greeter() -> None:
    """Phase 1.5 consumer: on face_detected events, fire a brief
    audible greeting through the existing inject-text path so the
    user knows the robot saw them. Cooldown'd per device.

    The plan called for a 5 s manual-listen window. The xiaozhi
    protocol's `listen` frames are device→server only, so a true
    server-driven mic-open requires a firmware change (tracked as
    a Phase 1.2 follow-up). Greeting the user is the same spirit
    on the existing surface and is the natural seed for Phase 4
    curiosity / boredom mode behaviour.
    """
    log.info(
        "perception face greeter started (min_interval=%.0fs text=%r)",
        FACE_GREET_MIN_INTERVAL_SEC, FACE_GREET_TEXT,
    )
    log.info(
        "perception named acknowledger active (min_interval=%.0fs template=%r quiet_after_chat=%.0fs)",
        FACE_NAME_GREET_MIN_INTERVAL_SEC, FACE_NAME_GREET_TEMPLATE,
        FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC,
    )
    q = _perception_subscribe()
    try:
        while True:
            event = await q.get()
            ev_name = event.get("name")
            if ev_name == "face_recognized":
                await _handle_face_recognized(event)
                continue
            if ev_name != "face_detected":
                continue
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue
            # Time-of-day gate. Sensor-noise frames in low light can trip
            # the face detector; greeting an empty room at 3 am is the
            # highest-value thing to suppress. Default window 06–21
            # (LOCAL_TZ); see FACE_GREET_HOUR_START / _END env vars.
            current_hour = datetime.now(LOCAL_TZ).hour
            if not (FACE_GREET_HOUR_START <= current_hour < FACE_GREET_HOUR_END):
                log.debug(
                    "face_detected → suppressed (outside %d-%d window): device=%s",
                    FACE_GREET_HOUR_START, FACE_GREET_HOUR_END, device_id,
                )
                continue
            # Layer 6 hand-off: if the household has a roster (anyone
            # with an `appearance:` field), the room-view roster match
            # path will fire its own contextual greeting via
            # ProactiveGreeter within ~1-2 s. Suppress the bare "Hi!"
            # to avoid stacking it on top of "Hey Hudson, library day!".
            # Empty roster (no household.yaml or no appearances) → keep
            # the bare "Hi!" alive so unconfigured deployments still
            # acknowledge faces.
            if _household_registry is not None and \
                    _household_registry.roster_ids_with_appearance():
                log.debug(
                    "face_detected → suppressed (roster owns greeting): "
                    "device=%s", device_id,
                )
                continue
            now = event.get("ts", 0.0)
            state = _perception_state.setdefault(device_id, {})
            last_greet = state.get("last_face_greet_t", 0.0)
            if now - last_greet < FACE_GREET_MIN_INTERVAL_SEC:
                continue
            state["last_face_greet_t"] = now
            # Empty FACE_GREET_TEXT disables the verbal injection — the
            # firmware-side WakeWordInvoke("face") still fires, so the
            # device opens its mic without the bridge saying "Hi!". This
            # gives "popup chime + mic open" rather than "popup chime +
            # 'Hi!' + mic open", which can feel quieter day-to-day.
            if not FACE_GREET_TEXT:
                log.info(
                    "face_detected → mic-only (FACE_GREET_TEXT empty): device=%s",
                    device_id,
                )
                continue
            log.info("face_detected → greeting: device=%s", device_id)
            _spawn(
                _dispatch_face_greeting(device_id, FACE_GREET_TEXT),
                name="dispatch_face_greeting",
            )
    except asyncio.CancelledError:
        log.info("perception face greeter cancelled")
        raise
    except Exception:
        log.exception("perception face greeter crashed")
    finally:
        _perception_unsubscribe(q)


# ---------------------------------------------------------------------------
# Purr-on-head-pet (Option B: server-pushed pre-rendered asset)
# ---------------------------------------------------------------------------

async def _dispatch_purr_audio(device_id: str) -> bool:
    """Push the purr asset to the device.

    Mirrors the inject-text dispatcher pattern used by the face greeter
    but targets a play-asset admin route on xiaozhi-server. The matching
    server-side admin route is a follow-up — until it lands, this call
    will log a warning and return False, but it MUST NOT crash the
    perception loop.

    Defensive contract:
      * Missing XIAOZHI_HOST → return False (no network attempt).
      * Network/HTTP failure → return False, log warning. Asset existence
        is checked server-side by xiaozhi-server's /play-asset route,
        which returns 404 if the path doesn't resolve in its own
        filesystem — the bridge surfaces that as a play-asset warning.
    """
    if not _XIAOZHI_HOST:
        log.warning("purr: XIAOZHI_HOST not set; cannot reach xiaozhi-server")
        return False
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/play-asset"
    payload = {"device_id": device_id, "asset": str(PURR_AUDIO_PATH)}

    def _post() -> bool:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning(
                    "purr play-asset %s: %s",
                    r.status_code, r.text[:200],
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


async def _perception_purr_player() -> None:
    """Consumer: on `head_pet_started` events, push the purr asset.

    Per-device cooldown stops a continuous head-pet from re-triggering
    the clip on every event burst. Bypasses kid-mode sandwich (the
    asset is curated bytes, not LLM-generated content). Extends
    `last_chat_t` by `PURR_DURATION_SEC` so the sound localizer
    (`_perception_sound_turner`) doesn't turn the head toward the
    speaker mid-purr — without that suppression the localizer would
    treat the purr's own audio as a sound event from the side.

    Firmware-side `head_pet_started` perception event emission is a
    separate task (see firmware/firmware/main/stackchan/modifiers/
    head_pet.h:82-91 for the existing visual-only handler). This
    consumer is ready for whenever that event lands on the bus.
    """
    log.info(
        "perception purr player started (cooldown=%.0fs asset=%s)",
        PURR_COOLDOWN_SEC, PURR_AUDIO_PATH,
    )
    q = _perception_subscribe()
    try:
        while True:
            event = await q.get()
            if event.get("name") != "head_pet_started":
                continue
            device_id = event.get("device_id", "")
            if not device_id or device_id == "unknown":
                continue
            now = event.get("ts", 0.0)
            state = _perception_state.setdefault(device_id, {})
            last_purr = state.get("last_purr_t", 0.0)
            if now - last_purr < PURR_COOLDOWN_SEC:
                continue
            state["last_purr_t"] = now
            # Suppress the sound-localiser head-turn while the purr
            # plays. Setting last_chat_t to now+duration is the
            # single hook the localiser already reads (it skips
            # turns when last_chat_t is fresh).
            state["last_chat_t"] = now + PURR_DURATION_SEC
            log.info("head_pet_started → purr: device=%s", device_id)
            _spawn(_dispatch_purr_audio(device_id), name="dispatch_purr_audio")
    except asyncio.CancelledError:
        log.info("perception purr player cancelled")
        raise
    except Exception:
        log.exception("perception purr player crashed")
    finally:
        _perception_unsubscribe(q)


# ---------------------------------------------------------------------------
# Fixed-audio asset allowlist
# ---------------------------------------------------------------------------
# Pre-rendered audio that bypasses the kid-mode content-filter sandwich
# because the bytes are curated, not LLM-generated. Add new assets here
# when you wire them into a perception consumer or admin route — keeps
# the "what plays without filtering" surface visible in one place.
_FIXED_AUDIO_ASSETS: tuple[Path, ...] = (PURR_AUDIO_PATH,)


# ---------------------------------------------------------------------------
# ProactiveGreeter (Layer 6) — adapters
# ---------------------------------------------------------------------------
# The greeter expects an object that exposes ``subscribe()`` ->
# ``asyncio.Queue`` and ``unsubscribe(q)``. Our perception bus is a pair
# of free functions (`_perception_subscribe` / `_perception_unsubscribe`)
# operating on a module-level listener list; the adapter below is the
# minimum shim needed to bridge the two shapes without altering the
# in-process bus surface that other consumers already rely on.
class _PerceptionBusAdapter:
    """Wraps the free-function perception bus to match the greeter's
    duck-typed dependency-injection contract."""

    @staticmethod
    def subscribe() -> asyncio.Queue:
        return _perception_subscribe()

    @staticmethod
    def unsubscribe(q: asyncio.Queue) -> None:
        _perception_unsubscribe(q)


class _CalendarFacade:
    """Wraps `_calendar_cache` + `summarize_for_prompt` into the
    `get_events()` / `summarize_for_prompt(events, person, include_household)`
    shape the greeter wants. Reads the cache lazily so a midnight roll or
    a fresh poll lands without a greeter restart. All branches are
    defensive — any raise here would propagate into the greeter's
    handler and be try/except-swallowed there, but we still degrade
    gracefully so the LLM-prompt path stays valid."""

    @staticmethod
    def get_events() -> list:
        try:
            return list(_calendar_cache.get("events") or [])
        except Exception:
            log.debug(
                "greeter calendar facade: get_events() raised", exc_info=True,
            )
            return []

    @staticmethod
    def summarize_for_prompt(
        events: list,
        *,
        person: str | None = None,
        include_household: bool = True,
    ) -> list[str]:
        try:
            return summarize_for_prompt(
                events,
                person=person,
                include_household=include_household,
            )
        except Exception:
            log.debug(
                "greeter calendar facade: summarize_for_prompt raised",
                exc_info=True,
            )
            return []


async def _greeter_llm_client(prompt: str) -> str:
    """LLM adapter for ProactiveGreeter. Routes through the same ACP
    client voice turns use. The resulting text is sent verbatim through
    `_dispatch_say` (TTS-direct), so we don't want voice wrapping
    applied here — what the greeter generates is exactly what Dotty
    speaks. Failures bubble up to the greeter, which has its own
    try/except + template fallback."""
    return await asyncio.wait_for(
        acp.prompt(prompt),
        timeout=REQUEST_TIMEOUT_SEC,
    )


async def _greeter_tts_pusher(device_id: str, text: str) -> None:
    """TTS adapter for ProactiveGreeter. Routes through the
    /xiaozhi/admin/say endpoint which generates TTS server-side and
    streams opus straight to the device WS — bypassing the ASR/LLM
    pipeline entirely so the greeter's pre-generated text is spoken
    verbatim. Errors are logged inside `_dispatch_say`; we add one
    more guard so an exception here can NEVER reach the greeter
    loop."""
    try:
        await _dispatch_say(device_id, text)
    except Exception:
        log.exception(
            "greeter tts pusher: _dispatch_say raised "
            "(device=%s)", device_id,
        )


# Lazily constructed in lifespan so unit-import of bridge.py stays cheap
# (the greeter reads env on construction).
_proactive_greeter: "ProactiveGreeter | None" = None  # noqa: F821

# Household registry — single source of truth for who lives here. Loaded
# from ~/.zeroclaw/household.yaml (overridable via HOUSEHOLD_YAML_PATH).
# Hot-reloads on file mtime change. None == registry init failed; bridge
# continues with no-one configured (every identity resolves to _household).
_household_registry: "HouseholdRegistry | None" = None  # noqa: F821

# Speaker resolver — Phase 1 of the family-companion identity work.
# Combines self-ID phrases, calendar prefix, time-of-day, and (when
# Layer 4 ships) face_recognized events into a single best-guess
# `SpeakerResolution` per voice turn. None == disabled (no registry).
_speaker_resolver: "SpeakerResolver | None" = None  # noqa: F821

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reconcile voice-daemon model with smart_mode state before anything
    # else — if the bridge was redeployed and config.toml drifted, this
    # rewrites + schedules a restart so the daemon comes up on the right
    # model. No-op when in sync.
    try:
        _reconcile_voice_model_at_startup()
    except Exception:
        log.exception("startup model reconcile raised — continuing anyway")
    try:
        async with app_lock:
            await acp.ensure_alive()
    except Exception:
        log.exception("Initial ACP spawn failed — will retry on first request")
    try:
        await _refresh_caches()
        log.info("context-primed weather=%r calendar_events=%d",
                 _weather_cache["text"][:60] if _weather_cache["text"] else "(none)",
                 len(_calendar_cache["events"]))
    except Exception:
        log.exception("Initial context fetch failed — will retry on first request")
    # Phase 1.5 / 1.6: start perception subscriber tasks
    perception_tasks = [
        asyncio.create_task(_perception_face_greeter()),
        asyncio.create_task(_perception_wake_word_turner()),
        asyncio.create_task(_perception_face_lost_aborter()),
        asyncio.create_task(_perception_purr_player()),
    ]
    # Security state capture loop — text-only photo + audio every
    # SECURITY_CAPTURE_INTERVAL_SEC, JPEG/audio bytes discarded after
    # VLM/ASR. See bridge/security_watch.py for the contract.
    try:
        from bridge.security_watch import (
            run_security_consumer, set_vision_cache_writer,
        )

        def _security_vision_cache_writer(
            device_id: str,
            *,
            jpeg_bytes: bytes,
            description: str,
            source: str = "security_capture",
        ) -> None:
            """Mutate _vision_cache from a security cycle. The dashboard's
            /ui/host/robot/photo/{mac} endpoint then serves the JPEG and
            the new /ui/security/recent/{mac} panel surfaces the source
            label so the operator can tell apart room_view vs security
            captures. In-memory only — no disk write — per the user's
            text-only-storage constraint for media."""
            try:
                _vision_cache[device_id] = {
                    "description": description or "",
                    "timestamp": perf_counter(),
                    "wall_ts": time.time(),
                    "jpeg_bytes": jpeg_bytes,
                    "question": "security capture",
                    "room_match_person_id": None,
                    "source": source,
                }
            except Exception:
                log.warning("security vision_cache write failed", exc_info=True)

        set_vision_cache_writer(_security_vision_cache_writer)
        perception_tasks.append(asyncio.create_task(
            run_security_consumer(_perception_subscribe, _perception_unsubscribe)
        ))
    except Exception:
        log.exception("security capture consumer failed to start")
    # sound_turner disabled by default — on-device SoundLocalizer has a
    # stuck-left bias (balance saturates at ~0.998), causing the head to
    # snap to yaw=-45 every few seconds. Re-enable once the firmware
    # localizer has a calibrated L/R baseline.
    if os.environ.get("DOTTY_SOUND_TURNER_ENABLED", "0") == "1":
        perception_tasks.append(asyncio.create_task(_perception_sound_turner()))
    else:
        log.info("perception sound turner disabled (DOTTY_SOUND_TURNER_ENABLED!=1)")
    # Scene synthesis — periodic + event-triggered "current environment"
    # writer. Reads _vision_cache / _audio_cache / _perception_state,
    # writes a one-line synthesis to a daily NDJSON ring file. Always
    # safe to run; emits only when there's at least vision-or-audio
    # signal to summarise.
    perception_tasks.append(asyncio.create_task(_scene_synthesis_loop()))
    # Idle photographer — silent take_photo every 3–5 min while idle.
    # Notable descriptions land in perception-YYYY-MM-DD.ndjson for
    # later FTS ingestion. Toggle with IDLE_PHOTOGRAPHER_ENABLED.
    if IDLE_PHOTOGRAPHER_ENABLED:
        perception_tasks.append(
            asyncio.create_task(_perception_idle_photographer())
        )
    else:
        log.info("perception idle photographer disabled (IDLE_PHOTOGRAPHER_ENABLED!=1)")
    # Sleep dreamer — on state→sleep, schedule N dreams across the
    # estimated sleep window. Cancels on early wake. Writes summaries
    # + full text to dreams-YYYY-MM-DD.ndjson.
    if DREAMER_ENABLED:
        perception_tasks.append(
            asyncio.create_task(_perception_sleep_dreamer())
        )
    else:
        log.info("perception sleep dreamer disabled (DREAMER_ENABLED!=1)")
    # Dance reflector — on dance_ended, fire a short narrative LLM
    # reflection. Silent. Writes to dances-YYYY-MM-DD.ndjson.
    if DANCE_REFLECTOR_ENABLED:
        perception_tasks.append(
            asyncio.create_task(_perception_dance_reflector())
        )
    else:
        log.info("perception dance reflector disabled (DANCE_REFLECTOR_ENABLED!=1)")
    # Layer 5: background calendar refresher (no-op when CALENDAR_IDS empty).
    calendar_task = asyncio.create_task(_calendar_poll_loop())

    # Household registry — load before the greeter so it can enrich
    # greetings with display name, persona, and birthday awareness. A
    # missing/malformed file leaves the registry empty, not absent.
    global _household_registry
    try:
        from bridge.household import HouseholdRegistry
        _household_registry = HouseholdRegistry()
        log.info(
            "household registry loaded from %s (%d people)",
            _household_registry.path,
            len(tuple(_household_registry.iter())),
        )
    except Exception:
        log.exception(
            "HouseholdRegistry init failed — continuing without it",
        )
        _household_registry = None

    # Speaker resolver — needs the registry to be useful, but can be
    # constructed even with an empty one (it'll just always fall back).
    # The resolver itself is dependency-light so failures here are
    # extremely unlikely; defensive try/except matches the pattern used
    # by every other lifespan-init component.
    global _speaker_resolver
    try:
        from bridge.speaker import SpeakerResolver
        _speaker_resolver = SpeakerResolver(
            registry=_household_registry,
            calendar_provider=lambda: (_calendar_cache.get("events") or []),
            # perception_provider stays None until Phase 4 (face-rec
            # firmware) ships — no recent-events buffer to pull from yet.
            perception_provider=None,
        )
        log.info("SpeakerResolver initialised (sticky=%.0fs ask_threshold=%.2f)",
                 _speaker_resolver.sticky_seconds,
                 _speaker_resolver.ask_threshold)
    except Exception:
        log.exception(
            "SpeakerResolver init failed — voice turns will use legacy path",
        )
        _speaker_resolver = None

    # Layer 6: proactive greeter. Defensive — a construct-or-start failure
    # must never block the bridge from booting (voice path comes first).
    global _proactive_greeter
    try:
        from bridge.proactive_greeter import ProactiveGreeter
        _proactive_greeter = ProactiveGreeter(
            perception_bus=_PerceptionBusAdapter(),
            llm_client=_greeter_llm_client,
            calendar_cache=_CalendarFacade(),
            tts_pusher=_greeter_tts_pusher,
            kid_mode_provider=lambda: KID_MODE,
            household_registry=_household_registry,
            turn_logger=_convo_log.log_turn,
        )
        _proactive_greeter.start()
    except Exception:
        log.exception(
            "ProactiveGreeter start failed — continuing without it",
        )
        _proactive_greeter = None

    yield
    for t in perception_tasks:
        t.cancel()
    calendar_task.cancel()
    await asyncio.gather(*perception_tasks, calendar_task, return_exceptions=True)
    if _proactive_greeter is not None:
        try:
            await _proactive_greeter.stop()
        except Exception:
            log.exception("ProactiveGreeter.stop() raised")
    await acp.shutdown()


app = FastAPI(title="ZeroClaw Bridge", lifespan=lifespan)

# Prometheus exposition. Mounted as an ASGI sub-app so it shares the
# bridge's listener — keep that listener LAN-only (bind 0.0.0.0 on a
# private network or 127.0.0.1 + a reverse proxy). NEVER expose /metrics
# to the public internet; it leaks operational details about the host.
if _METRICS_AVAILABLE and metrics_app is not None:
    try:
        app.mount("/metrics", metrics_app())
        log.info("Prometheus /metrics mounted")
    except Exception:
        log.exception("metrics mount failed — /metrics will be unavailable")

try:
    from bridge.dashboard import router as _dashboard_router, configure as _configure_dashboard
    app.include_router(_dashboard_router)
except Exception:
    log.exception("dashboard mount failed — admin UI at /ui will be unavailable")
    _configure_dashboard = None  # type: ignore[assignment]

# Vendored JS/CSS + PWA icons for the dashboard. Served same-origin so we
# can attach SRI to the <script>/<link> tags and drop the third-party CDNs
# (htmx.org / cdn.jsdelivr.net / cdn.tailwindcss.com). Re-build the
# tailwind bundle with `npm run build:css` after editing templates.
try:
    from fastapi.staticfiles import StaticFiles as _StaticFiles
    from pathlib import Path as _Path
    _STATIC_DIR = _Path(__file__).parent / "bridge" / "static"
    if _STATIC_DIR.is_dir():
        app.mount("/ui/static", _StaticFiles(directory=str(_STATIC_DIR)), name="ui-static")
    else:
        log.warning("dashboard static dir missing at %s — vendored assets will 404", _STATIC_DIR)
except Exception:
    log.exception("dashboard static mount failed — vendored assets at /ui/static will be unavailable")


@app.get("/health")
async def health() -> dict:
    proc_ok = acp._proc is not None and acp._proc.returncode is None
    return {
        "status": "ok",
        "service": "zeroclaw-bridge",
        "acp_running": proc_ok,
        "cached_session": acp._sid is not None,
        "session_turns": acp._sid_turns,
    }


@app.get("/api/calendar/today")
async def calendar_today(
    person: str | None = None,
    include_household: bool = True,
) -> dict:
    """LAN endpoint for today's calendar events.

    Routes through `summarize_for_prompt` so the response carries the
    same privacy guarantees as prompt injection: no ISO timestamps, no
    email addresses, no raw calendar IDs. Intended for the firmware /
    dashboard UI; deliberately NOT registered as an MCP tool because the
    firmware-side `MCP_TOOL_ALLOWLIST` is closed and we want this stay
    a passive read endpoint, not something the LLM can call.
    """
    t0 = perf_counter()
    err_kind: str | None = None
    try:
        # Triggers a lazy refresh if the cache is stale or the day rolled.
        await _refresh_caches()
        events = _calendar_cache.get("events") or []
        cleaned = summarize_for_prompt(
            events, person=person, include_household=include_household,
        )
        return {
            "ok": True,
            "date": _calendar_cache.get("date", ""),
            "fetched": _calendar_cache.get("fetched", 0.0),
            "consecutive_failures": _calendar_cache.get("consecutive_failures", 0),
            "person": person,
            "include_household": include_household,
            "events": cleaned,
            "count": len(cleaned),
        }
    except Exception:
        err_kind = "exception"
        raise
    finally:
        if _METRICS_AVAILABLE:
            _safe_metric(
                dotty_request_duration_seconds.labels(
                    endpoint="calendar_today",
                ).observe,
                perf_counter() - t0,
            )
            if err_kind:
                _safe_metric(
                    dotty_request_errors_total.labels(
                        endpoint="calendar_today", kind=err_kind,
                    ).inc,
                )


# ---------------------------------------------------------------------------
# Perception — ambient event ingest (Phase 1)
# ---------------------------------------------------------------------------


class PerceptionEventIn(BaseModel):
    device_id: str = "unknown"
    ts: float | None = None
    name: str
    data: dict = {}


@app.post("/api/perception/event", status_code=204)
async def perception_event(payload: PerceptionEventIn) -> None:
    """Ingest an ambient-perception event. Producers: firmware (via the
    xiaozhi-server relay) for face_detected / face_lost / sound_event,
    later phases add server-side audio scene + vision classifiers.
    Updates per-device state and broadcasts to all in-process
    subscribers (no consumers in Phase 1.1; added in 1.5 / 1.6)."""
    t0 = perf_counter()
    err_kind: str | None = None
    try:
        ts = payload.ts if payload.ts is not None else time.time()
        event = {
            "device_id": payload.device_id,
            "ts": ts,
            "name": payload.name,
            "data": payload.data or {},
        }
        _update_perception_state(
            payload.device_id, payload.name, event["data"], ts,
        )
        _perception_broadcast(event)
        log.info(
            "perception event: device=%s name=%s data=%s",
            payload.device_id, payload.name, event["data"],
        )
        return None
    except Exception:
        err_kind = "exception"
        raise
    finally:
        if _METRICS_AVAILABLE:
            _safe_metric(
                dotty_request_duration_seconds.labels(
                    endpoint="perception_event",
                ).observe,
                perf_counter() - t0,
            )
            if err_kind:
                _safe_metric(
                    dotty_request_errors_total.labels(
                        endpoint="perception_event", kind=err_kind,
                    ).inc,
                )


@app.get("/api/perception/state")
async def perception_state(device_id: str = "") -> dict:
    """Debug introspection — current per-device perception state.
    Used by Phase 1 verification + later by the dashboard.

    Each device entry is annotated with:
      sensor_stale  – True when no event has arrived within
                      _PERCEPTION_STALE_THRESHOLD_S seconds (or the
                      device has never sent an event).
      sensor_age_s  – Seconds since the last event (float("inf") when
                      last_event_t is absent).
    """
    now = time.time()

    def _annotate(raw: dict) -> dict:
        out = dict(raw)
        last_t = out.get("last_event_t")
        if last_t is None:
            age = float("inf")
        else:
            age = max(0.0, now - last_t)
        out["sensor_age_s"] = age
        out["sensor_stale"] = age > _PERCEPTION_STALE_THRESHOLD_S
        return out

    if device_id:
        return {device_id: _annotate(_perception_state.get(device_id, {}))}
    return {did: _annotate(s) for did, s in _perception_state.items()}


@app.get("/api/perception/feed")
async def perception_feed(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of live perception events.

    Each event arrives as:
        data: {"name": "...", "data": {...}, "device_id": "...", "ts": 1234.5}\\n\\n

    A keepalive comment (`: keepalive`) is sent every 15 s when idle.
    Connect with EventSource('/api/perception/feed') from the browser.
    """
    queue = _perception_subscribe()

    async def _generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    payload = {
                        "name": event.get("name", ""),
                        "data": event.get("data", {}),
                        "device_id": event.get("device_id", ""),
                        "ts": event.get("ts", 0.0),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _perception_unsubscribe(queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Vision — photo description via OpenRouter VLM
# ---------------------------------------------------------------------------

_vision_cache: dict[str, dict] = {}
_vision_events: dict[str, list[asyncio.Event]] = {}

# Audio caption cache — populated by POST /api/audio/explain. Mirrors
# _vision_cache shape but holds no raw audio bytes (caption text is the
# only durable surface; raw audio stays in the request body and is GC'd
# after the captioning call returns).
_audio_cache: dict[str, dict] = {}

# Per-device snapshot of the most recent scene synthesis — written by
# _scene_synthesis_loop, read by the dashboard's Perception card. Text
# only; the NDJSON sink under CONVO_LOG_DIR is the durable record.
_scene_synthesis_cache: dict[str, dict] = {}

def _call_narrative_llm(
    user_prompt: str,
    *,
    system_prompt: str,
    model: str = NARRATIVE_MODEL,
    max_tokens: int = 1200,
    temperature: float = 0.9,
    timeout_s: float = NARRATIVE_TIMEOUT_SEC,
) -> str | None:
    """Direct OpenRouter text-LLM call for internal narrative writes.

    Used for dreams, dance reflections, story summaries, and any other
    introspective text Dotty produces about their own experience.
    Bypasses ZeroClaw + the kid-mode sandwich — these are not voice
    output, they're cached narrative for memory.

    Returns the model's text response on success, or None on failure
    (network error, missing API key, malformed response). Callers
    treat None as "skip this write" rather than crashing the consumer.

    Higher temperature than VLM (0.9 vs 0.3) — narrative wants
    literary variation, not perceptual stability.
    """
    import requests as req

    if not VISION_API_KEY:
        log.warning("narrative LLM: VISION_API_KEY not set; skipping")
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = req.post(
            VISION_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {VISION_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("narrative LLM call failed (model=%s)", model)
        return None


def _call_vision_api(
    b64_image: str, question: str, *,
    system_prompt: str = VISION_SYSTEM_PROMPT,
) -> str:
    import requests as req

    if not VISION_API_KEY:
        log.warning("VISION_API_KEY not set")
        return "I couldn't quite see that clearly."
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                    {"type": "text", "text": question},
                ],
            },
        ],
        "max_tokens": 200,
        "temperature": 0.3,
    }
    try:
        resp = req.post(
            VISION_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {VISION_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=VISION_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("vision API call failed")
        return "I couldn't quite see that clearly."


# ---------------------------------------------------------------------------
# Room-view roster identification — description + 4-member name match
# ---------------------------------------------------------------------------
# Sentinel question from the xiaozhi side opts in to this path (see
# `_ROOM_VIEW_SENTINEL` above). The bridge builds the roster-aware
# question from the household registry on every call so YAML edits are
# picked up without restart.

# The exact reply format the VLM is asked to produce. Pinned to one
# line so a streaming or partial completion still parses; `DESC: ` and
# `NAME: ` are explicit markers the parser anchors on.
_ROOM_VIEW_PROMPT_TEMPLATE = (
    "Look at this photo and do THREE things in one reply.\n"
    "\n"
    "1. Describe the person in ONE short sentence — approximate age "
    "range, hair, clothing, distinguishing features.\n"
    "2. If the person clearly matches one of these family members, "
    "give that exact name. Otherwise reply with the name 'unknown'.\n"
    "3. Read the person's apparent mood — pick exactly one of: "
    "engaged, tired, excited, distressed, neutral.\n"
    "\n"
    "Family:\n"
    "{roster}\n"
    "\n"
    "Reply on a SINGLE line in this exact format:\n"
    "DESC: <one sentence> | NAME: <{name_choices}|unknown> | MOOD: <engaged|tired|excited|distressed|neutral>\n"
    "\n"
    "If you cannot see a person at all, reply with exactly: no one in view\n"
    "Do not invent names. Do not add commentary."
)
# Sentinel reply for empty frames — same string the v1 prompt used,
# so existing log-grep regexes keep working.
_ROOM_VIEW_NO_PERSON = "no one in view"
# Parser regex. Anchored at start, allows whitespace flexibility, and
# tolerates trailing punctuation around the name (e.g. `NAME: Hudson.`).
_ROOM_VIEW_RESP_RE = re.compile(
    r"^\s*DESC:\s*(?P<desc>.+?)\s*"
    r"\|\s*NAME:\s*(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*"
    # Accept ANY single-word MOOD value here so an out-of-vocab reply
    # ("chaotic") still parses the desc + name cleanly — the parser
    # validates the vocab and drops invalid moods to None.
    r"(?:\|\s*MOOD:\s*(?P<mood>[A-Za-z]+)\s*)?"
    r"[.!?]?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ROOM_VIEW_MOODS = frozenset(
    {"engaged", "tired", "excited", "distressed", "neutral"}
)


def _build_room_view_question() -> Optional[str]:
    """Build the roster-aware room_view prompt from the household
    registry. Returns None when the registry is unavailable or has no
    members with `appearance:` set — caller should fall back to the
    v1 description-only prompt."""
    if _household_registry is None:
        return None
    try:
        roster = _household_registry.render_roster_for_vlm()
    except Exception:
        log.exception("room_view: render_roster_for_vlm raised")
        return None
    if not roster.strip():
        return None
    try:
        name_choices = "|".join(sorted(
            p.display_name for p in _household_registry.iter()
            if (p.appearance or "").strip()
        ))
    except Exception:
        log.exception("room_view: roster name iteration raised")
        return None
    return _ROOM_VIEW_PROMPT_TEMPLATE.format(
        roster=roster, name_choices=name_choices,
    )


def _parse_room_view_response(
    raw: str, roster_ids: set[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse the VLM's room_view reply into `(description, person_id, mood)`.

    Behaviour:
      * Empty input  → (None, None, None)
      * "no one in view" sentinel → (None, None, None)
      * Format match + name in roster → (desc, person_id, mood_or_None)
      * Format match + name == "unknown" or off-roster → (desc, None, mood_or_None)
      * Format mismatch → (raw_stripped, None, None) — graceful degrade
        to v1 behaviour so we never lose the description signal even
        when the model deviates from the requested format.

    `mood` is None when the model omits the MOOD: field (older replies)
    or returns a value outside the fixed vocabulary. The MOOD field is
    optional in the regex precisely so older / non-conforming replies
    still parse cleanly.
    """
    if not raw:
        return None, None, None
    cleaned = raw.strip()
    if not cleaned:
        return None, None, None
    if _ROOM_VIEW_NO_PERSON in cleaned.lower():
        return None, None, None
    m = _ROOM_VIEW_RESP_RE.match(cleaned)
    if not m:
        # Fall back: treat the whole reply as a description. Mirrors the
        # v1 path so a botched format never costs us the description.
        return cleaned, None, None
    desc = m.group("desc").strip()
    name = m.group("name").strip().lower()
    raw_mood = (m.group("mood") or "").strip().lower()
    mood = raw_mood if raw_mood in _ROOM_VIEW_MOODS else None
    if not desc:
        desc = None  # paranoid — regex requires non-empty
    if name == "unknown" or name not in roster_ids:
        return desc, None, mood
    return desc, name, mood


@app.post("/api/vision/explain")
async def vision_explain(
    request: Request,
    question: str = Form("What do you see?"),
    file: UploadFile = File(...),
):
    t0 = perf_counter()
    err_kind: str | None = None
    try:
        device_id = request.headers.get("device-id", "unknown")
        jpeg_bytes = await file.read()
        log.info(
            "vision device=%s question=%s bytes=%d",
            device_id, question[:80], len(jpeg_bytes),
        )
        b64_image = base64.b64encode(jpeg_bytes).decode("ascii")

        # Room-view roster identification opt-in. The xiaozhi side
        # sends the sentinel in the `question` field when it wants the
        # bridge to substitute its roster-aware prompt + parse the
        # combined description-and-name reply. Falls back to the v1
        # description-only path if the registry is empty / unavailable
        # so the existing room_view behaviour is preserved.
        room_view_question = (
            _build_room_view_question() if question == _ROOM_VIEW_SENTINEL
            else None
        )
        room_match_person_id: str | None = None
        if room_view_question is not None:
            # Idle photo cooldown — skip the VLM call if we've already
            # captured an autonomous photo for this device within the
            # cooldown window. Cache + waiter wake still happen so the
            # firmware doesn't time out on /api/vision/latest.
            wall_now = time.time()
            state = _perception_state.setdefault(device_id, {})
            last_capture = state.get("last_room_view_capture_t", 0.0)
            cooldown_age = wall_now - last_capture
            dance_active = _is_dance_active(device_id)
            if dance_active or cooldown_age < DOTTY_IDLE_VISION_COOLDOWN_SEC:
                if dance_active:
                    log.info(
                        "room_view skipped: device=%s reason=dance_active",
                        device_id,
                    )
                else:
                    log.info(
                        "room_view skipped: device=%s cooldown=%.1fs/%.0fs",
                        device_id, cooldown_age,
                        DOTTY_IDLE_VISION_COOLDOWN_SEC,
                    )
                description = _ROOM_VIEW_NO_PERSON
                _vision_cache[device_id] = {
                    "description": description,
                    "timestamp": perf_counter(),
                    "wall_ts": time.time(),
                    "jpeg_bytes": jpeg_bytes,
                    "question": question,
                    "room_match_person_id": None,
                    "source": "room_view",
                }
                for ev in _vision_events.get(device_id, ()):
                    ev.set()
                return {"description": description}
            state["last_room_view_capture_t"] = wall_now
            roster_ids = (
                _household_registry.roster_ids_with_appearance()
                if _household_registry is not None else set()
            )
            raw = await asyncio.to_thread(
                _call_vision_api, b64_image, room_view_question,
                system_prompt=VISION_ROOM_VIEW_SYSTEM_PROMPT,
            )
            parsed_desc, room_match_person_id, parsed_mood = (
                _parse_room_view_response(raw, roster_ids)
            )
            description = parsed_desc or _ROOM_VIEW_NO_PERSON
            if parsed_mood:
                # Plumb mood into perception state so the talk-turn
                # PerceptionSnapshot picks it up. Cleared on face_lost
                # by the existing event handler (last_face_id pop).
                pstate = _perception_state.setdefault(device_id, {})
                pstate["face_mood"] = parsed_mood
                pstate["face_mood_t"] = time.time()
            log.info(
                "room_view device=%s match=%s mood=%s desc=%s",
                device_id, room_match_person_id or "-",
                parsed_mood or "-", description[:120],
            )
        else:
            # v1 path — either a normal "what do you see" call, OR a
            # sentinel call that fell back because the registry is
            # empty (no roster to choose from).
            if question == _ROOM_VIEW_SENTINEL:
                question = (
                    "Describe the person you can see in one short "
                    "sentence — approximate age range, hair, clothing, "
                    "distinguishing features. If you cannot see a "
                    "person, reply with exactly: no one in view. "
                    "Do not guess names."
                )
            description = await asyncio.to_thread(
                _call_vision_api, b64_image, question,
            )

        _vision_cache[device_id] = {
            "description": description,
            "timestamp": perf_counter(),
            "wall_ts": time.time(),
            "jpeg_bytes": jpeg_bytes,
            "question": question,
            "room_match_person_id": room_match_person_id,
            "source": "room_view",
        }
        # Wake every waiter polling this device. Concurrent callers
        # (room-view capture from textMessageHandlerRegistry + voice
        # "what do you see" from receiveAudioHandle) both legitimately
        # poll vision_latest for the same device_id; the previous
        # single-event-per-device pattern lost the first waiter when
        # the second one overwrote the dict entry.
        for ev in _vision_events.get(device_id, ()):
            ev.set()

        # Layer 6 hook — when room-view resolves to a roster member,
        # broadcast a synthetic `face_recognized` event so perception-bus
        # consumers (notably ProactiveGreeter) see the resolved identity.
        # Without this the person_id stays trapped on the connection and
        # only reaches the next voice turn — never the bus.
        if room_match_person_id:
            _perception_broadcast({
                "name": "face_recognized",
                "device_id": device_id,
                "ts": time.time(),
                "data": {
                    "identity": room_match_person_id,
                    "source": "room_view",
                },
            })

        now = perf_counter()
        for k in [k for k, v in _vision_cache.items() if now - v["timestamp"] > VISION_CACHE_TTL_SEC]:
            _vision_cache.pop(k, None)

        log.info("vision result device=%s desc=%s", device_id, description[:120])
        return {"description": description}
    except Exception:
        err_kind = "exception"
        raise
    finally:
        if _METRICS_AVAILABLE:
            _safe_metric(
                dotty_request_duration_seconds.labels(
                    endpoint="vision_explain",
                ).observe,
                perf_counter() - t0,
            )
            if err_kind:
                _safe_metric(
                    dotty_request_errors_total.labels(
                        endpoint="vision_explain", kind=err_kind,
                    ).inc,
                )


@app.get("/api/vision/latest/{device_id}")
async def vision_latest(device_id: str):
    _vision_cache.pop(device_id, None)
    event = asyncio.Event()
    waiters = _vision_events.setdefault(device_id, [])
    waiters.append(event)
    try:
        await asyncio.wait_for(event.wait(), timeout=15.0)
        entry = _vision_cache.get(device_id)
        if entry:
            # `room_match_person_id` is None for the v1 description-only
            # path and either a roster id (string) or None for the v2
            # room_view roster path. Returned alongside `description` so
            # the caller can shuttle both into the [ROOM_VIEW] marker
            # (see `_with_room_view_marker` on the xiaozhi side).
            return {
                "description": entry["description"],
                "room_match_person_id": entry.get("room_match_person_id"),
            }
        return JSONResponse(status_code=500, content={"error": "vision processing failed"})
    except asyncio.TimeoutError:
        return JSONResponse(status_code=404, content={"error": "no vision result in time"})
    finally:
        try:
            waiters.remove(event)
        except ValueError:
            pass
        if not waiters:
            _vision_events.pop(device_id, None)


# ---------------------------------------------------------------------------
# Audio captioning — security-framed "what does Dotty hear" describer
# ---------------------------------------------------------------------------
# Mirrors the vision pipeline above. POST a short audio clip (wav/mp3/
# opus/flac, base64-encoded under the OpenAI-style `input_audio` content
# block) to an audio-capable model on OpenRouter, cache the textual
# caption, broadcast a synthetic perception event so the dashboard
# refreshes. Raw audio bytes are NOT held in the cache — only the text.
# Phase A: callable by curl now. Phase B (firmware capture relay) lands
# in a separate change.

# Best-effort guess of the OpenAI-compatible audio format string from a
# multipart UploadFile. Defaults to "wav" because that's what we'll send
# from the firmware once the capture path lands. OpenRouter routes
# "wav" / "mp3" / "opus" / "flac" through to the underlying model.
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


def _audio_format_from_upload(file: UploadFile) -> str:
    ct = (file.content_type or "").lower().strip()
    if ct in _AUDIO_FMT_BY_CONTENT_TYPE:
        return _AUDIO_FMT_BY_CONTENT_TYPE[ct]
    name = (file.filename or "").lower()
    for ext, fmt in (
        (".wav", "wav"), (".mp3", "mp3"), (".ogg", "opus"),
        (".opus", "opus"), (".flac", "flac"),
    ):
        if name.endswith(ext):
            return fmt
    return "wav"


def _call_audio_caption_api(
    b64_audio: str, fmt: str, question: str, *,
    system_prompt: str = AUDIO_CAPTION_SYSTEM_PROMPT,
) -> str:
    """Synchronous OpenRouter call. Wrapped via asyncio.to_thread by the
    endpoint so it doesn't block the event loop. Mirrors the shape of
    `_call_vision_api` so the failure modes look the same to operators."""
    import requests as req

    if not AUDIO_CAPTION_API_KEY:
        log.warning("AUDIO_CAPTION_API_KEY not set")
        return "I couldn't quite hear that clearly."
    payload = {
        "model": AUDIO_CAPTION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64_audio, "format": fmt},
                    },
                    {"type": "text", "text": question},
                ],
            },
        ],
        "max_tokens": 200,
        "temperature": 0.3,
    }
    try:
        resp = req.post(
            AUDIO_CAPTION_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {AUDIO_CAPTION_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=AUDIO_CAPTION_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("audio caption API call failed")
        return "I couldn't quite hear that clearly."


@app.post("/api/audio/explain")
async def audio_explain(
    request: Request,
    question: str = Form("Describe what you hear."),
    file: UploadFile = File(...),
):
    t0 = perf_counter()
    err_kind: str | None = None
    try:
        device_id = request.headers.get("device-id", "unknown")
        audio_bytes = await file.read()
        fmt = _audio_format_from_upload(file)
        log.info(
            "audio device=%s question=%s bytes=%d format=%s",
            device_id, question[:80], len(audio_bytes), fmt,
        )
        b64_audio = base64.b64encode(audio_bytes).decode("ascii")
        description = await asyncio.to_thread(
            _call_audio_caption_api, b64_audio, fmt, question,
        )
        _audio_cache[device_id] = {
            "description": description,
            "timestamp": perf_counter(),
            "wall_ts": time.time(),
            "question": question,
            "source": "audio_explain",
        }
        # Purge stale entries — same TTL-cleanup pattern as _vision_cache.
        now = perf_counter()
        for k in [
            k for k, v in _audio_cache.items()
            if now - v["timestamp"] > AUDIO_CACHE_TTL_SEC
        ]:
            _audio_cache.pop(k, None)

        # Nudge the dashboard via the perception SSE feed so the new
        # caption shows up without waiting for the next polling tick.
        _perception_broadcast({
            "name": "audio_captioned",
            "device_id": device_id,
            "ts": time.time(),
            "data": {
                "source": "audio_explain",
                "preview": description[:80],
            },
        })

        log.info("audio result device=%s desc=%s", device_id, description[:120])
        return {"description": description}
    except Exception:
        err_kind = "exception"
        raise
    finally:
        if _METRICS_AVAILABLE:
            _safe_metric(
                dotty_request_duration_seconds.labels(
                    endpoint="audio_explain",
                ).observe,
                perf_counter() - t0,
            )
            if err_kind:
                _safe_metric(
                    dotty_request_errors_total.labels(
                        endpoint="audio_explain", kind=err_kind,
                    ).inc,
                )


# ---------------------------------------------------------------------------
# Scene synthesis — periodic "what's happening right now" memory writes
# ---------------------------------------------------------------------------
# Composes _vision_cache + _audio_cache + perception_state into one
# sentence and appends to a daily NDJSON ring file. Phase C (ZeroClaw
# ingestion of the NDJSON into FTS memory) is tracked separately. The
# loop self-throttles to SCENE_SYNTHESIS_MIN_GAP_SEC so a burst of
# face_recognized / state_changed events doesn't spam the file.

_last_synthesis_ts: dict[str, float] = {}
_SCENE_SYNTHESIS_TRIGGER_EVENTS = {
    "face_recognized", "audio_captioned", "state_changed",
}


def _scene_synthesis_log_path() -> Path:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    return CONVO_LOG_DIR / f"scene-synthesis-{today}.ndjson"


def _idle_perception_log_path() -> Path:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    return CONVO_LOG_DIR / f"perception-{today}.ndjson"


def _write_idle_perception_record(device_id: str, description: str) -> None:
    """Append one idle-perception record to the daily NDJSON ring file.
    Same shape and mode (0600, no media) as scene-synthesis records,
    with `type=perception` and `mode=idle` so future ingestion can
    discriminate. ZeroClaw FTS ingestion of these files is a separate
    routine (see Phase C scene-synthesis tracker)."""
    record = {
        "ts": datetime.now(LOCAL_TZ).isoformat(),
        "device": device_id,
        "type": "perception",
        "mode": "idle",
        "text": description,
    }
    path = _idle_perception_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        log.warning("idle perception ndjson write failed", exc_info=True)


_IDLE_TOKEN_RE = re.compile(r"\w+")


def _is_notable_perception(
    description: str, last_description: str | None,
    *,
    jaccard_threshold: float = IDLE_PHOTOGRAPHER_NOTABLE_JACCARD,
) -> bool:
    """True when `description` is worth saving to memory.

    Cheap, FTS-friendly notability check (memory is FTS-only — no
    embeddings). Skips trivially short or "nothing changed" responses,
    then computes token-set Jaccard similarity against the last saved
    perception. Above the threshold = same scene = skip.
    """
    if not description or len(description) < 20:
        return False
    if "same as before" in description.lower():
        return False
    if not last_description:
        return True
    cur = set(t.lower() for t in _IDLE_TOKEN_RE.findall(description))
    prev = set(t.lower() for t in _IDLE_TOKEN_RE.findall(last_description))
    if not cur or not prev:
        return True
    union = cur | prev
    if not union:
        return True
    jaccard = len(cur & prev) / len(union)
    return jaccard < jaccard_threshold


def _idle_photographer_pick_device() -> str | None:
    """Single-device deployment helper — pick the device the
    photographer should target. Mirrors `_pick_perception_device_id`
    in dashboard.py; replicated here so the photographer doesn't
    pull a dashboard import. Priority: most recent _vision_cache
    entry, else first device in _perception_state."""
    if _vision_cache:
        try:
            return max(
                _vision_cache.items(),
                key=lambda kv: kv[1].get("wall_ts", 0.0),
            )[0]
        except Exception:
            pass
    for did in _perception_state.keys():
        if did and did != "unknown":
            return did
    return None


# ---------------------------------------------------------------------------
# Dream / dance NDJSON writers — see commit-5 plan for memory-tagging
# rationale. Records are FTS-friendly text only; full dream text lives
# alongside the summary so a future ZeroClaw routine can choose what
# to ingest.
# ---------------------------------------------------------------------------
def _dreams_log_path() -> Path:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    return CONVO_LOG_DIR / f"dreams-{today}.ndjson"


def _dances_log_path() -> Path:
    today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    return CONVO_LOG_DIR / f"dances-{today}.ndjson"


def _write_jsonl_record(path: Path, record: dict) -> None:
    """Shared NDJSON append helper. Mode 0600, ensure_ascii=False so
    Unicode in narrative text round-trips cleanly. Best-effort —
    swallows errors and warns rather than crashing the caller."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        log.warning("ndjson write failed: %s", path, exc_info=True)


def _write_dream_record(
    *,
    dream_id: str, seed: str, full_text: str, summary: str | None,
) -> None:
    record = {
        "ts": datetime.now(LOCAL_TZ).isoformat(),
        "type": "dream",
        "dream_id": dream_id,
        "seed": seed,
        "summary": summary or "",
        "full_text": full_text,
    }
    _write_jsonl_record(_dreams_log_path(), record)


def _write_dance_record(
    *,
    device_id: str, dance: str, reflection: str,
) -> None:
    record = {
        "ts": datetime.now(LOCAL_TZ).isoformat(),
        "type": "dance",
        "device": device_id,
        "dance": dance,
        "reflection": reflection,
    }
    _write_jsonl_record(_dances_log_path(), record)


def _split_dream_text(raw: str) -> tuple[str, str | None]:
    """Split the dream LLM reply into `(full_text, summary)`. Looks for
    a final line starting with `SUMMARY:` (case-insensitive). When
    absent, returns the raw text and None — the dream still lands in
    the daily NDJSON; downstream FTS just doesn't get the short-form
    summary for that record.
    """
    if not raw:
        return "", None
    text = raw.rstrip()
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if line.lower().startswith("summary:"):
            summary = line.split(":", 1)[1].strip()
            full_text = "\n".join(lines[:i]).rstrip()
            return full_text, (summary or None)
    return text, None


# Dream prompt — sent to NARRATIVE_MODEL with no kid-mode wrap. The
# `seed` slot is filled with one of DREAM_INSPIRATIONS.
DREAM_SYSTEM_PROMPT = (
    "You are Dotty, a small family robot, asleep. You are dreaming. "
    "Write rich, multi-paragraph robot dreams in first person, present "
    "tense — perception is strange, time bends, identity is fluid. "
    "Draw on the seed's atmosphere without retelling it. End with a "
    "half-thought, not a wrap. After the dream, on a new line starting "
    "with 'SUMMARY:', give a 1–2 sentence summary suitable for memory."
)
DREAM_USER_PROMPT_TEMPLATE = (
    "Tonight's seed: {seed}\n"
    "\n"
    "Write a dream of 4–7 paragraphs as Dotty would dream it.\n"
)

# Dance reflection prompt — short introspective monologue, NOT spoken.
DANCE_SYSTEM_PROMPT = (
    "You are Dotty, a small family robot, reflecting privately on a "
    "dance you just finished. Write 2–3 sentences in first person, "
    "present tense, internal monologue (not spoken). Capture the joy, "
    "the rhythm, the silliness. Keep it under 300 characters."
)
DANCE_USER_PROMPT_TEMPLATE = (
    "You just finished dancing to {dance}. How did it feel?"
)


async def _perception_sleep_dreamer() -> None:
    """Background task: when the device transitions to sleep, schedule
    DREAM_COUNT_PER_NIGHT dreams across DREAM_WINDOW_SECONDS at
    evenly-spaced fractions (defaults: 3 dreams at 25/50/75% of an
    8h window). Cancels pending dreams if the device leaves sleep
    early (head-pet wake, manual wake, talk).

    Dreams are independent: each one calls the narrative LLM with a
    random seed, parses out a SUMMARY line, and appends a record to
    the daily dreams NDJSON. Failures are isolated — one dream's
    network blip doesn't kill the schedule.
    """
    log.info(
        "perception sleep dreamer started "
        "(window=%.0fs count=%d model=%s)",
        DREAM_WINDOW_SECONDS, DREAM_COUNT_PER_NIGHT, NARRATIVE_MODEL,
    )
    q = _perception_subscribe()
    pending: dict[str, list[asyncio.Task]] = {}

    def _cancel_pending(device_id: str) -> None:
        tasks = pending.pop(device_id, [])
        for t in tasks:
            if not t.done():
                t.cancel()

    async def _fire_one(device_id: str, seed: str) -> None:
        dream_id = uuid.uuid4().hex
        user_prompt = DREAM_USER_PROMPT_TEMPLATE.format(seed=seed)
        log.info(
            "dream firing: device=%s seed=%s id=%s",
            device_id, seed, dream_id,
        )
        text = await asyncio.to_thread(
            _call_narrative_llm,
            user_prompt,
            system_prompt=DREAM_SYSTEM_PROMPT,
            max_tokens=1200,
        )
        if not text:
            log.warning("dream skipped (LLM returned no text): id=%s", dream_id)
            return
        full_text, summary = _split_dream_text(text)
        _write_dream_record(
            dream_id=dream_id, seed=seed,
            full_text=full_text, summary=summary,
        )
        log.info(
            "dream saved: id=%s seed=%s chars=%d summary=%s",
            dream_id, seed, len(full_text),
            (summary or "")[:80],
        )

    def _schedule(device_id: str) -> None:
        _cancel_pending(device_id)
        if DREAM_COUNT_PER_NIGHT <= 0:
            return
        tasks: list[asyncio.Task] = []
        # Even fractions: 1/(N+1), 2/(N+1), ..., N/(N+1) of the window.
        # For N=3, this gives 25/50/75% — the canonical schedule.
        for i in range(1, DREAM_COUNT_PER_NIGHT + 1):
            delay = DREAM_WINDOW_SECONDS * (i / (DREAM_COUNT_PER_NIGHT + 1))
            seed = random.choice(DREAM_INSPIRATIONS)

            async def _delayed_fire(d: float, did: str, s: str) -> None:
                await asyncio.sleep(d)
                try:
                    await _fire_one(did, s)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("dream fire crashed: device=%s seed=%s", did, s)

            tasks.append(
                asyncio.create_task(_delayed_fire(delay, device_id, seed))
            )
        pending[device_id] = tasks
        log.info(
            "dreams scheduled: device=%s count=%d delays=%s",
            device_id, len(tasks),
            [f"{DREAM_WINDOW_SECONDS * (i / (DREAM_COUNT_PER_NIGHT + 1)):.0f}s"
             for i in range(1, DREAM_COUNT_PER_NIGHT + 1)],
        )

    try:
        while True:
            event = await q.get()
            name = event.get("name") or ""
            if name != "state_changed":
                continue
            device_id = event.get("device_id") or ""
            if not device_id:
                continue
            new_state = (event.get("data", {}).get("state") or "").strip().lower()
            if new_state == "sleep":
                _schedule(device_id)
            else:
                # Any non-sleep state cancels pending dreams. The next
                # transition back to sleep schedules a fresh batch.
                if device_id in pending:
                    log.info(
                        "dreams cancelled: device=%s reason=state=%s",
                        device_id, new_state,
                    )
                    _cancel_pending(device_id)
    except asyncio.CancelledError:
        log.info("perception sleep dreamer cancelled")
        for did in list(pending.keys()):
            _cancel_pending(did)
        raise
    except Exception:
        log.exception("perception sleep dreamer crashed")
    finally:
        _perception_unsubscribe(q)


async def _perception_dance_reflector() -> None:
    """Background task: on `dance_ended` events, fire the narrative LLM
    for a short reflection on the dance and append to the daily dances
    NDJSON. Silent — no audio, no LED change, no state mutation. Each
    reflection is independent; failures are logged and skipped.
    """
    log.info(
        "perception dance reflector started (model=%s)", NARRATIVE_MODEL,
    )
    q = _perception_subscribe()
    try:
        while True:
            event = await q.get()
            if event.get("name") != "dance_ended":
                continue
            device_id = event.get("device_id") or ""
            if not device_id:
                continue
            data = event.get("data") or {}
            # `data.dance` may be empty if the firmware emitted a bare
            # dance_ended (older firmware); fall back to a generic name.
            dance = (data.get("dance") or "the dance").strip() or "the dance"
            user_prompt = DANCE_USER_PROMPT_TEMPLATE.format(dance=dance)

            async def _fire(did: str, dn: str, up: str) -> None:
                text = await asyncio.to_thread(
                    _call_narrative_llm,
                    up,
                    system_prompt=DANCE_SYSTEM_PROMPT,
                    max_tokens=200,
                    temperature=0.85,
                )
                if not text:
                    log.warning(
                        "dance reflection skipped (no LLM text): device=%s dance=%s",
                        did, dn,
                    )
                    return
                _write_dance_record(
                    device_id=did, dance=dn, reflection=text,
                )
                log.info(
                    "dance reflection saved: device=%s dance=%s chars=%d",
                    did, dn, len(text),
                )

            _spawn(
                _fire(device_id, dance, user_prompt),
                name="dance_reflector_fire",
            )
    except asyncio.CancelledError:
        log.info("perception dance reflector cancelled")
        raise
    except Exception:
        log.exception("perception dance reflector crashed")
    finally:
        _perception_unsubscribe(q)


async def _perception_idle_photographer() -> None:
    """Background task: while idle, fire `take_photo` every 3–5 min
    (jittered) and write notable descriptions to the idle-perception
    NDJSON. No servo motion, no LED change, no audio cue — silent
    capture. Skips when state != idle, when listening, when a face
    is present (room-view path handles those), and when the dispatch
    relay returns False (xiaozhi-server admin route missing).

    Skips on no-device-known and on dispatch failures rather than
    crashing — a missed cycle just means the next 3–5 min retries.
    """
    log.info(
        "perception idle photographer started "
        "(sleep=%.0f–%.0fs jaccard=%.2f wait=%.0fs)",
        IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC,
        IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC,
        IDLE_PHOTOGRAPHER_NOTABLE_JACCARD,
        IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC,
    )
    try:
        from bridge.security_watch import dispatch_take_photo
    except Exception:
        log.exception("idle photographer: dispatch_take_photo import failed; aborting")
        return
    try:
        while True:
            sleep_s = random.uniform(
                IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC,
                IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC,
            )
            await asyncio.sleep(sleep_s)
            device_id = _idle_photographer_pick_device()
            if not device_id:
                continue
            pstate = _perception_state.get(device_id, {}) or {}
            current_state = (pstate.get("current_state") or "idle").lower()
            if current_state != "idle":
                continue
            if pstate.get("listening"):
                continue
            if pstate.get("face_present"):
                continue
            pre_ts = (
                _vision_cache.get(device_id, {}).get("wall_ts") or 0.0
            )
            ok = await dispatch_take_photo(
                device_id, question=IDLE_WANDER_PROMPT,
            )
            if not ok:
                # Already logged once by dispatch_take_photo; don't
                # spam the journal on repeated 404s.
                continue
            await asyncio.sleep(IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC)
            entry = _vision_cache.get(device_id) or {}
            new_ts = entry.get("wall_ts") or 0.0
            description = (entry.get("description") or "").strip()
            if not description or new_ts <= pre_ts:
                log.info(
                    "idle photographer: device=%s no fresh description "
                    "(new_ts=%.0f pre_ts=%.0f)",
                    device_id, new_ts, pre_ts,
                )
                continue
            last_text = pstate.get("last_idle_perception_text")
            if not _is_notable_perception(description, last_text):
                log.info(
                    "idle photographer: device=%s skipped (not notable, "
                    "len=%d)", device_id, len(description),
                )
                continue
            pstate["last_idle_perception_text"] = description
            pstate["last_idle_perception_t"] = time.time()
            _perception_state[device_id] = pstate
            _write_idle_perception_record(device_id, description)
            log.info(
                "idle photographer: device=%s saved perception (%d chars)",
                device_id, len(description),
            )
    except asyncio.CancelledError:
        log.info("perception idle photographer cancelled")
        raise
    except Exception:
        log.exception("perception idle photographer crashed")


def _compose_scene_synthesis(device_id: str) -> dict | None:
    """Build the synthesis record dict for `device_id`, or None if there's
    nothing fresh enough to be worth writing.

    Pulls only from in-memory caches — _vision_cache / _audio_cache /
    _perception_state. Caller decides whether to actually emit (cadence,
    min-gap guard)."""
    now_wall = time.time()
    now_perf = perf_counter()
    pstate = _perception_state.get(device_id) or {}

    vision_entry = _vision_cache.get(device_id) or {}
    has_vision = bool(
        vision_entry
        and now_perf - vision_entry.get("timestamp", 0.0)
        <= VISION_CACHE_TTL_SEC
    )
    vision_desc = (
        (vision_entry.get("description") or "").strip() if has_vision else ""
    )

    audio_entry = _audio_cache.get(device_id) or {}
    has_audio = bool(
        audio_entry
        and now_perf - audio_entry.get("timestamp", 0.0)
        <= AUDIO_CACHE_TTL_SEC
    )
    audio_desc = (
        (audio_entry.get("description") or "").strip() if has_audio else ""
    )

    # Don't emit a record that has literally nothing in it. Face presence
    # alone is too thin a signal to clutter the log.
    if not has_vision and not has_audio:
        return None

    face_id = (pstate.get("last_face_id") or "").strip() or None
    face_present = bool(pstate.get("face_present"))
    if face_id and face_id != "unknown":
        face_phrase = f"{face_id} is in the room."
    elif face_present:
        face_phrase = "Someone is in the room."
    else:
        face_phrase = "No one is detected."

    state = pstate.get("current_state") or "idle"

    parts: list[str] = []
    ts_label = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    parts.append(f"{ts_label} — {face_phrase}")
    if vision_desc:
        parts.append(f"Dotty sees {vision_desc.rstrip('.')}.")
    if audio_desc:
        parts.append(f"Heard: {audio_desc.rstrip('.')}.")
    parts.append(f"State: {state}.")
    text = " ".join(parts)

    return {
        "ts": datetime.now(LOCAL_TZ).isoformat(),
        "ts_wall": now_wall,
        "type": "scene_synthesis",
        "device": device_id,
        "text": text,
        "face_id": face_id,
        "state": state,
        "has_vision": has_vision,
        "has_audio_caption": has_audio,
    }


def _write_scene_synthesis_ndjson(record: dict) -> None:
    """Append one record to the daily NDJSON ring file. Mirrors the
    write_security_record pattern (mode 0600, JSON line, no media)."""
    path = _scene_synthesis_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Strip the wall ts before serialising — it's only used in-process
        # for cache TTL bookkeeping. The isoformat `ts` is the canonical
        # timestamp on disk.
        on_disk = {k: v for k, v in record.items() if k != "ts_wall"}
        line = json.dumps(on_disk, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort; first write sets the mode
    except Exception:
        log.warning("scene synthesis ndjson write failed", exc_info=True)


def _maybe_emit_scene_synthesis(device_id: str, *, reason: str) -> None:
    """Compose, throttle-check, cache, and write one synthesis record."""
    record = _compose_scene_synthesis(device_id)
    if record is None:
        return
    now_wall = record["ts_wall"]
    last = _last_synthesis_ts.get(device_id, 0.0)
    if now_wall - last < SCENE_SYNTHESIS_MIN_GAP_SEC:
        return
    _last_synthesis_ts[device_id] = now_wall
    _scene_synthesis_cache[device_id] = {
        "text": record["text"],
        "ts_wall": now_wall,
        "face_id": record["face_id"],
        "state": record["state"],
    }
    _write_scene_synthesis_ndjson(record)
    log.info(
        "scene_synthesis device=%s reason=%s text=%s",
        device_id, reason, record["text"][:160],
    )
    # Broadcast a synthetic event so the dashboard's SSE listener can
    # nudge the Perception card to redraw immediately instead of
    # waiting for the next 10 s polling tick. Same `dotty-refresh`
    # plumbing the vision/face events use.
    try:
        _perception_broadcast({
            "name": "scene_synthesised",
            "device_id": device_id,
            "ts": now_wall,
            "data": {
                "reason": reason,
                "preview": record["text"][:80],
            },
        })
    except Exception:
        log.warning("scene synthesis broadcast failed", exc_info=True)


async def _scene_synthesis_loop() -> None:
    """Single task that drives both the time-based ticker and the
    perception-event triggers. Subscribes to the perception bus and
    races queue.get() against an interval timeout — whichever fires
    first decides the next emit attempt. Failures never propagate."""
    queue = _perception_subscribe()
    log.info(
        "perception scene-synthesis loop started "
        "(interval=%.0fs min_gap=%.0fs)",
        SCENE_SYNTHESIS_INTERVAL_SEC, SCENE_SYNTHESIS_MIN_GAP_SEC,
    )
    try:
        while True:
            reason = "tick"
            try:
                ev = await asyncio.wait_for(
                    queue.get(), timeout=SCENE_SYNTHESIS_INTERVAL_SEC,
                )
            except asyncio.TimeoutError:
                ev = None
            if ev is not None:
                name = ev.get("name") or ""
                if name not in _SCENE_SYNTHESIS_TRIGGER_EVENTS:
                    continue
                if name == "state_changed":
                    new_state = (ev.get("data") or {}).get("state")
                    if new_state not in SCENE_SYNTHESIS_TRIGGER_STATES:
                        continue
                reason = name
                device_ids = [ev.get("device_id")] if ev.get("device_id") else []
            else:
                device_ids = list(_perception_state.keys())
            for did in device_ids:
                if not did:
                    continue
                try:
                    _maybe_emit_scene_synthesis(did, reason=reason)
                except Exception:
                    log.warning(
                        "scene synthesis emit failed device=%s", did,
                        exc_info=True,
                    )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("scene synthesis loop crashed — exiting cleanly")
    finally:
        _perception_unsubscribe(queue)


@app.post("/api/message", response_model=MessageOut)
async def message(payload: MessageIn) -> MessageOut:
    session_id = payload.session_id or str(uuid.uuid4())
    log.info("msg channel=%s session=%s len=%d",
             payload.channel, session_id, len(payload.content))
    await _refresh_caches()
    speaker = _resolve_speaker_for_request(payload)
    if speaker is not None and speaker.person_id:
        log.info(
            "speaker channel=%s person=%s addressee=%s conf=%.2f signals=%s",
            payload.channel, speaker.person_id, speaker.addressee,
            speaker.confidence,
            ",".join(v.signal for v in speaker.votes) or "-",
        )
    t0 = perf_counter()
    error_msg = None
    try:
        raw = await asyncio.wait_for(
            acp.prompt(
                payload.content,
                xiaozhi_sid=payload.session_id,
                prepare=_voice_preparer(
                    payload.channel, speaker,
                    room_description=(payload.metadata or {}).get(
                        "room_description"),
                    device_id=(payload.metadata or {}).get("device_id"),
                ),
            ),
            timeout=REQUEST_TIMEOUT_SEC,
        )
        raw = _clean_for_tts(_ensure_emoji_prefix(_content_filter(raw) or raw))
        raw = _strip_extra_emojis(raw)
        answer = _truncate_sentences(raw)
    except asyncio.TimeoutError:
        log.warning("ACP timeout")
        answer = f"{FALLBACK_EMOJI} I'm thinking too slowly right now, try again."
        error_msg = "timeout"
    except FileNotFoundError:
        log.exception("zeroclaw binary missing")
        answer = f"{FALLBACK_EMOJI} My AI brain is offline."
        error_msg = "binary_missing"
    except Exception:
        log.exception("ACP invocation failed")
        answer = f"{FALLBACK_EMOJI} Something went wrong, please try again."
        error_msg = "exception"
    elapsed_s = perf_counter() - t0
    if _METRICS_AVAILABLE:
        _safe_metric(
            dotty_request_duration_seconds.labels(endpoint="message").observe,
            elapsed_s,
        )
        if error_msg:
            _safe_metric(
                dotty_request_errors_total.labels(
                    endpoint="message", kind=error_msg,
                ).inc,
            )
        else:
            # Non-streaming first-audio = full response latency from the
            # bridge's POV (xiaozhi-server pipelines TTS once it gets the
            # full reply). Streaming endpoint records a tighter value at
            # first chunk emit.
            _safe_metric(record_first_audio, elapsed_s)
    _convo_log.log_turn(
        channel=payload.channel or "",
        session_id=session_id,
        request_text=payload.content,
        response_text=answer,
        latency_ms=elapsed_s * 1000.0,
        error=error_msg,
        latency_phases=dict(acp._last_phases) if acp._last_phases else None,
    )
    return MessageOut(response=answer, session_id=session_id)


if _configure_dashboard is not None:
    async def _dashboard_send_message(*, text: str, channel: str = "dotty") -> dict:
        out = await message(MessageIn(content=text, channel=channel))
        return {"response": out.response, "session_id": out.session_id}

    async def _dashboard_set_kid_mode(enabled: bool) -> dict:
        # kid_mode is guardrails only (content sandwich, denied tools, persona)
        # — it does NOT pick the model. smart_mode owns model selection.
        # Hot-reloaded in-process via `_apply_kid_mode`, so no daemon restart
        # is needed; the LED pip flip is pushed live by `_dispatch_set_toggle`.
        _write_kid_mode(enabled)
        _apply_kid_mode(enabled)
        ok = await _dispatch_set_toggle("", "kid_mode", enabled)
        return {"ok": ok}

    async def _dashboard_abort_device(*, device_id: str = "") -> dict:
        """Fire-and-forget POST to xiaozhi-server's admin abort route."""
        if not _XIAOZHI_HOST:
            return {"ok": False, "error": "XIAOZHI_HOST not set"}

        url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/abort"
        payload: dict = {}
        if device_id:
            payload["device_id"] = device_id
        def _post() -> dict:
            try:
                r = requests.post(url, json=payload, timeout=3)
                if r.status_code == 200:
                    return {"ok": True, **r.json()}
                if r.status_code == 503 and "no device connected" in r.text:
                    return {"ok": False, "error": "Dotty isn't connected right now — try again in a few seconds."}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return await asyncio.to_thread(_post)

    async def _dashboard_inject_to_device(*, text: str, device_id: str = "") -> dict:
        """Fire-and-forget POST to xiaozhi-server's admin route so the
        named (or first-available) device runs the text through its
        normal post-ASR pipeline — intent detection, MCP tools, TTS."""
        if not _XIAOZHI_HOST:
            return {"ok": False, "error": "XIAOZHI_HOST not set"}

        url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/inject-text"
        payload = {"text": text}
        if device_id:
            payload["device_id"] = device_id
        def _post() -> dict:
            try:
                r = requests.post(url, json=payload, timeout=3)
                if r.status_code == 200:
                    return {"ok": True, **r.json()}
                if r.status_code == 503 and "no device connected" in r.text:
                    return {"ok": False, "error": "Dotty isn't connected right now — try again in a few seconds."}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return await asyncio.to_thread(_post)

    def _dashboard_state_getter() -> str:
        """Return the current State of the (first connected) device, or 'idle'.
        Falls back to 'idle' before any state_changed event has been seen."""
        for st in _perception_state.values():
            s = st.get("current_state")
            if s:
                return s
        return "idle"

    def _dashboard_perception_state_getter() -> dict:
        """Snapshot of per-device perception state with sensor_stale +
        sensor_age_s annotations — same shape as /api/perception/state but
        called in-process so the dashboard avoids an HTTP round-trip on
        every status-strip refresh. Stale threshold is the bridge's
        _PERCEPTION_STALE_THRESHOLD_S; sensors going quiet past that
        window flips the Dotty header dot to amber even when voice is
        otherwise live."""
        now = time.time()
        out: dict[str, dict] = {}
        for did, raw in _perception_state.items():
            entry = dict(raw)
            last_t = entry.get("last_event_t")
            age = float("inf") if last_t is None else max(0.0, now - last_t)
            entry["sensor_age_s"] = age
            entry["sensor_stale"] = age > _PERCEPTION_STALE_THRESHOLD_S
            out[did] = entry
        return out

    async def _dashboard_set_state(state: str) -> dict:
        ok = await _dispatch_set_state("", state)
        return {"ok": ok}

    async def _dashboard_set_smart_mode(enabled: bool) -> dict:
        # smart_mode owns voice-daemon model selection. ON → SMART_MODEL,
        # OFF → DEFAULT_MODEL. Rewrites config.toml + schedules daemon
        # restart so the new model is live for the next turn.
        _write_smart_mode(enabled)
        ok = await _dispatch_set_toggle("", "smart_mode", enabled)
        target_model = SMART_MODEL if enabled else DEFAULT_MODEL
        try:
            _apply_model_swap("voice", target_model)
            _admin_schedule_restart(_ADMIN_DAEMON_CFG["voice"][1])
        except Exception:
            logging.getLogger("zeroclaw-bridge").exception(
                "smart_mode flip succeeded but model swap/restart to %r failed",
                target_model,
            )
        return {"ok": ok}

    def _identity_display_name(identity: str) -> str | None:
        """Resolve a household person_id to its display_name, or None if the
        registry isn't loaded / the id isn't in the roster. Used by the
        dashboard's Perception card to label an identified face."""
        if not identity or _household_registry is None:
            return None
        try:
            person = _household_registry.get(identity)
        except Exception:
            return None
        if person is None:
            return None
        return getattr(person, "display_name", None) or None

    _configure_dashboard(
        send_message=_dashboard_send_message,
        vision_cache=_vision_cache,
        audio_cache=_audio_cache,
        scene_synthesis_cache=_scene_synthesis_cache,
        kid_mode_getter=_read_kid_mode,
        kid_mode_setter=_dashboard_set_kid_mode,
        smart_mode_getter=_read_smart_mode,
        smart_mode_setter=_dashboard_set_smart_mode,
        state_getter=_dashboard_state_getter,
        state_setter=_dashboard_set_state,
        inject_to_device=_dashboard_inject_to_device,
        abort_device=_dashboard_abort_device,
        subscribe_events=_dashboard_subscribe_events,
        unsubscribe_events=_dashboard_unsubscribe_events,
        perception_state_getter=_dashboard_perception_state_getter,
        perception_recent_getter=get_recent_perception,
        identity_display_name=_identity_display_name,
    )


# ---------------------------------------------------------------------------
# /admin/* — runtime configuration mutations. Localhost-only so only same-host
# callers can hit them. Useful when an external agent (e.g. a separate ZeroClaw
# daemon or operator script) needs to flip kid-mode, swap models, edit a
# persona file, or amend the MCP tool allowlist without an SSH session.
#
# Paths and systemd unit names are env-configurable (defaults match the
# documented ZeroClaw host layout):
#   ZEROCLAW_VOICE_CFG       - voice daemon config.toml
#   ZEROCLAW_VOICE_UNIT      - voice daemon's systemd unit (the bridge)
#   ZEROCLAW_DISCORD_CFG     - optional secondary daemon config.toml
#   ZEROCLAW_DISCORD_UNIT    - optional secondary daemon's systemd unit
#   ZEROCLAW_WORKSPACE       - workspace dir holding SOUL.md / IDENTITY.md / ...
# ---------------------------------------------------------------------------
from fastapi import APIRouter, Depends, HTTPException

_ADMIN_ALLOWED_PERSONA_FILES = {
    "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md",
    "TOOLS.md", "BOOTSTRAP.md", "HEARTBEAT.md", "MEMORY.md",
}
_ADMIN_WORKSPACE_DIR = Path(
    os.environ.get("ZEROCLAW_WORKSPACE", "/root/.zeroclaw/workspace")
)
_ADMIN_DAEMON_CFG = {
    "voice": (
        os.environ.get("ZEROCLAW_VOICE_CFG", "/root/.zeroclaw/config.toml"),
        os.environ.get("ZEROCLAW_VOICE_UNIT", "zeroclaw-bridge"),
    ),
    "discord": (
        os.environ.get("ZEROCLAW_DISCORD_CFG", "/root/.zeroclaw-discord/config.toml"),
        os.environ.get("ZEROCLAW_DISCORD_UNIT", "zeroclaw-discord"),
    ),
}


def _admin_require_localhost(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="admin endpoints are localhost-only")


def _admin_schedule_restart(unit: str, delay: float = 2.0) -> None:
    """Spawn detached `sleep N && systemctl restart UNIT` so the HTTP
    response can flush before the bridge SIGTERMs itself. start_new_session
    detaches the child from the parent's process group so it survives the
    SIGTERM cascade."""
    import subprocess
    subprocess.Popen(
        ["bash", "-c", f"sleep {delay} && systemctl restart {unit}"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class _AdminKidModeIn(BaseModel):
    enabled: bool
    device_id: str = ""


class _AdminSmartModeIn(BaseModel):
    enabled: bool
    device_id: str = ""


class _AdminStateIn(BaseModel):
    state: str
    device_id: str = ""


class _AdminPersonaIn(BaseModel):
    file: str
    content: str


class _AdminModelIn(BaseModel):
    daemon: str
    model: str


class _AdminSafetyIn(BaseModel):
    action: str
    tool: str


_admin_router = APIRouter(
    prefix="/admin", dependencies=[Depends(_admin_require_localhost)],
)


@_admin_router.post("/kid-mode")
async def _admin_kid_mode(payload: _AdminKidModeIn) -> dict:
    # kid_mode is guardrails only (content sandwich, denied tools, persona)
    # — model selection lives on smart_mode. Hot-reloaded in-process via
    # `_apply_kid_mode` so no daemon restart is needed; the firmware pip
    # update is pushed live by `_dispatch_set_toggle`.
    _write_kid_mode(payload.enabled)
    _apply_kid_mode(payload.enabled)
    pushed = await _dispatch_set_toggle(
        payload.device_id, "kid_mode", payload.enabled,
    )
    return {
        "ok": True, "enabled": payload.enabled,
        "device_pushed": pushed,
        "hot_applied": True,
    }


@_admin_router.post("/smart-mode")
async def _admin_smart_mode(payload: _AdminSmartModeIn) -> dict:
    """Flip smart_mode toggle. Owns voice-daemon model selection: ON →
    SMART_MODEL (claude-sonnet-4-6), OFF → DEFAULT_MODEL (mistral). Rewrites
    the daemon's config.toml and schedules a systemctl restart so the new
    model is live on the next turn. Pip update is pushed via the
    /xiaozhi/admin/set-toggle MCP relay so it lands live."""
    _write_smart_mode(payload.enabled)
    pushed = await _dispatch_set_toggle(
        payload.device_id, "smart_mode", payload.enabled,
    )
    target_model = SMART_MODEL if payload.enabled else DEFAULT_MODEL
    cfg_path, unit = _apply_model_swap("voice", target_model)
    _admin_schedule_restart(unit)
    return {
        "ok": True, "enabled": payload.enabled, "device_pushed": pushed,
        "model": target_model, "config": cfg_path, "restart": unit,
    }


@_admin_router.post("/state")
async def _admin_state(payload: _AdminStateIn) -> dict:
    """Phase 4 — dashboard / external trigger to set Dotty's high-level state.
    Valid: idle / talk / story_time / security / sleep / dance. Pushes
    self.robot.set_state MCP via the xiaozhi-server relay; the firmware
    StateManager handles the transition. No daemon restart."""
    valid = ("idle", "talk", "story_time", "security", "sleep", "dance")
    if payload.state not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"state must be one of {valid}",
        )
    pushed = await _dispatch_set_state(payload.device_id, payload.state)
    return {"ok": True, "state": payload.state, "device_pushed": pushed}


@_admin_router.post("/persona")
async def _admin_persona(payload: _AdminPersonaIn) -> dict:
    if payload.file not in _ADMIN_ALLOWED_PERSONA_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"file must be one of {sorted(_ADMIN_ALLOWED_PERSONA_FILES)}",
        )
    target = _ADMIN_WORKSPACE_DIR / payload.file
    real = target.resolve() if target.is_symlink() or target.exists() else target
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_suffix(real.suffix + ".new")
    tmp.write_text(payload.content)
    tmp.replace(real)
    return {
        "ok": True, "file": str(target), "resolved": str(real),
        "bytes": len(payload.content),
    }


def _read_voice_model_from_cfg() -> str | None:
    """Return the current `model = "..."` value from the voice daemon's
    config.toml, or None if the file/section is missing."""
    cfg_path, _ = _ADMIN_DAEMON_CFG["voice"]
    cfg_p = Path(cfg_path)
    if not cfg_p.exists():
        return None
    try:
        src = cfg_p.read_text()
    except OSError:
        return None
    section_re = re.compile(r'\[providers\.models\."custom:[^"]+"\]')
    m = section_re.search(src)
    if not m:
        return None
    sec_start = m.start()
    sec_end = src.find("\n[", sec_start + 1)
    if sec_end == -1:
        sec_end = len(src)
    section = src[sec_start:sec_end]
    cur = re.search(r'^model = "([^"]+)"$', section, flags=re.MULTILINE)
    return cur.group(1) if cur else None


def _reconcile_voice_model_at_startup() -> None:
    """Bridge owns the source of truth for which model the voice daemon runs
    on (smart_mode state file → DEFAULT_MODEL or SMART_MODEL). On startup,
    if config.toml drifts from that, rewrite + schedule a daemon restart so
    the next turn lands on the right model. No-op when already in sync."""
    target = SMART_MODEL if _read_smart_mode() else DEFAULT_MODEL
    current = _read_voice_model_from_cfg()
    if current is None:
        log.warning(
            "startup model reconcile: voice config.toml unreadable — skipping",
        )
        return
    if current == target:
        log.info(
            "startup model reconcile: voice daemon already on %r (smart_mode=%s)",
            target, _read_smart_mode(),
        )
        return
    try:
        _, unit = _apply_model_swap("voice", target)
        _admin_schedule_restart(unit)
        log.info(
            "startup model reconcile: voice daemon %r -> %r (smart_mode=%s, restart scheduled)",
            current, target, _read_smart_mode(),
        )
    except Exception:
        log.exception("startup model reconcile failed")


def _apply_model_swap(daemon: str, model: str) -> tuple[str, str]:
    """Rewrite the `model = "..."` line inside the custom-provider section
    of the named daemon's config.toml. Returns (cfg_path, unit). Caller is
    responsible for scheduling the systemctl restart so this can be reused
    from flows that already schedule a restart of their own (e.g. the kid
    mode toggle)."""
    if daemon not in _ADMIN_DAEMON_CFG:
        raise HTTPException(
            status_code=400,
            detail=f"daemon must be one of {sorted(_ADMIN_DAEMON_CFG)}",
        )
    if not re.fullmatch(r"[A-Za-z0-9./:_-]+", model):
        raise HTTPException(status_code=400, detail="model id has invalid chars")
    cfg_path, unit = _ADMIN_DAEMON_CFG[daemon]
    cfg_p = Path(cfg_path)
    if not cfg_p.exists():
        raise HTTPException(status_code=404, detail=f"config not found: {cfg_path}")
    src = cfg_p.read_text()
    section_re = re.compile(r'\[providers\.models\."custom:[^"]+"\]')
    m = section_re.search(src)
    if not m:
        raise HTTPException(status_code=500, detail="provider section not found")
    sec_start = m.start()
    sec_end = src.find("\n[", sec_start + 1)
    if sec_end == -1:
        sec_end = len(src)
    section = src[sec_start:sec_end]
    new_section, n = re.subn(
        r'^model = ".*"$',
        f'model = "{model}"',
        section, count=1, flags=re.MULTILINE,
    )
    if n == 0:
        raise HTTPException(status_code=500, detail="model line not found")
    cfg_p.write_text(src[:sec_start] + new_section + src[sec_end:])
    return (cfg_path, unit)


@_admin_router.post("/model")
async def _admin_model(payload: _AdminModelIn) -> dict:
    cfg_path, unit = _apply_model_swap(payload.daemon, payload.model)
    _admin_schedule_restart(unit)
    return {
        "ok": True, "daemon": payload.daemon, "model": payload.model,
        "config": cfg_path, "restart": unit,
    }


@_admin_router.post("/safety")
async def _admin_safety(payload: _AdminSafetyIn) -> dict:
    if payload.action not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    if not re.fullmatch(r"[A-Za-z0-9._]+", payload.tool):
        raise HTTPException(status_code=400, detail="tool name has invalid chars")
    self_path = Path(__file__)
    src = self_path.read_text()
    start_marker = "# === ADMIN_ALLOWLIST_START ==="
    end_marker = "# === ADMIN_ALLOWLIST_END ==="
    if start_marker not in src or end_marker not in src:
        raise HTTPException(status_code=500, detail="allowlist markers missing")
    pre, rest = src.split(start_marker, 1)
    block, post = rest.split(end_marker, 1)
    set_re = re.compile(
        r'MCP_TOOL_ALLOWLIST:\s*set\[str\]\s*=\s*\{([^}]*)\}',
        re.DOTALL,
    )
    m_set = set_re.search(block)
    if not m_set:
        raise HTTPException(status_code=500, detail="allowlist set literal not found")
    items = set(re.findall(r'"([^"]+)"', m_set.group(1)))
    before_size = len(items)
    if payload.action == "add":
        items.add(payload.tool)
    else:
        items.discard(payload.tool)
    new_items = sorted(items)
    new_inner = "\n    " + ",\n    ".join(f'"{t}"' for t in new_items) + ",\n"
    new_block = block[: m_set.start(1)] + new_inner + block[m_set.end(1):]
    new_src = pre + start_marker + new_block + end_marker + post
    new_path = self_path.with_suffix(".py.new")
    new_path.write_text(new_src)
    import py_compile
    try:
        py_compile.compile(str(new_path), doraise=True)
    except py_compile.PyCompileError as exc:
        new_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"py_compile failed: {exc}")
    new_path.replace(self_path)
    _admin_schedule_restart(_ADMIN_DAEMON_CFG["voice"][1])
    return {
        "ok": True, "action": payload.action, "tool": payload.tool,
        "size_before": before_size, "size_after": len(new_items),
        "restart": _ADMIN_DAEMON_CFG["voice"][1],
    }


app.include_router(_admin_router)


@app.post("/api/message/stream")
async def message_stream(payload: MessageIn) -> StreamingResponse:
    """NDJSON-streaming variant of /api/message.

    Emits one JSON line per token-level chunk as the LLM produces it:
        {"type":"chunk","content":"..."}
    Ends with a single final line (after the LLM turn completes):
        {"type":"final","content":"<full text>","session_id":"..."}
    or on error:
        {"type":"error","message":"...","session_id":"..."}

    The first non-whitespace character across all emitted chunks is checked
    against ALLOWED_EMOJIS; if the LLM forgot its emoji leader, FALLBACK_EMOJI
    is prepended to the first chunk before it goes out. This keeps the face
    animation protocol intact without waiting for the full response.
    """
    session_id = payload.session_id or str(uuid.uuid4())
    log.info(
        "stream channel=%s session=%s len=%d",
        payload.channel, session_id, len(payload.content),
    )
    await _refresh_caches()
    speaker = _resolve_speaker_for_request(payload)
    if speaker is not None and speaker.person_id:
        log.info(
            "speaker channel=%s person=%s addressee=%s conf=%.2f signals=%s",
            payload.channel, speaker.person_id, speaker.addressee,
            speaker.confidence,
            ",".join(v.signal for v in speaker.votes) or "-",
        )

    # `t_request_start` is captured per-request and read inside on_chunk
    # so the first-audio histogram observes the elapsed time at the
    # exact point the bridge emits its first content chunk to the client.
    t_request_start = perf_counter()
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    state = {
        "seen_nonws": False, "blocked": False,
        "sentence_ends": 0, "truncated": False,
        "first_audio_recorded": False,
    }

    async def on_chunk(content: str) -> None:
        content = _clean_for_tts(content)
        if not content:
            return
        if state["blocked"] or state["truncated"]:
            return
        replacement = _content_filter(content)
        if replacement:
            log.warning("content-filter-hit-stream chunk_len=%d", len(content))
            state["blocked"] = True
            state["seen_nonws"] = True
            await queue.put(("chunk", replacement))
            return
        if not state["seen_nonws"]:
            stripped = content.lstrip()
            if stripped:
                state["seen_nonws"] = True
                if not any(stripped.startswith(e) for e in ALLOWED_EMOJIS):
                    content = f"{FALLBACK_EMOJI} " + content
                # First chunk that carries non-whitespace content == the
                # first-audio milestone from the bridge's perspective.
                # xiaozhi-server pipelines TTS synthesis off of this, so
                # (bridge_first_chunk + tts_synth_first) ~= true audible
                # latency on-device. We capture the bridge half here.
                if _METRICS_AVAILABLE and not state["first_audio_recorded"]:
                    state["first_audio_recorded"] = True
                    _safe_metric(
                        record_first_audio,
                        perf_counter() - t_request_start,
                    )
        out = []
        for ch in content:
            out.append(ch)
            if ch in '.!?':
                state["sentence_ends"] += 1
                if state["sentence_ends"] >= MAX_SENTENCES:
                    state["truncated"] = True
                    break
        content = ''.join(out)
        if content:
            await queue.put(("chunk", content))

    async def run_turn() -> None:
        t0 = perf_counter()
        error_msg = None
        full = ""
        try:
            full = await asyncio.wait_for(
                acp.prompt(
                    payload.content,
                    xiaozhi_sid=payload.session_id,
                    chunk_cb=on_chunk,
                    prepare=_voice_preparer(
                        payload.channel, speaker,
                        room_description=(payload.metadata or {}).get(
                            "room_description"),
                        device_id=(payload.metadata or {}).get("device_id"),
                    ),
                ),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            full = _clean_for_tts(full)
            if not state["blocked"]:
                final_hit = _content_filter(full)
                if final_hit is not None:
                    full = final_hit
                    state["blocked"] = True
            if state["blocked"]:
                full = _CONTENT_FILTER_REPLACEMENT
            full = _ensure_emoji_prefix(full)
            full = _strip_extra_emojis(full)
            full = _truncate_sentences(full)
            if not state["seen_nonws"]:
                await queue.put(("chunk", full))
            await queue.put(("final", full))
        except asyncio.TimeoutError:
            log.warning("ACP timeout (stream)")
            error_msg = "timeout"
            await queue.put(("error", f"{FALLBACK_EMOJI} I'm thinking too slowly right now, try again."))
        except FileNotFoundError:
            log.exception("zeroclaw binary missing (stream)")
            error_msg = "binary_missing"
            await queue.put(("error", f"{FALLBACK_EMOJI} My AI brain is offline."))
        except Exception:
            log.exception("ACP invocation failed (stream)")
            error_msg = "exception"
            await queue.put(("error", f"{FALLBACK_EMOJI} Something went wrong, please try again."))
        elapsed_s = perf_counter() - t0
        if _METRICS_AVAILABLE:
            _safe_metric(
                dotty_request_duration_seconds.labels(
                    endpoint="message_stream",
                ).observe,
                elapsed_s,
            )
            if error_msg:
                _safe_metric(
                    dotty_request_errors_total.labels(
                        endpoint="message_stream", kind=error_msg,
                    ).inc,
                )
        _convo_log.log_turn(
            channel=payload.channel or "",
            session_id=session_id,
            request_text=payload.content,
            response_text=full,
            latency_ms=elapsed_s * 1000.0,
            error=error_msg,
            latency_phases=dict(acp._last_phases) if acp._last_phases else None,
        )

    async def gen():
        task = asyncio.create_task(run_turn())
        try:
            while True:
                kind, data = await queue.get()
                if kind == "chunk":
                    yield json.dumps({"type": "chunk", "content": data}, ensure_ascii=False) + "\n"
                elif kind == "final":
                    yield json.dumps(
                        {"type": "final", "content": data, "session_id": session_id},
                        ensure_ascii=False,
                    ) + "\n"
                    break
                elif kind == "error":
                    yield json.dumps(
                        {"type": "error", "message": data, "session_id": session_id},
                        ensure_ascii=False,
                    ) + "\n"
                    break
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(gen(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
