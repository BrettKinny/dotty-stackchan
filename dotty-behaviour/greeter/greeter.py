"""ProactiveGreeter — Layer-6 contextualised named greetings.

Subscribes to the perception bus; on `face_recognized` (or, optionally,
`face_detected` promoted to unknown) generates a context-aware greeting
via the injected LLM client + calendar facade + household registry,
respects per-identity cooldown + per-day cap + persisted state, then
pushes the text to the device via the TTS pusher (typically
XiaozhiAdminClient.say).

Lifted from bridge/proactive_greeter.py. Differences:
  * Events are PerceptionEvent dataclass instances; handler uses
    attribute access (event.name, event.device_id, event.data,
    event.ts) instead of dict .get().
  * Default state path moved off the RPi to
    /var/lib/dotty-behaviour/state/greeter_state.json.
  * Logger renamed.

Failure modes preserved verbatim: LLM error → template fallback,
TTS error → log + (optionally) record the synthetic greeter turn,
state file unreadable → start fresh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from household import PersonResolver
from perception import PerceptionEvent, PerceptionState

log = logging.getLogger("dotty-behaviour.greeter")


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


class ProactiveGreeter:
    DEFAULT_STATE_PATH = "/var/lib/dotty-behaviour/state/greeter_state.json"

    def __init__(
        self,
        perception_state: PerceptionState,
        llm_client: Callable[[str], Awaitable[str]],
        calendar_facade: Any,
        tts_pusher: Callable[[str, str], Awaitable[Any]],
        kid_mode_provider: Callable[[], bool],
        *,
        household_registry: Any = None,
        clock: Callable[[], float] = time.time,
        tz: Optional[ZoneInfo] = None,
        state_path: Optional[str | Path] = None,
    ) -> None:
        self._state_bus = perception_state
        self._llm = llm_client
        self._calendar = calendar_facade
        self._tts = tts_pusher
        self._kid_mode = kid_mode_provider
        self._household = household_registry
        self._resolver = PersonResolver(household_registry)
        self._clock = clock
        tz_name = os.environ.get("TZ", "Australia/Brisbane")
        try:
            self._tz = tz or ZoneInfo(tz_name)
        except Exception:
            self._tz = ZoneInfo("UTC")

        self.enabled = _env_bool("GREETER_ENABLED", True)
        self.use_face_detected = _env_bool(
            "GREETER_USE_FACE_DETECTED", False
        )
        self.greet_unknown = _env_bool("GREETER_GREET_UNKNOWN", False)
        self.cooldown_seconds = (
            _env_float("GREETER_COOLDOWN_HOURS", 4.0) * 3600.0
        )
        self.per_day_max = _env_int("GREETER_PER_DAY_MAX", 3)
        self.greeting_max_words = _env_int("GREETER_GREETING_MAX_WORDS", 15)

        path_raw = (
            state_path
            if state_path is not None
            else os.environ.get("GREETER_STATE_PATH", self.DEFAULT_STATE_PATH)
        )
        self._state_path = Path(path_raw).expanduser()

        # In-memory greet log: {date: {identity: {"count": N, "last_ts": float}}}
        self._state: dict[str, dict[str, dict[str, Any]]] = self._load_state()

    # ------------------------------------------------------------------
    # Lifecycle — exposed as a `run()` coroutine to match other consumers
    # ------------------------------------------------------------------
    async def run(self) -> None:
        if not self.enabled:
            log.info("ProactiveGreeter disabled (GREETER_ENABLED=false)")
            return
        log.info(
            "ProactiveGreeter started "
            "(use_face_detected=%s greet_unknown=%s cooldown_s=%.0f "
            "per_day_max=%d state=%s)",
            self.use_face_detected, self.greet_unknown,
            self.cooldown_seconds, self.per_day_max, self._state_path,
        )
        try:
            q = self._state_bus.subscribe()
        except Exception:
            log.exception("ProactiveGreeter: bus subscribe failed")
            return
        try:
            while True:
                event = await q.get()
                try:
                    await self._handle(event)
                except Exception:
                    log.exception(
                        "ProactiveGreeter: handler raised (event=%s)",
                        getattr(event, "name", "?"),
                    )
        except asyncio.CancelledError:
            log.info("ProactiveGreeter cancelled")
            raise
        finally:
            try:
                self._state_bus.unsubscribe(q)
            except Exception:
                log.debug("ProactiveGreeter: unsubscribe raised", exc_info=True)

    async def _handle(self, event: PerceptionEvent) -> None:
        if event.name == "face_recognized":
            return await self._on_face_recognized(event)
        if event.name == "face_detected" and self.use_face_detected:
            # Promote to face_recognized-style payload with unknown identity
            promoted = PerceptionEvent(
                device_id=event.device_id,
                name="face_recognized",
                data={**(event.data or {}), "identity": "unknown"},
                ts=event.ts,
            )
            return await self._on_face_recognized(promoted)

    async def _on_face_recognized(self, event: PerceptionEvent) -> None:
        device_id = event.device_id
        if not device_id or device_id == "unknown":
            return
        identity = (event.data.get("identity") or "").strip() or "unknown"

        time_of_day = self._current_window()
        t0 = self._clock()

        if identity == "unknown":
            if not self.greet_unknown:
                log.debug("greeter: unknown skipped (GREETER_GREET_UNKNOWN=false)")
                return
            if not self._take_slot(identity, event_ts=event.ts):
                return
            text = self._sandwich(
                "Hello! I don't think we've met.", window=time_of_day,
            )
            await self._safe_push(
                device_id, text, t0=t0,
                request_text=f"face:{identity}",
            )
            return

        if not self._take_slot(identity, event_ts=event.ts):
            return

        greeting = await self._generate_greeting(
            identity=identity, window=time_of_day,
        )
        text = self._sandwich(greeting, window=time_of_day)
        await self._safe_push(
            device_id, text, t0=t0,
            request_text=f"face:{identity}",
        )

    # ------------------------------------------------------------------
    # Time-of-day
    # ------------------------------------------------------------------
    def _current_window(self) -> str:
        now = datetime.fromtimestamp(self._clock(), tz=self._tz)
        hour = now.hour
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "night"

    # ------------------------------------------------------------------
    # Cooldown / per-day cap
    # ------------------------------------------------------------------
    def _today_key(self) -> str:
        return datetime.fromtimestamp(
            self._clock(), tz=self._tz
        ).strftime("%Y-%m-%d")

    def _take_slot(
        self, identity: str, *, event_ts: Optional[float]
    ) -> bool:
        now = float(event_ts) if event_ts else self._clock()
        today = self._today_key()
        # GC old days
        if today not in self._state:
            self._state = {today: self._state.get(today, {})}
        bucket = self._state.setdefault(today, {})
        slot = bucket.get(identity, {"count": 0, "last_ts": 0.0})
        if slot["count"] >= self.per_day_max:
            log.info(
                "greeter: per-day cap reached identity=%s count=%d",
                identity, slot["count"],
            )
            return False
        last_ts = float(slot.get("last_ts") or 0.0)
        if last_ts > 0.0 and now - last_ts < self.cooldown_seconds:
            log.info(
                "greeter: cooldown active identity=%s (%.0fs since last)",
                identity, now - last_ts,
            )
            return False
        slot["count"] = int(slot["count"]) + 1
        slot["last_ts"] = now
        bucket[identity] = slot
        self._save_state()
        return True

    # ------------------------------------------------------------------
    # Greeting generation
    # ------------------------------------------------------------------
    async def _generate_greeting(
        self, *, identity: str, window: str,
    ) -> str:
        events_summary: list[str] = []
        try:
            events = self._calendar.get_events()
            # `identity` is a canonical person id; calendar events carry
            # the free-typed `[Name]` title prefix. The resolver expands
            # the id to every tag that means this person (id, display
            # name, configured calendar_prefix) so their own events
            # survive the case/name-space gap (audit 2026-06-06).
            person_tags = self._resolver.calendar_tags(identity)
            events_summary = self._calendar.summarize_for_prompt(
                events,
                person=person_tags or identity,
                include_household=True,
            ) or []
        except Exception:
            log.warning(
                "greeter: calendar lookup failed for %s; continuing without",
                identity, exc_info=True,
            )

        prompt = self._build_prompt(
            identity=identity, window=window, events=events_summary,
        )
        try:
            text = await self._llm(prompt)
            cleaned = self._post_process(text or "")
            if cleaned:
                return cleaned
            log.info("greeter: LLM returned empty; template fallback")
        except Exception:
            log.warning(
                "greeter: LLM call failed; template fallback",
                exc_info=True,
            )
        return self._template_fallback(identity=identity, window=window)

    def _build_prompt(
        self, *, identity: str, window: str, events: list[str],
    ) -> str:
        bullet_block = (
            "\n".join(f"- {e}" for e in events) if events else "(none)"
        )
        max_words = self.greeting_max_words

        person = self._lookup_person(identity)
        addressee = person.display_name if person else identity
        persona_line = ""
        birthday_line = ""
        if person is not None:
            descr = person.compact_description(max_chars=180)
            if descr:
                persona_line = f"About {addressee}: {descr}\n"
            days = person.days_until_birthday()
            if days is not None:
                if days == 0:
                    birthday_line = (
                        f"It is {addressee}'s birthday today — "
                        f"a warm acknowledgement is welcome.\n"
                    )
                elif 1 <= days <= 7:
                    birthday_line = (
                        f"{addressee}'s birthday is in {days} day"
                        f"{'s' if days != 1 else ''} — you may mention "
                        f"it lightly if it fits.\n"
                    )

        return (
            f"You are Dotty, a friendly home robot. {addressee} just "
            f"walked into the room. The time of day is {window}.\n"
            f"{persona_line}"
            f"{birthday_line}"
            f"Today's calendar items relevant to {addressee}:\n"
            f"{bullet_block}\n\n"
            f"Write a single short, warm spoken greeting addressed to "
            f"{addressee}. If a calendar item is highly relevant to the "
            f"current time-of-day window, you may mention it naturally — "
            f"otherwise just greet them. "
            f"Hard rules: ENGLISH ONLY. {max_words} words or fewer. "
            f"One sentence. No emoji, no Markdown, no lists."
        )

    def _lookup_person(self, identity: str) -> Any:
        # PersonResolver owns the id lookup (case fold, unknown/empty
        # handling, exception safety).
        return self._resolver.resolve(identity)

    @staticmethod
    def _post_process(text: str) -> str:
        cleaned = " ".join(text.strip().split())
        if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] in "\"'":
            cleaned = cleaned[1:-1].strip()
        return cleaned

    def _template_fallback(self, *, identity: str, window: str) -> str:
        person = self._lookup_person(identity)
        addressee = person.display_name if person else identity
        return f"Good {window}, {addressee}!"

    def _sandwich(self, text: str, *, window: str) -> str:  # noqa: ARG002

        try:
            kid = bool(self._kid_mode())
        except Exception:
            kid = True
        if kid:
            for ch in ("😊", "😆", "😢", "😮", "🤔", "😠", "😐", "😍", "😴"):
                if text.startswith(ch):
                    text = text[len(ch):].lstrip()
                    break
        return text

    # ------------------------------------------------------------------
    # TTS push
    # ------------------------------------------------------------------
    async def _safe_push(
        self,
        device_id: str,
        text: str,
        *,
        t0: Optional[float] = None,  # noqa: ARG002 — reserved for turn_logger plumbing
        request_text: str = "",  # noqa: ARG002 — reserved for turn_logger plumbing
    ) -> None:
        if not text:
            return
        try:
            await self._tts(device_id, text)
            log.info(
                "greeter: pushed greeting device=%s text=%r", device_id, text,
            )
        except Exception:
            log.exception(
                "greeter: tts_pusher raised (device=%s)", device_id,
            )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------
    def _load_state(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            if not self._state_path.exists():
                return {}
            raw = self._state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                log.warning("greeter: state file not a dict; starting fresh")
                return {}
            out: dict[str, dict[str, dict[str, Any]]] = {}
            for day, bucket in data.items():
                if not isinstance(bucket, dict):
                    continue
                inner: dict[str, dict[str, Any]] = {}
                for identity, slot in bucket.items():
                    if isinstance(slot, dict):
                        inner[identity] = {
                            "count": int(slot.get("count") or 0),
                            "last_ts": float(slot.get("last_ts") or 0.0),
                        }
                out[str(day)] = inner
            return out
        except (OSError, json.JSONDecodeError, ValueError):
            log.warning(
                "greeter: state file unreadable; starting fresh",
                exc_info=True,
            )
            return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(
                self._state_path.suffix + ".tmp"
            )
            tmp.write_text(
                json.dumps(self._state, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)
        except OSError:
            log.warning(
                "greeter: failed to persist state to %s",
                self._state_path, exc_info=True,
            )
