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

# Version stamp surfaced on /health and in startup logs.
VERSION: str = "0.1.0"


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
