"""Read-only snapshot of Dotty's current perception.

Composes four independent per-device caches owned by bridge.py:

  * `_perception_state[device_id]`   — face presence, identity, listening, mode
  * `_vision_cache[device_id]`       — last VLM photo description (`wall_ts`)
  * `_audio_cache[device_id]`        — last audio caption (`wall_ts`)
  * `_scene_synthesis_cache[device_id]` — last composed sentence (`ts_wall`)

into a single `PerceptionSnapshot` consumed by:

  * `_build_perception_block()` in bridge.py — talk-turn system-prompt addendum
  * future dashboard refactor (out of scope for this commit)

The snapshot is computed at read time; it does not subscribe to events
or hold state of its own. All inputs are passed in explicitly so unit
tests can construct them without monkey-patching the bridge module.

Age gates match the caches' own TTLs (see VISION_CACHE_TTL_SEC,
AUDIO_CACHE_TTL_SEC in bridge.py). Stale fields fall out of the
snapshot rather than appearing as "(2 hours ago)" footnotes — the
prompt block is for *current* perception only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal


VISION_AGE_GATE_SEC = 60.0
AUDIO_AGE_GATE_SEC = 120.0
SCENE_SYNTH_AGE_GATE_SEC = 600.0


FaceState = Literal["off", "detected", "identified"]


STORY_FRAMING_LINE = (
    "You are inside the story you're telling — not narrating from outside. "
    "Describe what you see, hear, and feel as a character in the story. "
    "Use rich sensory language. Save vivid moments to memory when they happen."
)


@dataclass(frozen=True)
class PerceptionSnapshot:
    state: str
    face: FaceState
    face_id: str | None
    face_mood: str | None
    listening: bool
    last_vision_desc: str | None
    last_vision_age_s: float | None
    last_audio_desc: str | None
    last_audio_age_s: float | None
    scene_synth: str | None
    scene_synth_age_s: float | None

    def to_prompt_block(self) -> str:
        """Format as a `[Current perception]` system-prompt addendum.

        Returns "" when nothing meaningful is cached *and* the current
        state has no special framing to add (idle/talk with empty
        caches). Story mode always emits at least the framing line so
        the LLM gets the "you're inside the story" instruction even
        when no other perception is cached.

        Trailing newline included so callers can concatenate without
        padding.
        """
        lines: list[str] = []

        # Story-mode framing — appears first so the rest of the
        # perception ("you see X") reads as in-story sensation.
        if (self.state or "").lower() == "story_time":
            lines.append(STORY_FRAMING_LINE)

        if self.face == "identified" and self.face_id:
            who_line = f"You see {self.face_id} in front of you."
            if self.face_mood:
                who_line += f" They look {self.face_mood}."
            lines.append(who_line)
        elif self.face == "detected":
            who_line = "You see an unrecognised face in front of you."
            if self.face_mood:
                who_line += f" They look {self.face_mood}."
            lines.append(who_line)

        # Prefer the synth sentence (composed, sentence-shaped) over raw
        # vision/audio. The synth loop already merges those signals.
        if self.scene_synth:
            lines.append(self.scene_synth.strip())
        else:
            if self.last_vision_desc:
                lines.append(f"You see: {self.last_vision_desc.strip()}")
            if self.last_audio_desc:
                lines.append(f"You hear: {self.last_audio_desc.strip()}")

        if not lines:
            return ""
        return "[Current perception] " + " ".join(lines) + "\n"


def _age_or_none(wall_ts) -> float | None:
    if not isinstance(wall_ts, (int, float)):
        return None
    return max(0.0, time.time() - float(wall_ts))


def snapshot(
    device_id: str | None,
    *,
    perception_state: dict,
    vision_cache: dict,
    audio_cache: dict,
    scene_synthesis_cache: dict,
) -> PerceptionSnapshot:
    """Compose a frozen snapshot from the four bridge caches.

    All four cache dicts are passed in explicitly — this module never
    imports from bridge.py, so tests can construct synthetic caches
    directly without monkey-patching.
    """
    pstate: dict = perception_state.get(device_id, {}) if device_id else {}

    face: FaceState = "off"
    face_id: str | None = None
    if pstate.get("face_present"):
        identity = (pstate.get("last_face_id") or "").strip()
        if identity and identity != "unknown":
            face = "identified"
            face_id = identity
        else:
            face = "detected"

    # Mood is set by _parse_room_view_response when the VLM returns a
    # value from _ROOM_VIEW_MOODS. Cleared on face_lost so it doesn't
    # outlast the person it describes.
    raw_mood = (pstate.get("face_mood") or "").strip().lower()
    face_mood = raw_mood if raw_mood else None
    if face == "off":
        face_mood = None

    listening = bool(pstate.get("listening"))
    state = pstate.get("current_state") or "idle"

    last_vision_desc: str | None = None
    last_vision_age_s: float | None = None
    if device_id:
        v = vision_cache.get(device_id) or {}
        age = _age_or_none(v.get("wall_ts"))
        desc = (v.get("description") or "").strip()
        if desc and age is not None and age <= VISION_AGE_GATE_SEC:
            last_vision_desc = desc
            last_vision_age_s = age

    last_audio_desc: str | None = None
    last_audio_age_s: float | None = None
    if device_id:
        a = audio_cache.get(device_id) or {}
        age = _age_or_none(a.get("wall_ts"))
        desc = (a.get("description") or "").strip()
        if desc and age is not None and age <= AUDIO_AGE_GATE_SEC:
            last_audio_desc = desc
            last_audio_age_s = age

    # NB: the scene-synthesis cache uses `ts_wall` (not `wall_ts`) —
    # see _scene_synthesis_cache writer in bridge.py. Both spellings
    # exist in the codebase for historical reasons.
    scene_synth: str | None = None
    scene_synth_age_s: float | None = None
    if device_id:
        s = scene_synthesis_cache.get(device_id) or {}
        age = _age_or_none(s.get("ts_wall"))
        text = (s.get("text") or "").strip()
        if text and age is not None and age <= SCENE_SYNTH_AGE_GATE_SEC:
            scene_synth = text
            scene_synth_age_s = age

    return PerceptionSnapshot(
        state=state,
        face=face,
        face_id=face_id,
        face_mood=face_mood,
        listening=listening,
        last_vision_desc=last_vision_desc,
        last_vision_age_s=last_vision_age_s,
        last_audio_desc=last_audio_desc,
        last_audio_age_s=last_audio_age_s,
        scene_synth=scene_synth,
        scene_synth_age_s=scene_synth_age_s,
    )
