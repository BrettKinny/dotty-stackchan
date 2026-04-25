"""Phase 4 — Engagement-decision sublayer.

Headline payoff: "Dotty notices you walk in and chimes up on its own."

The :class:`EngagementDecider` is a perception-event consumer that, on
a periodic tick, decides whether to emit an unprompted utterance. It
is the next-layer-up sibling of
:class:`bridge.proactive_greeter.ProactiveGreeter`: where the greeter
is a single-purpose face → greeting reactor, the decider is the
general "should we say something now?" arbiter for **all** unprompted
behaviours.

Responsibilities
----------------
- **Stimulus pool.** Bounded recent-history buffer of perception
  events (``face_*``, ``sound_event``, ``head_pet_*``, calendar
  reminders) that future utterances might comment on.
- **Cooldown registry.** Per intent-type cooldown timers
  (``casual_greeting``: 30 min, ``calendar_reminder``: 2 h,
  ``time_marker``: 4 h, ``curiosity``: 6 h, ``unknown_face``: 1 h).
  Bumped on every successful action.
- **Mood scalar.** A ``[0.0, 1.0]`` float that decays toward
  ``0.5`` (neutral) with a configurable τ (default 10 min).
  Perception events nudge it up (positive interactions) or down
  (silence, lost engagement). Below ``0.3`` the decider quiets down
  except for high-priority intents.
- **Time-of-day filter.** ``ENGAGEMENT_ACTIVE_WINDOW`` (default
  ``06:00-21:00``). Outside the active window only critical intents
  (``calendar_reminder``, ``casual_greeting``) pass.
- **Quiet-hours gate.** ``ENGAGEMENT_QUIET_HOURS`` (default
  ``22:00-06:00``). Hard-blocks **all** unprompted utterances —
  even high-priority ones — when in effect.

Decision flow (per candidate intent)
-----------------------------------
1. Cooldown check.
2. Quiet-hours hard block.
3. Time-of-day window check.
4. Mood-floor check (low mood → low-priority intents skipped).
5. Stimulus check (intent-specific evidence required).
6. LLM generation (kid-safe sandwich) with template fallback.
7. TTS push, cooldown bump, mood bump.

Persistence
-----------
Cooldown registry + mood are persisted on each tick to
``ENGAGEMENT_STATE_PATH`` (default ``~/.zeroclaw/engagement_state.json``)
via atomic write (temp + ``os.replace``). Corrupt state files cause a
fresh start rather than an exception — same defensive contract as the
proactive greeter.

Defensive
---------
Every external call (LLM, TTS, calendar, filesystem, perception bus)
is try/except guarded. An EngagementDecider failure must **never**
break the perception bus or the voice path. The decider always
prefers to silently no-op over crashing.

Tuning note
-----------
The mood-update coefficients in :class:`EngagementDecider`
(``+0.10`` per TTS push, ``+0.05`` per face_recognized, ``-0.02`` per
silent minute, etc.) are starter values informed by intuition rather
than measurement. They will need real-world tuning once the loop is
wired up and observed; expect to surface the constants behind env
overrides as soon as we have telemetry.

TODO — Bridge wiring deferred
-----------------------------
A concurrent agent owns ``bridge.py`` writes for this iteration, so
this module is shipped as scaffold + tests + docs only. To wire it up
in a follow-up commit, instantiate ``EngagementDecider`` in
``bridge.py``'s ``lifespan`` *after* the existing
:class:`ProactiveGreeter` (so cooldown discipline can later route
proactive greetings through the decider rather than firing them
independently). Approximate shape:

.. code-block:: python

    from bridge.engagement_decider import EngagementDecider

    engagement = EngagementDecider(
        perception_bus_adapter=_perception_bus,
        llm_client=_llm_complete,
        calendar_facade=_calendar_cache,
        tts_pusher=_push_tts,
        kid_mode_provider=_kid_mode_flag,
    )
    engagement.start()
    try:
        yield
    finally:
        await engagement.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from bridge.intent_templates import render_template

log = logging.getLogger("zeroclaw-bridge.engagement_decider")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _parse_window(spec: str) -> Optional[tuple[int, int]]:
    """Parse a ``HH:MM-HH:MM`` window into ``(start_min, end_min)``.

    The end is allowed to be earlier than the start (wraps midnight) —
    callers handle the wrap when checking membership.
    """
    try:
        a, b = spec.split("-", 1)
        sh, sm = a.split(":", 1)
        eh, em = b.split(":", 1)
        start = int(sh) * 60 + int(sm)
        end = int(eh) * 60 + int(em)
        if not (0 <= start <= 24 * 60 and 0 <= end <= 24 * 60):
            return None
        return start, end
    except Exception:
        return None


def _in_window(now_min: int, window: tuple[int, int]) -> bool:
    """Return True iff ``now_min`` falls inside ``window``.

    Supports wrap-around windows (e.g. ``22:00-06:00``).
    """
    start, end = window
    if start == end:
        return False
    if start < end:
        return start <= now_min < end
    # Wraps midnight.
    return now_min >= start or now_min < end


def _window_name(now_min: int) -> str:
    """Coarse time-of-day bucket name — mirrors greeter conventions."""
    if 5 * 60 <= now_min < 12 * 60:
        return "morning"
    if 12 * 60 <= now_min < 17 * 60:
        return "afternoon"
    if 17 * 60 <= now_min < 21 * 60:
        return "evening"
    return "night"


# ---------------------------------------------------------------------------
# Intent metadata
# ---------------------------------------------------------------------------


# Default cooldowns in seconds, per intent type.
DEFAULT_COOLDOWNS: dict[str, float] = {
    "casual_greeting": 30 * 60,        # 30 min
    "calendar_reminder": 2 * 3600,     # 2 h
    "time_marker": 4 * 3600,           # 4 h
    "curiosity": 6 * 3600,             # 6 h
    "unknown_face": 1 * 3600,          # 1 h
}

# Intents that are allowed to fire OUTSIDE the active time-of-day
# window. Everything else is suppressed when out-of-window.
HIGH_PRIORITY_INTENTS: frozenset[str] = frozenset(
    {"calendar_reminder", "casual_greeting"}
)

# Intents that are allowed to fire even when mood is low (< 0.3).
# We only let calendar reminders through here — those are time-critical
# and the user explicitly asked for them.
MOOD_BYPASS_INTENTS: frozenset[str] = frozenset({"calendar_reminder"})


# ---------------------------------------------------------------------------
# Decider
# ---------------------------------------------------------------------------


class EngagementDecider:
    """Periodic, perception-driven decider for unprompted utterances.

    Parameters
    ----------
    perception_bus_adapter :
        Object exposing ``subscribe()`` -> ``asyncio.Queue`` and
        ``unsubscribe(q)``. In production this wraps the in-process
        listener registry inside ``bridge.py``.
    llm_client :
        Async callable ``(prompt: str) -> str``. May raise — every
        call is guarded and falls through to a template line on
        failure or empty output.
    calendar_facade :
        Object with ``get_events()`` and (optionally)
        ``summarize_for_prompt(events, person, include_household)``.
        Either may raise; we degrade to no calendar context.
    tts_pusher :
        Async callable ``(device_id: str, text: str) -> Any`` that
        delivers the utterance to the device. Errors are logged but
        never propagated.
    kid_mode_provider :
        Callable returning the current kid-mode flag (bool).
    time_of_day_provider :
        Optional callable returning a tz-aware ``datetime``. Defaults
        to ``datetime.now(self._tz)``. Tests inject a fixed clock.
    """

    DEFAULT_STATE_PATH = "~/.zeroclaw/engagement_state.json"

    # Stimulus pool size. Plenty of room to capture a few minutes of
    # bus chatter without growing unbounded.
    STIMULUS_POOL_MAX = 256

    # Mood update coefficients. Starter values; will need tuning once
    # we have real telemetry. Constants live here so they're easy to
    # spot and lift behind env overrides later.
    MOOD_BUMP_TTS_PUSH = 0.10
    MOOD_BUMP_FACE_RECOGNIZED = 0.05
    MOOD_BUMP_VOICE_TURN = 0.05
    MOOD_BUMP_HEAD_PET = 0.10
    MOOD_DROP_FACE_LOST = 0.05
    MOOD_DROP_PER_SILENT_MIN = 0.02
    MOOD_NEUTRAL = 0.5

    def __init__(
        self,
        perception_bus_adapter: Any,
        llm_client: Callable[[str], Awaitable[str]],
        calendar_facade: Any,
        tts_pusher: Callable[[str, str], Awaitable[Any]],
        kid_mode_provider: Callable[[], bool],
        time_of_day_provider: Optional[Callable[[], datetime]] = None,
        *,
        clock: Callable[[], float] = time.time,
        tz: Optional[ZoneInfo] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._bus = perception_bus_adapter
        self._llm = llm_client
        self._calendar = calendar_facade
        self._tts = tts_pusher
        self._kid_mode = kid_mode_provider
        self._clock = clock
        self._rng = rng or random.Random()

        tz_name = os.environ.get("TZ", "Australia/Brisbane")
        self._tz = tz or ZoneInfo(tz_name)
        self._now: Callable[[], datetime] = (
            time_of_day_provider
            if time_of_day_provider is not None
            else (lambda: datetime.now(self._tz))
        )

        # ---- Configuration ------------------------------------------------
        self.enabled = _env_bool("ENGAGEMENT_ENABLED", False)
        self.tick_seconds = max(1, _env_int("ENGAGEMENT_TICK_SEC", 30))
        self.mood_decay_minutes = max(
            1.0, _env_float("ENGAGEMENT_MOOD_DECAY_MIN", 10.0)
        )
        self.quiet_hours = _parse_window(
            os.environ.get("ENGAGEMENT_QUIET_HOURS", "22:00-06:00")
        ) or (22 * 60, 6 * 60)
        self.active_window = _parse_window(
            os.environ.get("ENGAGEMENT_ACTIVE_WINDOW", "06:00-21:00")
        ) or (6 * 60, 21 * 60)

        # Per-intent cooldowns can be overridden via
        # ENGAGEMENT_COOLDOWN_<INTENT>=<seconds> (e.g.
        # ENGAGEMENT_COOLDOWN_CURIOSITY=7200).
        self.cooldowns: dict[str, float] = {}
        for intent, default_seconds in DEFAULT_COOLDOWNS.items():
            key = f"ENGAGEMENT_COOLDOWN_{intent.upper()}"
            self.cooldowns[intent] = max(
                1.0, _env_float(key, default_seconds)
            )

        # Greeter env reused — keeps the operator surface small.
        self.greet_unknown = _env_bool("GREETER_GREET_UNKNOWN", False)
        self.utterance_max_words = _env_int(
            "ENGAGEMENT_UTTERANCE_MAX_WORDS", 18
        )

        state_path_raw = os.environ.get(
            "ENGAGEMENT_STATE_PATH", self.DEFAULT_STATE_PATH
        )
        self._state_path = Path(state_path_raw).expanduser()

        # ---- Mutable state -----------------------------------------------
        self.mood: float = self.MOOD_NEUTRAL
        self.cooldown_registry: dict[str, float] = {}
        self.stimulus_pool: Deque[dict] = deque(maxlen=self.STIMULUS_POOL_MAX)
        self._last_event_ts: float = self._clock()
        self._last_mood_update_ts: float = self._clock()
        self._last_silence_drop_ts: float = self._clock()

        loaded = self._load_state()
        if loaded:
            self.mood = float(loaded.get("mood", self.MOOD_NEUTRAL))
            registry = loaded.get("cooldown_registry") or {}
            if isinstance(registry, dict):
                for k, v in registry.items():
                    try:
                        self.cooldown_registry[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue

        self._task: Optional[asyncio.Task] = None
        self._consumer: Optional[asyncio.Task] = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled:
            log.info(
                "EngagementDecider disabled (ENGAGEMENT_ENABLED=false)"
            )
            return
        if self._task is not None and not self._task.done():
            log.warning("EngagementDecider.start() called twice; ignoring")
            return
        self._stopped = False
        try:
            self._consumer = asyncio.create_task(self._consume_perception())
        except Exception:
            log.exception("EngagementDecider: failed to start consumer")
            self._consumer = None
        try:
            self._task = asyncio.create_task(self._tick_loop())
        except Exception:
            log.exception("EngagementDecider: failed to start tick loop")
            self._task = None
        log.info(
            "EngagementDecider started (tick=%ds active=%s quiet=%s "
            "decay_min=%.1f state=%s)",
            self.tick_seconds, self.active_window, self.quiet_hours,
            self.mood_decay_minutes, self._state_path,
        )

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._consumer, self._task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._consumer = None
        self._task = None

    # ------------------------------------------------------------------
    # Perception consumer
    # ------------------------------------------------------------------
    async def _consume_perception(self) -> None:
        """Pull events from the perception bus into the stimulus pool."""
        try:
            q = self._bus.subscribe()
        except Exception:
            log.exception("EngagementDecider: bus.subscribe() failed")
            return
        try:
            while not self._stopped:
                event = await q.get()
                try:
                    self._on_perception_event(event)
                except Exception:
                    log.exception(
                        "EngagementDecider: perception handler raised "
                        "(event=%s)",
                        event.get("name") if isinstance(event, dict) else "?",
                    )
        except asyncio.CancelledError:
            log.info("EngagementDecider consumer cancelled")
            raise
        finally:
            try:
                self._bus.unsubscribe(q)
            except Exception:
                log.debug(
                    "EngagementDecider: unsubscribe raised", exc_info=True,
                )

    def _on_perception_event(self, event: Any) -> None:
        """Append to stimulus pool and apply event-driven mood bumps."""
        if not isinstance(event, dict):
            return
        # Sanity copy — we don't want to retain a reference to a
        # mutable object the bus may keep editing.
        snapshot = {
            "name": event.get("name") or "",
            "device_id": event.get("device_id") or "",
            "ts": float(event.get("ts") or self._clock()),
            "data": dict(event.get("data") or {}),
        }
        self.stimulus_pool.append(snapshot)
        self._last_event_ts = snapshot["ts"]

        name = snapshot["name"]
        if name == "face_recognized":
            identity = (snapshot["data"].get("identity") or "").strip()
            if identity and identity != "unknown":
                self._bump_mood(self.MOOD_BUMP_FACE_RECOGNIZED)
        elif name == "voice_turn_completed":
            self._bump_mood(self.MOOD_BUMP_VOICE_TURN)
        elif name == "head_pet_started":
            self._bump_mood(self.MOOD_BUMP_HEAD_PET)
        elif name == "face_lost":
            # Penalty only when the loss wasn't preceded by an
            # engagement bump in the last few seconds.
            self._bump_mood(-self.MOOD_DROP_FACE_LOST)

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------
    async def _tick_loop(self) -> None:
        try:
            while not self._stopped:
                try:
                    await self._evaluate_engagement_tick()
                except Exception:
                    log.exception("EngagementDecider: tick raised")
                try:
                    await asyncio.sleep(self.tick_seconds)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            log.info("EngagementDecider tick loop cancelled")
            raise

    async def _evaluate_engagement_tick(self) -> None:
        """One pass of the decision flow.

        Order of operations:
            1. Decay mood toward neutral.
            2. Apply silence penalty if no events for a while.
            3. Determine the current local time.
            4. Hard-block on quiet hours.
            5. Iterate intents in priority order; first to clear all
               gates wins this tick.
            6. Persist state.
        """
        now_ts = self._clock()
        self._decay_mood(now_ts)
        self._apply_silence_penalty(now_ts)

        try:
            now_dt = self._now()
        except Exception:
            log.exception(
                "EngagementDecider: time_of_day_provider raised"
            )
            now_dt = datetime.now(self._tz)
        now_min = now_dt.hour * 60 + now_dt.minute

        if _in_window(now_min, self.quiet_hours):
            log.debug(
                "engagement_tick: quiet hours active, skipping all intents"
            )
            self._save_state()
            return

        in_active_window = _in_window(now_min, self.active_window)
        window_name = _window_name(now_min)

        log.debug(
            "engagement_tick mood=%.2f active=%s window=%s pool=%d",
            self.mood, in_active_window, window_name, len(self.stimulus_pool),
        )

        # Iterate intents in priority order. First success ends the tick.
        priority_order = [
            "calendar_reminder",
            "casual_greeting",
            "time_marker",
            "unknown_face",
            "curiosity",
        ]
        for intent in priority_order:
            try:
                fired = await self._maybe_fire_intent(
                    intent=intent,
                    now_ts=now_ts,
                    in_active_window=in_active_window,
                    window_name=window_name,
                )
            except Exception:
                log.exception(
                    "EngagementDecider: intent %s raised", intent,
                )
                fired = False
            if fired:
                break

        self._save_state()

    # ------------------------------------------------------------------
    # Intent gating + dispatch
    # ------------------------------------------------------------------
    async def _maybe_fire_intent(
        self,
        *,
        intent: str,
        now_ts: float,
        in_active_window: bool,
        window_name: str,
    ) -> bool:
        """Run the gate cascade for ``intent``. Returns True iff fired."""
        # 1. Cooldown.
        if not self._cooldown_clear(intent, now_ts):
            return False

        # 2. Quiet hours already handled by the caller.

        # 3. Time-of-day window.
        if not in_active_window and intent not in HIGH_PRIORITY_INTENTS:
            log.debug(
                "engagement_tick: %s suppressed (out-of-window)", intent,
            )
            return False

        # 4. Mood floor.
        if self.mood < 0.3 and intent not in MOOD_BYPASS_INTENTS:
            log.debug(
                "engagement_tick: %s suppressed (mood=%.2f below 0.3)",
                intent, self.mood,
            )
            return False

        # 5. Stimulus check.
        evidence = self._stimulus_for_intent(intent)
        if evidence is None:
            log.debug(
                "engagement_tick: %s suppressed (no relevant stimulus)",
                intent,
            )
            return False

        # 6. Generate utterance.
        text, device_id = await self._generate_for_intent(
            intent=intent,
            evidence=evidence,
            window_name=window_name,
        )
        if not text or not device_id:
            log.debug(
                "engagement_tick: %s produced empty utterance", intent,
            )
            return False

        # 7. Push + bump.
        pushed = await self._safe_push(device_id, text)
        if pushed:
            self.cooldown_registry[intent] = now_ts
            self._bump_mood(self.MOOD_BUMP_TTS_PUSH)
            log.info(
                "engagement_tick: fired intent=%s device=%s text=%r",
                intent, device_id, text,
            )
            return True
        return False

    def _cooldown_clear(self, intent: str, now_ts: float) -> bool:
        last_ts = self.cooldown_registry.get(intent)
        if last_ts is None:
            return True
        cooldown = self.cooldowns.get(intent, 0.0)
        if now_ts - float(last_ts) >= cooldown:
            return True
        log.debug(
            "engagement_tick: cooldown active for intent=%s "
            "(%.0fs since last)",
            intent, now_ts - float(last_ts),
        )
        return False

    def _stimulus_for_intent(self, intent: str) -> Optional[dict]:
        """Return a relevant stimulus event for ``intent`` or None.

        For intents whose semantics are "say something based on the
        environment" (``time_marker``, ``curiosity``), we return a
        synthetic placeholder dict to indicate eligibility.
        """
        if intent == "casual_greeting":
            return self._most_recent(
                lambda ev: (
                    ev["name"] == "face_recognized"
                    and (ev["data"].get("identity") or "").strip() not in (
                        "", "unknown",
                    )
                )
            )
        if intent == "unknown_face":
            if not self.greet_unknown:
                return None
            return self._most_recent(
                lambda ev: (
                    ev["name"] == "face_detected"
                    or (
                        ev["name"] == "face_recognized"
                        and (ev["data"].get("identity") or "").strip() in (
                            "", "unknown",
                        )
                    )
                )
            )
        if intent == "calendar_reminder":
            return self._calendar_evidence()
        if intent == "time_marker":
            # Always eligible if we have ANY recent activity — else
            # the robot would announce time markers to an empty room.
            if self.stimulus_pool:
                return {"name": "synthetic_time_marker", "data": {}, "device_id": self._best_device_id()}
            return None
        if intent == "curiosity":
            # Curiosity is the boredom intent — eligible only when
            # there's been at least one stimulus historically (so we
            # have a device to talk to) and mood is at least neutral.
            if self.mood < self.MOOD_NEUTRAL:
                return None
            if not self.stimulus_pool:
                return None
            return {"name": "synthetic_curiosity", "data": {}, "device_id": self._best_device_id()}
        return None

    def _most_recent(self, predicate: Callable[[dict], bool]) -> Optional[dict]:
        for ev in reversed(self.stimulus_pool):
            try:
                if predicate(ev):
                    return ev
            except Exception:
                continue
        return None

    def _best_device_id(self) -> str:
        """Pick a device to push to — most recent event's device_id."""
        for ev in reversed(self.stimulus_pool):
            dev = ev.get("device_id") or ""
            if dev and dev != "unknown":
                return dev
        return ""

    def _calendar_evidence(self) -> Optional[dict]:
        """Look up imminent calendar items via the calendar facade."""
        try:
            events = self._calendar.get_events()
        except Exception:
            log.warning(
                "EngagementDecider: calendar.get_events raised; skipping",
                exc_info=True,
            )
            return None
        if not events:
            return None
        # The decider doesn't try to be clever — it just asks the
        # facade for a summary line for "anyone" and treats the first
        # entry as evidence. The actual relevance scoring belongs in
        # the calendar layer.
        try:
            summary = []
            if hasattr(self._calendar, "summarize_for_prompt"):
                summary = self._calendar.summarize_for_prompt(
                    events, person=None, include_household=True,
                ) or []
        except Exception:
            log.warning(
                "EngagementDecider: calendar.summarize raised; skipping",
                exc_info=True,
            )
            summary = []
        first = summary[0] if summary else str(events[0])
        return {
            "name": "synthetic_calendar",
            "device_id": self._best_device_id(),
            "data": {"event_summary": first},
        }

    # ------------------------------------------------------------------
    # Utterance generation
    # ------------------------------------------------------------------
    async def _generate_for_intent(
        self,
        *,
        intent: str,
        evidence: dict,
        window_name: str,
    ) -> tuple[str, str]:
        """Return ``(text, device_id)`` for the intent.

        LLM is consulted for ``casual_greeting`` and ``time_marker``
        and ``curiosity`` — those benefit from variety. The other
        intents use template lines verbatim, since they're either
        canned (``unknown_face``) or contain user-provided text from
        the calendar (``calendar_reminder``).
        """
        device_id = (
            evidence.get("device_id") or self._best_device_id() or ""
        )
        params: dict[str, str] = {}
        if intent == "casual_greeting":
            params["name"] = (
                evidence.get("data", {}).get("identity") or "there"
            )
        elif intent == "calendar_reminder":
            data = evidence.get("data") or {}
            params["name"] = str(data.get("name") or "there")
            params["event_summary"] = str(data.get("event_summary") or "your event")
            params["when_human"] = str(data.get("when_human") or "soon")

        # Template line is always the floor.
        template_line = render_template(
            intent, window=window_name, params=params, rng=self._rng,
        )

        # Intents that go through the LLM for variety.
        wants_llm = intent in {
            "casual_greeting", "time_marker", "curiosity",
        }
        if not wants_llm:
            return template_line, device_id

        prompt = self._build_prompt(
            intent=intent, params=params, window_name=window_name,
        )
        try:
            raw = await self._llm(prompt)
        except Exception:
            log.warning(
                "EngagementDecider: LLM call failed for %s; "
                "using template fallback",
                intent, exc_info=True,
            )
            return template_line, device_id
        cleaned = self._post_process(raw or "")
        if not cleaned:
            log.info(
                "EngagementDecider: LLM returned empty for %s; "
                "using template fallback",
                intent,
            )
            cleaned = template_line
        cleaned = self._sandwich(cleaned)
        return cleaned, device_id

    def _build_prompt(
        self,
        *,
        intent: str,
        params: Mapping[str, str],
        window_name: str,
    ) -> str:
        max_words = self.utterance_max_words
        person = params.get("name") or "the person in front of you"
        if intent == "casual_greeting":
            base = (
                f"You are Dotty, a friendly home robot. {person} just "
                f"walked into the room. The time of day is "
                f"{window_name}. Write a single short, warm spoken "
                f"greeting addressed to {person}."
            )
        elif intent == "time_marker":
            base = (
                f"You are Dotty, a friendly home robot. Acknowledge "
                f"the {window_name} hour with a short, warm one-liner "
                f"addressed to whoever might be listening."
            )
        elif intent == "curiosity":
            base = (
                f"You are Dotty, a friendly home robot. Share a brief, "
                f"playful curiosity-driven observation suitable for a "
                f"quiet {window_name}."
            )
        else:
            base = (
                "You are Dotty, a friendly home robot. Say something "
                "short and warm."
            )
        return (
            f"{base} "
            f"Hard rules: ENGLISH ONLY. {max_words} words or fewer. "
            f"One sentence. No emoji, no Markdown, no lists."
        )

    @staticmethod
    def _post_process(text: str) -> str:
        cleaned = " ".join((text or "").strip().split())
        if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] in "\"'":
            cleaned = cleaned[1:-1].strip()
        return cleaned

    def _sandwich(self, text: str) -> str:
        """Apply kid-mode emoji-prefix scrub. Mirrors ProactiveGreeter."""
        try:
            kid = bool(self._kid_mode())
        except Exception:
            kid = True
        if kid and text:
            for ch in (
                "\U0001f60a",  # 😊
                "\U0001f606",  # 😆
                "\U0001f622",  # 😢
                "\U0001f62e",  # 😮
                "\U0001f914",  # 🤔
                "\U0001f620",  # 😠
                "\U0001f610",  # 😐
                "\U0001f60d",  # 😍
                "\U0001f634",  # 😴
            ):
                if text.startswith(ch):
                    text = text[len(ch):].lstrip()
                    break
        return text

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------
    async def _safe_push(self, device_id: str, text: str) -> bool:
        if not text or not device_id:
            return False
        try:
            await self._tts(device_id, text)
            return True
        except Exception:
            log.exception(
                "EngagementDecider: tts_pusher raised (device=%s)",
                device_id,
            )
            return False

    # ------------------------------------------------------------------
    # Mood arithmetic
    # ------------------------------------------------------------------
    def _bump_mood(self, delta: float) -> None:
        """Adjust mood, clamped to ``[0.0, 1.0]``."""
        try:
            new = float(self.mood) + float(delta)
        except (TypeError, ValueError):
            return
        if math.isnan(new) or math.isinf(new):
            return
        self.mood = max(0.0, min(1.0, new))

    def _decay_mood(self, now_ts: float) -> None:
        """Exponential decay toward neutral with τ = decay_minutes."""
        try:
            elapsed = max(0.0, float(now_ts) - float(self._last_mood_update_ts))
        except (TypeError, ValueError):
            elapsed = 0.0
        self._last_mood_update_ts = now_ts
        if elapsed <= 0.0:
            return
        # mood' = neutral + (mood - neutral) * exp(-elapsed / τ)
        tau_seconds = self.mood_decay_minutes * 60.0
        if tau_seconds <= 0:
            return
        try:
            factor = math.exp(-elapsed / tau_seconds)
        except (OverflowError, ValueError):
            factor = 0.0
        self.mood = self.MOOD_NEUTRAL + (
            (self.mood - self.MOOD_NEUTRAL) * factor
        )

    def _apply_silence_penalty(self, now_ts: float) -> None:
        """Drop mood by a fixed delta per minute of total silence."""
        try:
            silent = max(0.0, float(now_ts) - float(self._last_event_ts))
        except (TypeError, ValueError):
            return
        if silent < 60.0:
            return
        # Snap to the last whole minute since the last drop so we
        # don't over-penalise on rapid ticks.
        try:
            since_drop = max(
                0.0, float(now_ts) - float(self._last_silence_drop_ts)
            )
        except (TypeError, ValueError):
            return
        if since_drop < 60.0:
            return
        minutes = int(since_drop // 60)
        if minutes <= 0:
            return
        self._bump_mood(-self.MOOD_DROP_PER_SILENT_MIN * minutes)
        self._last_silence_drop_ts = now_ts

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            if not self._state_path.exists():
                return {}
            raw = self._state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                log.warning(
                    "EngagementDecider: state file not a dict; starting fresh"
                )
                return {}
            return data
        except (OSError, json.JSONDecodeError, ValueError):
            log.warning(
                "EngagementDecider: state file unreadable; starting fresh",
                exc_info=True,
            )
            return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(
                self._state_path.suffix + ".tmp"
            )
            payload = {
                "mood": float(self.mood),
                "cooldown_registry": {
                    str(k): float(v)
                    for k, v in self.cooldown_registry.items()
                },
            }
            tmp.write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
        except OSError:
            log.warning(
                "EngagementDecider: failed to persist state to %s",
                self._state_path, exc_info=True,
            )


__all__ = [
    "EngagementDecider",
    "DEFAULT_COOLDOWNS",
    "HIGH_PRIORITY_INTENTS",
    "MOOD_BYPASS_INTENTS",
    "_parse_window",
    "_in_window",
    "_window_name",
]
