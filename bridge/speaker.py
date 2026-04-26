"""SpeakerResolver — "who is talking to Dotty?".

The resolver is a small, deterministic, dependency-light combiner that
turns four weak signals into a single best-guess `Person` plus a
confidence score. Phase 1 of the family-companion identity work; sits
in front of the LLM call so every voice turn can be grounded in "we're
talking to Hudson, not just _household."

Why combine signals
-------------------
Without on-device face recognition (Layer 4 — pending firmware), no
single signal is reliable:

  - **Self-ID** (utterances like "It's Brett") is unambiguous when
    present, but absent on most turns.
  - **Calendar** (today's `[Person]` events near now) is great in a
    routine household but silent on weekends / unscheduled time.
  - **Time-of-day** prior is cheap and surprisingly accurate for kids
    after school / parents in the evening, but degenerates for any
    non-routine moment.
  - **Perception** (recent `face_detected`) tells us *someone is here*
    even before face_recognized lands; it gates the resolver against
    inventing a speaker when the room is empty.

Each signal contributes a weighted vote; the combiner returns the
top-1 person plus the per-signal evidence that picked them, so we can
audit every decision.

Sticky behaviour
----------------
A self-ID match latches the channel onto that person for
`SPEAKER_STICKY_SEC` (default 600 s). The user's natural behaviour is
to talk to Dotty for several turns in one sitting — re-resolving from
scratch every turn would (a) wobble, (b) drop the user mid-thought if
calendar/time priors disagree. The latch survives ACP session
rotation by living bridge-side. A new self-ID phrase always wins ("no
wait, it's Brett") — so anyone can correct an identification
explicitly.

Privacy
-------
Resolver output is *only* a registry id + the data the registry
already exposes. No raw voice fingerprints, no photos, no biometric-
adjacent material. All decisions persist to a small audit table for
tuning; that table never leaves the RPi.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("zeroclaw-bridge.speaker")


# ---------------------------------------------------------------------------
# Signal labels — string constants are easier to grep across logs than enums.
# ---------------------------------------------------------------------------
SIG_SELF_ID = "self_id"
SIG_STICKY = "sticky"
SIG_CALENDAR = "calendar"
SIG_TIME_OF_DAY = "time_of_day"
SIG_PERCEPTION = "perception"
SIG_VLM_MATCH = "vlm_match"
SIG_FALLBACK = "fallback"

# Default weights — small integers chosen so a single self-ID dominates,
# stickiness carries inertia, and prior-only resolutions stay below the
# clarification threshold so we ASK rather than guess.
#
# SIG_VLM_MATCH is the room_view roster identification (description-based,
# no biometrics). Weight 0.6 sits between sticky (0.70) and perception
# (0.40): a fresh visual match should beat the calendar/time priors
# decisively but should NOT override an explicit self-ID ("no it's
# Brett") — and should also not flap against a still-warm sticky latch
# from a previous self-ID, since the latch is the user's expressed
# intent ("this is who I am") whereas the VLM match is a guess.
DEFAULT_WEIGHTS: dict[str, float] = {
    SIG_SELF_ID: 0.95,
    SIG_STICKY: 0.70,
    SIG_VLM_MATCH: 0.60,
    SIG_PERCEPTION: 0.40,  # face_recognized when Layer 4 ships; lower until then
    SIG_CALENDAR: 0.25,
    SIG_TIME_OF_DAY: 0.15,
}

# Time-of-day bucket boundaries (24h, local TZ). Buckets overlap on
# purpose at the seams so the prior is non-zero across transitions.
_TIME_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("morning", 6 * 60, 11 * 60),
    ("afternoon", 11 * 60, 15 * 60),
    ("after-school", 15 * 60, 18 * 60),
    ("early-evening", 18 * 60, 20 * 60),
    ("evening", 20 * 60, 22 * 60),
    ("night", 22 * 60, 24 * 60 + 6 * 60),  # 22:00 → 06:00 next day
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SignalVote:
    """A single piece of evidence pointing at one person."""

    signal: str
    person_id: str
    weight: float
    evidence: str = ""


@dataclass(frozen=True)
class SpeakerResolution:
    """Resolver output. `person_id` is None when nothing matched (the
    bridge falls back to the registry's default_person, typically
    `_household`). `addressee` is the registry display name for prompt
    insertion. `ask_clarification` is True when confidence is below the
    threshold AND we have at least one candidate — the bridge can choose
    to surface a "is that you, X?" question."""

    person_id: Optional[str]
    addressee: Optional[str]
    confidence: float
    votes: tuple[SignalVote, ...] = ()
    ask_clarification: bool = False
    runner_up_id: Optional[str] = None
    runner_up_confidence: float = 0.0


@dataclass
class _StickyState:
    """Per-channel latch state."""

    person_id: str
    set_ts: float
    source: str = SIG_SELF_ID  # what made us latch (today: only self_id)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------
class SpeakerResolver:
    """Resolves "who is talking" per voice turn.

    The resolver is constructed once per bridge process and called
    synchronously on every inbound message. It is thread-safe in the
    asyncio sense: shared mutable state (sticky latches, recent
    perception cache) is guarded by a `threading.Lock` so the rare
    cross-thread call (e.g. a dashboard HTTP handler from FastAPI's thread
    pool) doesn't race with voice traffic.

    Parameters
    ----------
    registry :
        `bridge.household.HouseholdRegistry`. Optional — if None, the
        resolver always returns the fallback identity. Wiring stays
        simple and a missing/empty registry never breaks the bridge.
    calendar_provider :
        Callable returning the current cached calendar events
        (list of dicts as produced by `_calendar_cache["events"]`).
        Optional. Errors are swallowed.
    perception_provider :
        Callable returning a list of perception events seen recently
        (each a dict with `name`, `ts`, optional `data.identity`).
        Optional.
    clock :
        `() -> float` for unix-time. Test seam.
    tz :
        zoneinfo.ZoneInfo for time-of-day resolution. Defaults to the
        TZ env var, then Australia/Brisbane.
    """

    def __init__(
        self,
        registry: Any = None,
        *,
        calendar_provider: Optional[Any] = None,
        perception_provider: Optional[Any] = None,
        clock: Any = None,
        tz: Optional[ZoneInfo] = None,
        weights: Optional[dict[str, float]] = None,
    ) -> None:
        self._registry = registry
        self._calendar = calendar_provider
        self._perception = perception_provider
        self._clock = clock or time.time
        tz_name = os.environ.get("TZ", "Australia/Brisbane")
        self._tz = tz or ZoneInfo(tz_name)
        self._weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        if weights:
            self._weights.update(weights)

        self.sticky_seconds = _env_float("SPEAKER_STICKY_SEC", 600.0)
        self.calendar_window_min = _env_float(
            "SPEAKER_CALENDAR_WINDOW_MIN", 30.0,
        )
        self.perception_window_sec = _env_float(
            "SPEAKER_PERCEPTION_WINDOW_SEC", 30.0,
        )
        # Below this confidence, surface a "Is that you, X?" hint to the
        # caller. Tunable: real-world calibration probably wants 0.4–0.6.
        self.ask_threshold = _env_float("SPEAKER_ASK_THRESHOLD", 0.5)

        self._sticky: dict[str, _StickyState] = {}
        self._lock = threading.Lock()
        # Optional audit hook. The bridge wires this to a SQLite
        # `speaker_decisions` table; keeping it as a callable means the
        # resolver doesn't depend on the storage layer.
        self._audit_hook: Optional[Any] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def set_audit_hook(self, hook: Any) -> None:
        """Register a function called as `hook(resolution, channel,
        request_text)` after every resolve. Errors raised by the hook
        are swallowed."""
        self._audit_hook = hook

    # ------------------------------------------------------------------
    # Public API — resolve()
    # ------------------------------------------------------------------
    def resolve(
        self,
        text: str,
        *,
        channel: Optional[str] = None,
        device_id: Optional[str] = None,
        vlm_match_person_id: Optional[str] = None,
    ) -> SpeakerResolution:
        """Run all signals against the inbound utterance and produce a
        single best-guess person.

        `channel` and `device_id` together key the sticky latch — most
        deployments use channel alone (one device per channel) but we
        accept both for future multi-device setups.

        `vlm_match_person_id` carries the description-based room_view
        identification result (see `bridge.py:_call_vision_api` +
        `_parse_room_view_response`). When the bridge has a fresh VLM
        match for one of the household roster members, pass its
        canonical id here; a SIG_VLM_MATCH vote is appended. Unknown
        ids are silently ignored.
        """
        sticky_key = self._sticky_key(channel, device_id)
        now = float(self._clock())

        votes: list[SignalVote] = []

        # Signal A — self-ID (highest precedence, also resets sticky).
        self_id_person = self._signal_self_id(text)
        if self_id_person is not None:
            votes.append(SignalVote(
                signal=SIG_SELF_ID,
                person_id=self_id_person,
                weight=self._weights[SIG_SELF_ID],
                evidence=f"matched self-id phrase in {text[:40]!r}",
            ))
            self._set_sticky(sticky_key, self_id_person, source=SIG_SELF_ID, ts=now)

        # Signal B — sticky latch (only if not already overridden by self-ID).
        else:
            sticky_id = self._signal_sticky(sticky_key, now)
            if sticky_id is not None:
                votes.append(SignalVote(
                    signal=SIG_STICKY,
                    person_id=sticky_id,
                    weight=self._weights[SIG_STICKY],
                    evidence=f"latched within {self.sticky_seconds:.0f}s window",
                ))

        # Signal C — calendar prior.
        for vote in self._signal_calendar(now):
            votes.append(vote)

        # Signal D — time-of-day prior.
        for vote in self._signal_time_of_day(now):
            votes.append(vote)

        # Signal E — recent perception event.
        for vote in self._signal_perception(now):
            votes.append(vote)

        # Signal F — VLM room_view roster match (description-based,
        # no biometrics). Validated against the registry so a stale or
        # mistyped person_id from upstream can't pin the resolver to a
        # phantom identity.
        if vlm_match_person_id:
            normalised = vlm_match_person_id.strip().lower()
            if normalised and self._registry is not None:
                try:
                    person = self._registry.get(normalised)
                except Exception:
                    log.debug("speaker: registry.get raised in vlm_match", exc_info=True)
                    person = None
                if person is not None:
                    votes.append(SignalVote(
                        signal=SIG_VLM_MATCH,
                        person_id=person.id,
                        weight=self._weights[SIG_VLM_MATCH],
                        evidence="room_view VLM matched roster",
                    ))

        resolution = self._combine(votes, now=now)
        try:
            if self._audit_hook is not None:
                self._audit_hook(resolution, channel or "", text)
        except Exception:
            log.debug("speaker: audit hook raised", exc_info=True)
        return resolution

    # ------------------------------------------------------------------
    # Sticky management (also exposed for tests / explicit corrections)
    # ------------------------------------------------------------------
    def force_set_sticky(
        self, channel: Optional[str], device_id: Optional[str], person_id: str,
    ) -> None:
        """Override the sticky latch programmatically — used by the
        dashboard "I am ..." control and by the optional clarification
        flow ("Is that you, Hudson?" → "yes")."""
        self._set_sticky(
            self._sticky_key(channel, device_id),
            person_id,
            source="manual",
            ts=float(self._clock()),
        )

    def clear_sticky(
        self, channel: Optional[str] = None, device_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._sticky.pop(self._sticky_key(channel, device_id), None)

    def peek_sticky(
        self, channel: Optional[str] = None, device_id: Optional[str] = None,
    ) -> Optional[str]:
        """Read-only view used by tests + the /api/speaker/state dashboard
        endpoint. Returns the person_id if the latch is live, else None."""
        key = self._sticky_key(channel, device_id)
        now = float(self._clock())
        with self._lock:
            state = self._sticky.get(key)
            if state is None:
                return None
            if now - state.set_ts > self.sticky_seconds:
                self._sticky.pop(key, None)
                return None
            return state.person_id

    # ------------------------------------------------------------------
    # Signal implementations
    # ------------------------------------------------------------------
    def _signal_self_id(self, text: str) -> Optional[str]:
        if self._registry is None:
            return None
        try:
            person = self._registry.match_self_id(text)
        except Exception:
            log.debug("speaker: registry.match_self_id raised", exc_info=True)
            return None
        return person.id if person is not None else None

    def _signal_sticky(self, key: str, now: float) -> Optional[str]:
        with self._lock:
            state = self._sticky.get(key)
            if state is None:
                return None
            if now - state.set_ts > self.sticky_seconds:
                self._sticky.pop(key, None)
                return None
            return state.person_id

    def _signal_calendar(self, now: float) -> Iterable[SignalVote]:
        if self._calendar is None or self._registry is None:
            return ()
        try:
            events = self._calendar() or []
        except Exception:
            log.debug("speaker: calendar provider raised", exc_info=True)
            return ()
        if not events:
            return ()

        window_minutes = self.calendar_window_min
        votes: list[SignalVote] = []
        seen_persons: set[str] = set()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            person_tag = ev.get("person")
            if not person_tag or person_tag.startswith("_"):
                continue
            try:
                person = self._registry.get_by_calendar_prefix(person_tag)
            except Exception:
                continue
            if person is None or person.id in seen_persons:
                continue
            # If the event has a parseable start, weight closer events
            # higher. We scale weight by distance-to-start, capped to the
            # configured window.
            distance_min = self._event_distance_minutes(ev, now=now)
            if distance_min is None:
                # Event of unknown time — give a small flat weight so the
                # signal isn't lost, but well below an in-window event.
                weight = self._weights[SIG_CALENDAR] * 0.4
            elif distance_min > window_minutes:
                continue
            else:
                proximity = max(0.0, 1.0 - (distance_min / window_minutes))
                weight = self._weights[SIG_CALENDAR] * (0.5 + 0.5 * proximity)
            seen_persons.add(person.id)
            votes.append(SignalVote(
                signal=SIG_CALENDAR,
                person_id=person.id,
                weight=weight,
                evidence=(
                    f"calendar tag {person_tag!r} within "
                    f"{window_minutes:.0f} min window"
                ),
            ))
        return votes

    def _signal_time_of_day(self, now: float) -> Iterable[SignalVote]:
        if self._registry is None:
            return ()
        bucket = self._current_time_bucket(now)
        if bucket is None:
            return ()
        day_kind = self._current_day_kind(now)

        votes: list[SignalVote] = []
        try:
            people = list(self._registry.iter())
        except Exception:
            log.debug("speaker: registry.iter raised", exc_info=True)
            return ()
        for person in people:
            buckets = person.usual_times.get(day_kind, ()) if person.usual_times else ()
            if not buckets:
                continue
            if "any" in buckets or bucket in buckets:
                votes.append(SignalVote(
                    signal=SIG_TIME_OF_DAY,
                    person_id=person.id,
                    weight=self._weights[SIG_TIME_OF_DAY],
                    evidence=f"usual_times[{day_kind}] includes {bucket}",
                ))
        return votes

    def _signal_perception(self, now: float) -> Iterable[SignalVote]:
        if self._perception is None:
            return ()
        try:
            events = self._perception() or []
        except Exception:
            log.debug("speaker: perception provider raised", exc_info=True)
            return ()
        votes: list[SignalVote] = []
        seen: set[str] = set()
        cutoff = now - self.perception_window_sec
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ts = float(ev.get("ts") or 0.0)
            if ts < cutoff:
                continue
            name = ev.get("name") or ""
            data = ev.get("data") or {}
            if name == "face_recognized":
                identity = (data.get("identity") or "").strip().lower()
                if identity and identity != "unknown" and identity not in seen:
                    # Strong signal: known face seen recently.
                    votes.append(SignalVote(
                        signal=SIG_PERCEPTION,
                        person_id=identity,
                        weight=self._weights[SIG_PERCEPTION],
                        evidence=f"face_recognized {ts:.0f}s",
                    ))
                    seen.add(identity)
            # face_detected (Layer 3 — no identity yet) does not produce a
            # vote; it's used for the "someone is here" gate when we
            # eventually add ASK escalation. Hook for Phase 1.5.
        return votes

    # ------------------------------------------------------------------
    # Combiner
    # ------------------------------------------------------------------
    def _combine(
        self, votes: list[SignalVote], *, now: float,  # noqa: ARG002
    ) -> SpeakerResolution:
        if not votes:
            return self._fallback_resolution(votes=())

        # Aggregate per-person. Zero-weight votes (from a signal whose
        # weight has been turned off via config / tests) are dropped at
        # this stage so they don't pin the resolver to a phantom top-1.
        scores: dict[str, float] = {}
        signals_by_person: dict[str, list[SignalVote]] = {}
        for v in votes:
            if v.weight <= 0:
                continue
            scores[v.person_id] = scores.get(v.person_id, 0.0) + v.weight
            signals_by_person.setdefault(v.person_id, []).append(v)

        if not scores:
            return self._fallback_resolution(votes=tuple(votes))

        # Pick top-1 and runner-up.
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_id, top_score = ranked[0]
        runner_up_id: Optional[str] = None
        runner_up_score: float = 0.0
        if len(ranked) > 1:
            runner_up_id, runner_up_score = ranked[1]

        # Confidence is the top score, capped at 1.0. We deliberately
        # don't normalise across candidates because a clear top score
        # against a sea of zeros should still be high-confidence.
        confidence = min(1.0, top_score)

        # Look up display name; defensive against registry hiccups.
        display_name: Optional[str] = top_id
        if self._registry is not None:
            try:
                p = self._registry.get(top_id)
                if p is not None:
                    display_name = p.display_name
            except Exception:
                log.debug(
                    "speaker: registry.get raised in _combine", exc_info=True,
                )

        ask = (
            confidence < self.ask_threshold
            and SIG_SELF_ID not in {v.signal for v in votes}
            and SIG_STICKY not in {v.signal for v in votes}
        )

        return SpeakerResolution(
            person_id=top_id,
            addressee=display_name,
            confidence=round(confidence, 3),
            votes=tuple(signals_by_person[top_id]),
            ask_clarification=ask,
            runner_up_id=runner_up_id,
            runner_up_confidence=round(runner_up_score, 3),
        )

    def _fallback_resolution(
        self, *, votes: tuple[SignalVote, ...],
    ) -> SpeakerResolution:
        default = (
            self._registry.default_person
            if self._registry is not None else "_household"
        )
        return SpeakerResolution(
            person_id=None,
            addressee=default,
            confidence=0.0,
            votes=votes,
            ask_clarification=False,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _set_sticky(
        self, key: str, person_id: str, *, source: str, ts: float,
    ) -> None:
        with self._lock:
            self._sticky[key] = _StickyState(
                person_id=person_id, set_ts=ts, source=source,
            )

    @staticmethod
    def _sticky_key(
        channel: Optional[str], device_id: Optional[str],
    ) -> str:
        return f"{channel or ''}::{device_id or ''}"

    def _current_time_bucket(self, now: float) -> Optional[str]:
        local = datetime.fromtimestamp(now, tz=self._tz)
        minutes = local.hour * 60 + local.minute
        # Handle wraparound buckets first (night extends past midnight).
        for name, start, end in _TIME_BUCKETS:
            if end > 24 * 60:
                if minutes >= start or minutes < (end - 24 * 60):
                    return name
            else:
                if start <= minutes < end:
                    return name
        return None

    def _current_day_kind(self, now: float) -> str:
        """Returns 'weekdays' or 'weekends'. Public-ish so tests can
        inject. Aligns with the household.yaml `usual_times` keys."""
        local = datetime.fromtimestamp(now, tz=self._tz)
        # Mon=0 ... Sun=6
        return "weekends" if local.weekday() >= 5 else "weekdays"

    @staticmethod
    def _event_distance_minutes(ev: dict, *, now: float) -> Optional[float]:
        """Minutes between `now` and the event's start. Returns None if
        the event has no parseable start. The calendar cache already
        holds an `start_iso` field for parsed events."""
        iso = ev.get("start_iso") or ev.get("start") or ""
        if not iso:
            return None
        try:
            start = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            return None
        return abs((start.timestamp() - now) / 60.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


__all__ = [
    "SpeakerResolver",
    "SpeakerResolution",
    "SignalVote",
    "SIG_SELF_ID",
    "SIG_STICKY",
    "SIG_CALENDAR",
    "SIG_TIME_OF_DAY",
    "SIG_PERCEPTION",
    "SIG_VLM_MATCH",
]
