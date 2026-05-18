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
VISION_MODEL: str = os.environ.get("VISION_MODEL", "google/gemini-2.0-flash-001")
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
