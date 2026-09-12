"""Environment-driven configuration.

Mirrors the env-var surface bridge.py reads, minus the variables tied
to obsolete code paths (ZEROCLAW_BIN, VOICE_LOCAL_PROFILE_KEY, the
smart-mode model-swap inputs, etc.). Loaded once at import time; tests
that need to override should set os.environ before importing
dotty_behaviour.config or use the helpers in tests/.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# Version stamp surfaced on /health and in startup logs.
VERSION: str = "0.1.0"

# Local timezone — used for NDJSON daily filenames and timestamps.
# Falls back to UTC if the TZ env var names a zone Python can't find.
try:
    LOCAL_TZ: ZoneInfo = ZoneInfo(os.environ.get("TZ", "Australia/Brisbane"))
except Exception:
    LOCAL_TZ = ZoneInfo("UTC")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# HTTP server
HOST: str = os.environ.get("DOTTY_BEHAVIOUR_HOST", "0.0.0.0")
PORT: int = _env_int("DOTTY_BEHAVIOUR_PORT", 8090)

# Outbound: xiaozhi-server admin endpoints (same-host loopback under
# Unraid's network_mode: host). Empty XIAOZHI_HOST disables dispatch
# the same way bridge.py does today.
XIAOZHI_HOST: str = os.environ.get("XIAOZHI_HOST", "")
XIAOZHI_HTTP_PORT: int = _env_int("XIAOZHI_OTA_PORT", 8003)

# Outbound: llama-swap for narrative LLM (dreams, dance reflections,
# scene synthesis). Mirrors bridge.py's NARRATIVE_LLM_URL.
NARRATIVE_LLM_URL: str = os.environ.get(
    "NARRATIVE_LLM_URL", "http://127.0.0.1:8080/v1"
)
NARRATIVE_MODEL: str = os.environ.get("NARRATIVE_MODEL", "qwen3.6:27b-think")
NARRATIVE_TIMEOUT_SEC: float = _env_float("NARRATIVE_TIMEOUT_SEC", 90.0)

# Filesystem roots — bind-mounted under /var/lib/dotty-behaviour/ in
# the container, /mnt/user/appdata/dotty-behaviour/ on the Unraid host.
STATE_DIR: Path = Path(
    os.environ.get("DOTTY_STATE_DIR", "/var/lib/dotty-behaviour/state")
)
# Camera voice access uses the canonical bridge-owned policy file, not this
# daemon's independent state directory or startup-only app.state.kid_mode.
KID_MODE_STATE_FILE: Path = Path(os.environ.get(
    "DOTTY_KID_MODE_STATE", "/var/lib/dotty-bridge/state/kid-mode"
))
LOG_DIR: Path = Path(
    os.environ.get("CONVO_LOG_DIR", "/var/lib/dotty-behaviour/logs")
)
SECRETS_DIR: Path = Path(
    os.environ.get("DOTTY_SECRETS_DIR", "/var/lib/dotty-behaviour/secrets")
)

# Per-cache TTLs — identical to bridge.py so the snapshot semantics
# don't drift across the cutover.
VISION_CACHE_TTL_SEC: float = _env_float("VISION_CACHE_TTL_SEC", 60.0)
AUDIO_CACHE_TTL_SEC: float = _env_float("AUDIO_CACHE_TTL_SEC", 120.0)
SCENE_SYNTHESIS_AGE_GATE_SEC: float = _env_float(
    "SCENE_SYNTHESIS_AGE_GATE_SEC", 600.0
)

# Perception bus tuning — match bridge.py's tuned defaults.
PERCEPTION_QUEUE_MAX: int = _env_int("PERCEPTION_QUEUE_MAX", 200)
PERCEPTION_RECENT_MAX: int = _env_int("PERCEPTION_RECENT_MAX", 50)
PERCEPTION_STALE_THRESHOLD_SEC: float = _env_float(
    "PERCEPTION_STALE_THRESHOLD_SEC", 300.0
)

# ---------------------------------------------------------------------------
# Consumer knobs (all mirror bridge.py defaults — env-name compatible so an
# existing /root/zeroclaw-bridge env file works without translation).
# ---------------------------------------------------------------------------

# face_lost_aborter — when a greeting was fired and the face vanishes,
# wait `GRACE` seconds (debounce HuMan-detector flicker) before
# aborting TTS, and only act within `WINDOW` of the last greet.
FACE_LOST_ABORT_WINDOW_SEC: float = _env_float("FACE_LOST_ABORT_WINDOW_SEC", 12.0)
FACE_LOST_ABORT_GRACE_SEC: float = _env_float("FACE_LOST_ABORT_GRACE_SEC", 4.0)

# sound_turner — gentler "curious about an ambient noise" head turn.
SOUND_TURN_COOLDOWN_SEC: float = _env_float("SOUND_TURN_COOLDOWN_SEC", 3.0)
SOUND_TURN_YAW_DEG: int = _env_int("SOUND_TURN_YAW_DEG", 45)
SOUND_TURN_SPEED: int = _env_int("SOUND_TURN_SPEED", 250)
# How long after the last conversation to suppress the ambient turner
# (sound from the user mid-chat shouldn't yank the head). Bridge.py
# hard-codes this as 30.0 inline; surfaced as an env knob here.
SOUND_TURN_QUIET_AFTER_CHAT_SEC: float = _env_float(
    "SOUND_TURN_QUIET_AFTER_CHAT_SEC", 30.0
)

# wake_word_turner — deliberate "look at the speaker who summoned me".
WAKE_TURN_ENABLED: bool = (
    os.environ.get("WAKE_TURN_ENABLED", "1") not in ("0", "false", "False")
)
WAKE_TURN_YAW_DEG: int = _env_int("WAKE_TURN_YAW_DEG", 45)
WAKE_TURN_SPEED: int = _env_int("WAKE_TURN_SPEED", 200)

# face_identified_refresher — periodic re-fire so the firmware face-id
# LED stays green while a person remains in frame (firmware times out
# the green pip after ~4 s on its own).
FACE_IDENTITY_TTL_SEC: float = _env_float("FACE_IDENTITY_TTL_SEC", 30.0)
FACE_IDENTITY_REFRESH_INTERVAL_SEC: float = _env_float(
    "FACE_IDENTITY_REFRESH_INTERVAL_SEC", 3.0
)
FACE_IDENTITY_REFRESH_QUIET_SEC: float = _env_float(
    "FACE_IDENTITY_REFRESH_QUIET_SEC", 2.0
)

# purr_player — cat-purr asset on head_pet_started.
PURR_AUDIO_PATH: str = os.environ.get("PURR_AUDIO_PATH", "/var/lib/dotty-behaviour/assets/purr.opus")
PURR_COOLDOWN_SEC: float = _env_float("PURR_COOLDOWN_SEC", 5.0)
# Approximate playback length — used to extend `last_chat_t` so the
# sound localiser stays quiet while the purr plays.
PURR_DURATION_SEC: float = _env_float("PURR_DURATION_SEC", 2.0)

# sleep_dreamer — schedule N dreams across an estimated 8h sleep window
# at evenly-spaced fractions (default 3 dreams at 25/50/75%). Override
# DREAM_WINDOW_SECONDS for bench testing (e.g. 180 → 45/90/135 s).
DREAMER_ENABLED: bool = os.environ.get("DREAMER_ENABLED", "1") == "1"
DREAM_WINDOW_SECONDS: float = _env_float("DREAM_WINDOW_SECONDS", 28800.0)
DREAM_COUNT_PER_NIGHT: int = _env_int("DREAM_COUNT_PER_NIGHT", 3)

# dance_reflector — write a short LLM reflection on dance_ended.
DANCE_REFLECTOR_ENABLED: bool = (
    os.environ.get("DANCE_REFLECTOR_ENABLED", "1") == "1"
)

# Sci-fi literary seeds — the dreamer picks one uniformly at random
# per dream; the LLM is asked to draw on the seed's atmosphere
# without retelling it. Extend by appending.
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

# ---------------------------------------------------------------------------
# Vision / VLM — OpenRouter-compatible chat completion that accepts an
# image_url content block. Defaults mirror bridge.py so an existing
# OpenRouter key continues to work.
# ---------------------------------------------------------------------------
VISION_MODEL: str = os.environ.get(
    "VISION_MODEL", "google/gemini-3.1-flash-lite"
)
VISION_API_URL: str = os.environ.get(
    "VISION_API_URL", "https://openrouter.ai/api/v1/chat/completions"
)
VISION_API_KEY: str = os.environ.get(
    "VISION_API_KEY", os.environ.get("OPENROUTER_API_KEY", "")
)
VISION_TIMEOUT_SEC: float = _env_float("VISION_TIMEOUT", 15.0)

# Optional split — point the VLM at a local model (e.g. Ollama
# Qwen2.5-VL) while VISION_API_URL still serves the cloud-routed
# narrative LLM. Defaults to the legacy VISION_* values.
VLM_MODEL: str = os.environ.get("VLM_MODEL", VISION_MODEL)
VLM_API_KEY: str = os.environ.get("VLM_API_KEY", VISION_API_KEY)
VLM_API_URL: str = os.environ.get("VLM_API_URL", VISION_API_URL)

# How long an idle-photo room_view cache entry is fresh enough that
# subsequent triggers within the window skip the VLM call.
DOTTY_IDLE_VISION_COOLDOWN_SEC: float = _env_float(
    "DOTTY_IDLE_VISION_COOLDOWN_SEC", 120.0
)
# Grace window inside a fresh `talk` state transition during which a
# room_view photo is still allowed (kickoff capture). Subsequent
# face_detected events inside the same turn arrive >grace seconds
# after the transition and stay gated.
ROOM_VIEW_TALK_KICKOFF_GRACE_SEC: float = _env_float(
    "ROOM_VIEW_TALK_KICKOFF_GRACE_SEC", 5.0
)


# ---------------------------------------------------------------------------
# Audio captioning — OpenAI-style chat completion with an `input_audio`
# content block (Gemini family routes audio through this format on
# OpenRouter). Reuses the OpenRouter key by default so a single
# credential covers both modalities.
# ---------------------------------------------------------------------------
AUDIO_CAPTION_MODEL: str = os.environ.get(
    "AUDIO_CAPTION_MODEL", "google/gemini-2.5-flash"
)
AUDIO_CAPTION_API_KEY: str = os.environ.get(
    "AUDIO_CAPTION_API_KEY", VISION_API_KEY
)
AUDIO_CAPTION_API_URL: str = os.environ.get(
    "AUDIO_CAPTION_API_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)
AUDIO_CAPTION_TIMEOUT_SEC: float = _env_float("AUDIO_CAPTION_TIMEOUT", 20.0)


# ---------------------------------------------------------------------------
# Idle photographer — silent take_photo every IDLE_PHOTOGRAPHER_SLEEP_*
# seconds when the device is idle, no face, not listening. Notability
# gate (Jaccard similarity vs last saved description) suppresses
# repetitive "same scene" writes.
# ---------------------------------------------------------------------------
IDLE_PHOTOGRAPHER_ENABLED: bool = (
    os.environ.get("IDLE_PHOTOGRAPHER_ENABLED", "1") == "1"
)
IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC: float = _env_float(
    "IDLE_PHOTOGRAPHER_SLEEP_MIN_SEC", 180.0
)
IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC: float = _env_float(
    "IDLE_PHOTOGRAPHER_SLEEP_MAX_SEC", 300.0
)
IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC: float = _env_float(
    "IDLE_PHOTOGRAPHER_RESULT_WAIT_SEC", 20.0
)
IDLE_PHOTOGRAPHER_NOTABLE_JACCARD: float = _env_float(
    "IDLE_PHOTOGRAPHER_NOTABLE_JACCARD", 0.7
)

IDLE_WANDER_PROMPT: str = (
    "Describe what you see in 1–3 sentences as a curious robot would "
    "notice it — light, objects, the room's mood. No people "
    "identification needed. Stay short and concrete."
)

# ---------------------------------------------------------------------------
# Scene synthesis — every SCENE_SYNTHESIS_INTERVAL_SEC (or on trigger
# events) compose vision_cache + audio_cache + state into a single
# sentence, append to NDJSON, broadcast a `scene_synthesised` event.
# MIN_GAP suppresses thrashing when many trigger events arrive in a
# burst.
# ---------------------------------------------------------------------------
SCENE_SYNTHESIS_ENABLED: bool = (
    os.environ.get("SCENE_SYNTHESIS_ENABLED", "1") == "1"
)
SCENE_SYNTHESIS_INTERVAL_SEC: float = _env_float(
    "SCENE_SYNTHESIS_INTERVAL_SEC", 300.0
)
SCENE_SYNTHESIS_MIN_GAP_SEC: float = _env_float(
    "SCENE_SYNTHESIS_MIN_GAP_SEC", 120.0
)
SCENE_SYNTHESIS_TRIGGER_STATES: frozenset[str] = frozenset(
    {"story_time", "security", "sleep"}
)
SCENE_SYNTHESIS_TRIGGER_EVENTS: frozenset[str] = frozenset(
    {"face_recognized", "audio_captioned", "state_changed"}
)


# ---------------------------------------------------------------------------
# Face greeter — bare "Hi!" on face_detected when the household has no
# appearance-bearing roster. With a roster, the proactive greeter (Layer
# 6, deferred) handles named greetings via the face_recognized path.
# ---------------------------------------------------------------------------
FACE_GREET_MIN_INTERVAL_SEC: float = _env_float(
    "FACE_GREET_MIN_INTERVAL_SEC",
    _env_float("FACE_GREET_COOLDOWN_SEC", 30.0),
)
FACE_GREET_TEXT: str = os.environ.get("FACE_GREET_TEXT", "Hi!")
FACE_GREET_HOUR_START: int = _env_int("FACE_GREET_HOUR_START", 6)
FACE_GREET_HOUR_END: int = _env_int("FACE_GREET_HOUR_END", 21)

# Name greeter (face_recognized) — "Oh, it's <name>!" via TTS.
FACE_NAME_GREET_MIN_INTERVAL_SEC: float = _env_float(
    "FACE_NAME_GREET_MIN_INTERVAL_SEC", 30.0
)
FACE_NAME_GREET_TEMPLATE: str = os.environ.get(
    "FACE_NAME_GREET_TEMPLATE", "Oh, it's {name}!"
)
FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC: float = _env_float(
    "FACE_NAME_GREET_QUIET_AFTER_CHAT_SEC", 10.0
)

# Path to household.yaml — surfaced as a separate config entry so the
# state-file slice can override it without touching the package.
HOUSEHOLD_YAML_PATH: str = os.environ.get(
    "HOUSEHOLD_YAML_PATH",
    str(STATE_DIR / "household.yaml"),
)

# ---------------------------------------------------------------------------
# Weather + calendar — env-driven external data used by the greeter
# prompt and the dashboard /api/calendar/today route. CALENDAR_IDS
# empty (default) → calendar surface is a quiet no-op.
# ---------------------------------------------------------------------------
WEATHER_LOCATION: str = os.environ.get("WEATHER_LOCATION", "Brisbane")
WEATHER_TTL_SEC: float = _env_float("WEATHER_TTL_SEC", 1800.0)

CALENDAR_TTL_SEC: float = _env_float("CALENDAR_TTL_SEC", 7200.0)
CALENDAR_IDS: tuple[str, ...] = tuple(
    c.strip() for c in os.environ.get("CALENDAR_ID", "").split(",") if c.strip()
)
CALENDAR_SA_PATH: str = os.environ.get(
    "CALENDAR_SA_PATH",
    str(SECRETS_DIR / "google-calendar-sa.json"),
)
GWS_BIN: str = os.environ.get("GWS_BIN", "/usr/local/bin/gws")
CALENDAR_HOUSEHOLD_BUCKET: str = os.environ.get(
    "CALENDAR_HOUSEHOLD_BUCKET", "_household"
)
CALENDAR_PERSON_PREFIX_RE: str = os.environ.get(
    "CALENDAR_PERSON_PREFIX_RE",
    r"^\s*\[(?P<person>[A-Za-z][A-Za-z0-9_-]{0,31})\]\s*(?P<rest>.+)$",
)

# Proactive greeter state file — env-overridable but defaults under the
# bind-mounted state dir.
GREETER_STATE_PATH: str = os.environ.get(
    "GREETER_STATE_PATH",
    str(STATE_DIR / "greeter_state.json"),
)

# ---------------------------------------------------------------------------
# Security capture loop — fires on state_changed → security; runs a
# per-device interval timer that does take_photo + capture_audio +
# NDJSON append. Text-only persistence (image / audio bytes discarded;
# only the VLM/ASR descriptions land on disk).
# ---------------------------------------------------------------------------
SECURITY_CYCLE_ENABLED: bool = (
    os.environ.get("SECURITY_CYCLE_ENABLED", "1") == "1"
)
SECURITY_CAPTURE_INTERVAL_SEC: float = _env_float(
    "SECURITY_CAPTURE_INTERVAL_SEC", 20.0
)
SECURITY_AUDIO_DURATION_MS: int = _env_int("SECURITY_AUDIO_DURATION_MS", 5000)
SECURITY_VLM_PROMPT: str = os.environ.get(
    "SECURITY_VLM_PROMPT",
    "Describe everything visible in this image. Note any people, "
    "movements, or items of interest. Be concise.",
)
SECURITY_VLM_WAIT_SEC: float = _env_float("SECURITY_VLM_TIMEOUT_SEC", 20.0)
SECURITY_RING_BUFFER_SIZE: int = _env_int("SECURITY_RING_BUFFER_SIZE", 60)


AUDIO_CAPTION_SYSTEM_PROMPT: str = (
    "You are listening to a short audio clip from a small family "
    "robot's microphone. Describe what you hear in 1–2 sentences. "
    "Note speech (paraphrase briefly, do not transcribe verbatim), "
    "music, and ambient sounds. Especially flag anything unusual — "
    "raised voices, distress, breaking glass, alarms, impacts, or "
    "sudden loud noises — at the start of your reply. If the clip is "
    "normal ambient sound or quiet conversation, say so briefly. "
    "Do not invent details you cannot hear."
)


def build_vision_system_prompt(kid: bool) -> str:
    """Vision system prompt — toggle on kid_mode for child-safe phrasing.

    Identical wording to bridge.py's `_build_vision_system_prompt` so a
    photo described pre- vs post-cutover comes out the same.
    """
    return (
        "You are describing a photo taken by a small robot's camera "
        "(low resolution). "
        + (
            "Describe what you see in simple, clear language suitable "
            "for a young child. "
            "Focus on objects, colors, and actions. Do NOT identify or "
            "name specific people. "
            "If the image contains anything inappropriate for young "
            "children, say only 'I see something I am not sure about' "
            "without further detail. "
            if kid
            else
            "Describe what you see clearly and concisely. "
            "Focus on objects, people, colors, and actions. "
        )
        + "If the image is blurry or unclear, describe what you can "
        "make out. Keep your description to 2-3 sentences."
    )
