"""Unit tests for ``bridge.engagement_decider.EngagementDecider``.

Pure-unit tests — no network, no filesystem outside ``tempfile``-style
temp directories. Uses ``unittest.mock.AsyncMock`` for the LLM and
TTS dependencies and a small fake perception bus + calendar.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

# Ensure the repo root is importable when running this file directly.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.engagement_decider import (  # noqa: E402
    EngagementDecider,
    DEFAULT_COOLDOWNS,
    HIGH_PRIORITY_INTENTS,
    MOOD_BYPASS_INTENTS,
    _in_window,
    _parse_window,
    _window_name,
)
from bridge.intent_templates import render_template, TEMPLATES  # noqa: E402


TZ = ZoneInfo("Australia/Brisbane")

# Reset env keys that might leak in from the host between tests.
ENV_KEYS = [
    "ENGAGEMENT_ENABLED",
    "ENGAGEMENT_TICK_SEC",
    "ENGAGEMENT_QUIET_HOURS",
    "ENGAGEMENT_ACTIVE_WINDOW",
    "ENGAGEMENT_MOOD_DECAY_MIN",
    "ENGAGEMENT_STATE_PATH",
    "ENGAGEMENT_UTTERANCE_MAX_WORDS",
    "GREETER_GREET_UNKNOWN",
] + [f"ENGAGEMENT_COOLDOWN_{k.upper()}" for k in DEFAULT_COOLDOWNS]


def _clear_env() -> None:
    for k in ENV_KEYS:
        os.environ.pop(k, None)


class _FakeBus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    def subscribe(self) -> asyncio.Queue:
        return self.queue

    def unsubscribe(self, q: asyncio.Queue) -> None:
        pass


class _FakeCalendar:
    def __init__(self, events=None, summary=None) -> None:
        self._events = events or []
        self._summary = summary or []

    def get_events(self):
        return list(self._events)

    def summarize_for_prompt(self, events, *, person=None, include_household=True):
        return list(self._summary)


def _build(
    *,
    fixed_now: datetime,
    llm=None,
    tts=None,
    calendar=None,
    bus=None,
    kid_mode=lambda: True,
    state_path: Path | None = None,
    rng_seed: int = 1234,
) -> tuple[EngagementDecider, _FakeBus, AsyncMock, _FakeCalendar, AsyncMock]:
    """Construct a decider with sensible test defaults.

    `fixed_now` freezes the wall-clock used for cooldown timestamps
    AND the time-of-day decisions, so window/quiet-hours behaviour
    is deterministic.
    """
    bus = bus or _FakeBus()
    llm = llm or AsyncMock(return_value="Hello there!")
    tts = tts or AsyncMock()
    calendar = calendar or _FakeCalendar()

    if state_path is None:
        td = tempfile.TemporaryDirectory()
        state_path = Path(td.name) / "engagement_state.json"

    # Make sure the state path is picked up at construction time so the
    # initial _load_state() honours it (otherwise persistence round-trip
    # tests can't observe the stored state).
    os.environ["ENGAGEMENT_STATE_PATH"] = str(state_path)

    # Mutable holder so tests can advance the clock between operations.
    holder = {"now": fixed_now}

    def _clock() -> float:
        return holder["now"].timestamp()

    def _now() -> datetime:
        return holder["now"]

    decider = EngagementDecider(
        perception_bus_adapter=bus,
        llm_client=llm,
        calendar_facade=calendar,
        tts_pusher=tts,
        kid_mode_provider=kid_mode,
        time_of_day_provider=_now,
        clock=_clock,
        tz=TZ,
        rng=random.Random(rng_seed),
    )
    decider._holder = holder  # type: ignore[attr-defined]
    return decider, bus, llm, calendar, tts


def _push_face_recognized(decider: EngagementDecider, identity: str = "Hudson") -> None:
    decider._on_perception_event({
        "name": "face_recognized",
        "device_id": "dev-1",
        "ts": decider._clock(),
        "data": {"identity": identity},
    })


def _push_face_detected(decider: EngagementDecider) -> None:
    decider._on_perception_event({
        "name": "face_detected",
        "device_id": "dev-1",
        "ts": decider._clock(),
        "data": {},
    })


def _push_voice_turn(decider: EngagementDecider) -> None:
    decider._on_perception_event({
        "name": "voice_turn_completed",
        "device_id": "dev-1",
        "ts": decider._clock(),
        "data": {},
    })


def _push_head_pet(decider: EngagementDecider) -> None:
    decider._on_perception_event({
        "name": "head_pet_started",
        "device_id": "dev-1",
        "ts": decider._clock(),
        "data": {},
    })


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class WindowTests(unittest.TestCase):
    def test_parse_basic(self) -> None:
        self.assertEqual(_parse_window("06:00-21:00"), (360, 21 * 60))

    def test_parse_invalid(self) -> None:
        self.assertIsNone(_parse_window("garbage"))

    def test_in_window_no_wrap(self) -> None:
        self.assertTrue(_in_window(8 * 60, (6 * 60, 21 * 60)))
        self.assertFalse(_in_window(22 * 60, (6 * 60, 21 * 60)))

    def test_in_window_wrap(self) -> None:
        # 22:00-06:00 should match 23:30 and 03:00 but not 12:00.
        self.assertTrue(_in_window(23 * 60 + 30, (22 * 60, 6 * 60)))
        self.assertTrue(_in_window(3 * 60, (22 * 60, 6 * 60)))
        self.assertFalse(_in_window(12 * 60, (22 * 60, 6 * 60)))

    def test_window_name(self) -> None:
        self.assertEqual(_window_name(7 * 60), "morning")
        self.assertEqual(_window_name(13 * 60), "afternoon")
        self.assertEqual(_window_name(18 * 60), "evening")
        self.assertEqual(_window_name(2 * 60), "night")


class TemplateTests(unittest.TestCase):
    def test_casual_greeting_morning(self) -> None:
        line = render_template(
            "casual_greeting", window="morning", params={"name": "Hudson"},
        )
        self.assertEqual(line, "Good morning, Hudson!")

    def test_curiosity_pool_picks_one(self) -> None:
        line = render_template("curiosity", rng=random.Random(0))
        self.assertIn(line, TEMPLATES["curiosity"])

    def test_unknown_intent_returns_empty(self) -> None:
        self.assertEqual(render_template("does_not_exist"), "")

    def test_calendar_reminder_substitution(self) -> None:
        line = render_template(
            "calendar_reminder",
            params={
                "name": "Hudson",
                "event_summary": "library trip",
                "when_human": "9am",
            },
        )
        self.assertIn("Hudson", line)
        self.assertIn("library trip", line)
        self.assertIn("9am", line)

    def test_missing_param_does_not_raise(self) -> None:
        # `name` deliberately absent — should NOT raise KeyError.
        line = render_template(
            "calendar_reminder",
            params={"event_summary": "trip", "when_human": "9am"},
        )
        # Safe-format returns the un-substituted literal template
        # rather than crashing on the missing field. The contract is
        # "no exception, non-empty output" — exact substitution is a
        # nice-to-have we don't want to encode in the test.
        self.assertTrue(line)
        self.assertIn("don't forget", line)


# ---------------------------------------------------------------------------
# Decider behaviour tests
# ---------------------------------------------------------------------------


class EngagementDeciderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _clear_env()

    def tearDown(self) -> None:
        _clear_env()

    # ----- cooldown -----------------------------------------------------
    async def test_cooldown_blocks_repeat_intent(self) -> None:
        # Tuesday 08:30 — inside active window, outside quiet hours.
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, bus, llm, cal, tts = _build(fixed_now=now)
        _push_face_recognized(decider)

        await decider._evaluate_engagement_tick()
        self.assertEqual(tts.await_count, 1)

        # Pin the cooldown registry for the lower-priority intents so
        # the only thing that COULD fire on the second tick is
        # casual_greeting again. (Without this, the second tick might
        # fall through to time_marker / curiosity since they have no
        # registered cooldown yet.)
        decider.cooldown_registry["time_marker"] = decider._clock()
        decider.cooldown_registry["curiosity"] = decider._clock()
        decider.cooldown_registry["unknown_face"] = decider._clock()

        # Advance the clock by 1 minute — far less than 30-min cooldown.
        decider._holder["now"] = now.replace(minute=31)
        _push_face_recognized(decider)
        await decider._evaluate_engagement_tick()
        # Still just 1 — the cooldown blocked the repeat.
        self.assertEqual(tts.await_count, 1)

    # ----- quiet hours --------------------------------------------------
    async def test_quiet_hours_block_all_intents(self) -> None:
        # 02:00 — squarely inside default 22:00-06:00 quiet hours.
        now = datetime(2026, 4, 21, 2, 0, tzinfo=TZ)
        decider, bus, llm, cal, tts = _build(fixed_now=now)
        _push_face_recognized(decider)

        await decider._evaluate_engagement_tick()
        self.assertEqual(tts.await_count, 0)
        llm.assert_not_awaited()

    async def test_quiet_hours_block_even_calendar_reminder(self) -> None:
        now = datetime(2026, 4, 21, 23, 30, tzinfo=TZ)
        cal = _FakeCalendar(
            events=["09:00 [Hudson] Library day"],
            summary=["09:00 Library day"],
        )
        decider, _bus, llm, _cal, tts = _build(fixed_now=now, calendar=cal)
        # Add a stimulus so the decider has a device to talk to.
        _push_face_recognized(decider)

        await decider._evaluate_engagement_tick()
        # Calendar reminder is high-priority but quiet hours hard-block.
        self.assertEqual(tts.await_count, 0)

    # ----- active window ------------------------------------------------
    async def test_outside_active_window_low_priority_blocked(self) -> None:
        # 21:30 — outside default active 06:00-21:00, outside quiet hours.
        os.environ["ENGAGEMENT_QUIET_HOURS"] = "23:00-06:00"
        now = datetime(2026, 4, 21, 21, 30, tzinfo=TZ)
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now)
        # Push curiosity-style stimulus; curiosity is LOW priority.
        _push_voice_turn(decider)
        # Force cooldown registry empty so curiosity could otherwise fire.
        decider.cooldown_registry.clear()

        await decider._evaluate_engagement_tick()
        # No casual_greeting evidence (no face_recognized), curiosity is
        # not high-priority → blocked outside active window.
        self.assertEqual(tts.await_count, 0)

    async def test_outside_active_window_casual_greeting_passes(self) -> None:
        # 21:30 — outside active window, but casual_greeting is HP.
        os.environ["ENGAGEMENT_QUIET_HOURS"] = "23:00-06:00"
        now = datetime(2026, 4, 21, 21, 30, tzinfo=TZ)
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now)
        _push_face_recognized(decider, identity="Hudson")

        await decider._evaluate_engagement_tick()
        self.assertEqual(tts.await_count, 1)

    # ----- mood floor ---------------------------------------------------
    async def test_low_mood_blocks_low_priority_intents(self) -> None:
        now = datetime(2026, 4, 21, 14, 0, tzinfo=TZ)
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now)
        decider.mood = 0.1
        _push_voice_turn(decider)  # not face_recognized → curiosity-only territory

        await decider._evaluate_engagement_tick()
        # Low mood → no curiosity / time_marker firing.
        self.assertEqual(tts.await_count, 0)

    async def test_low_mood_allows_calendar_reminder(self) -> None:
        now = datetime(2026, 4, 21, 14, 0, tzinfo=TZ)
        cal = _FakeCalendar(
            events=["13:30 Library trip"],
            summary=["13:30 Library trip"],
        )
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now, calendar=cal)
        decider.mood = 0.05
        _push_face_recognized(decider, identity="Hudson")
        # Block casual_greeting via cooldown so calendar_reminder comes through.
        # (It's processed first in priority order anyway.)

        await decider._evaluate_engagement_tick()
        # calendar_reminder is in MOOD_BYPASS — should pass.
        self.assertEqual(tts.await_count, 1)

    # ----- stimulus required -------------------------------------------
    async def test_casual_greeting_requires_face_stimulus(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now)
        # No face events pushed.
        await decider._evaluate_engagement_tick()
        self.assertEqual(tts.await_count, 0)

    # ----- mood mechanics ----------------------------------------------
    def test_mood_decay_toward_neutral(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        decider.mood = 0.9
        decider._last_mood_update_ts = decider._clock()
        # Advance clock by 30 minutes — well past τ=10min.
        decider._holder["now"] = now.replace(hour=9, minute=0)
        decider._decay_mood(decider._clock())
        # Should be much closer to 0.5 than 0.9.
        self.assertLess(decider.mood, 0.6)
        self.assertGreater(decider.mood, 0.5)

    def test_mood_bumps_on_positive_event(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        decider.mood = 0.5
        _push_head_pet(decider)
        self.assertGreater(decider.mood, 0.5)

    def test_mood_drops_on_face_lost(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        decider.mood = 0.5
        decider._on_perception_event({
            "name": "face_lost",
            "device_id": "dev-1",
            "ts": decider._clock(),
            "data": {},
        })
        self.assertLess(decider.mood, 0.5)

    def test_mood_clamped_to_unit_range(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        decider.mood = 0.95
        for _ in range(20):
            _push_head_pet(decider)
        self.assertLessEqual(decider.mood, 1.0)
        decider.mood = 0.05
        for _ in range(20):
            decider._on_perception_event({
                "name": "face_lost",
                "device_id": "dev-1",
                "ts": decider._clock(),
                "data": {},
            })
        self.assertGreaterEqual(decider.mood, 0.0)

    # ----- persistence --------------------------------------------------
    def test_state_round_trip(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            decider, *_ = _build(fixed_now=now, state_path=state_path)
            decider.mood = 0.73
            decider.cooldown_registry["casual_greeting"] = decider._clock()
            decider._save_state()
            self.assertTrue(state_path.exists())

            # New decider — should load saved state.
            decider2, *_ = _build(fixed_now=now, state_path=state_path)
            self.assertAlmostEqual(decider2.mood, 0.73)
            self.assertIn("casual_greeting", decider2.cooldown_registry)

    def test_corrupt_state_starts_fresh(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text("{not valid json")
            decider, *_ = _build(fixed_now=now, state_path=state_path)
            # Defaults — fresh start.
            self.assertEqual(decider.mood, EngagementDecider.MOOD_NEUTRAL)
            self.assertEqual(decider.cooldown_registry, {})

    def test_state_file_not_a_dict_starts_fresh(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text("[1, 2, 3]")
            decider, *_ = _build(fixed_now=now, state_path=state_path)
            self.assertEqual(decider.mood, EngagementDecider.MOOD_NEUTRAL)

    # ----- LLM failure → template fallback ------------------------------
    async def test_llm_failure_falls_back_to_template(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        broken_llm = AsyncMock(side_effect=RuntimeError("LLM down"))
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now, llm=broken_llm)
        _push_face_recognized(decider, identity="Hudson")

        await decider._evaluate_engagement_tick()
        self.assertEqual(tts.await_count, 1)
        _, text = tts.call_args.args
        # Template line for morning + Hudson.
        self.assertEqual(text, "Good morning, Hudson!")

    async def test_tts_failure_swallowed(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        broken_tts = AsyncMock(side_effect=RuntimeError("network"))
        decider, _bus, _llm, _cal, _tts = _build(fixed_now=now, tts=broken_tts)
        _push_face_recognized(decider, identity="Hudson")
        # Must NOT raise.
        await decider._evaluate_engagement_tick()
        broken_tts.assert_awaited()

    async def test_calendar_failure_does_not_crash(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        cal = MagicMock()
        cal.get_events.side_effect = RuntimeError("calendar down")
        decider, _bus, _llm, _cal, tts = _build(fixed_now=now, calendar=cal)
        _push_face_recognized(decider, identity="Hudson")
        # Calendar evidence will return None; casual_greeting still runs.
        await decider._evaluate_engagement_tick()
        # casual_greeting should still fire even though calendar errored.
        self.assertEqual(tts.await_count, 1)

    async def test_bad_event_does_not_crash_consumer(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        # Non-dict event — should be silently ignored.
        decider._on_perception_event(None)
        decider._on_perception_event(42)
        decider._on_perception_event("nope")
        self.assertEqual(len(decider.stimulus_pool), 0)

    # ----- env overrides ------------------------------------------------
    def test_env_cooldown_override(self) -> None:
        os.environ["ENGAGEMENT_COOLDOWN_CURIOSITY"] = "7200"
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        self.assertEqual(decider.cooldowns["curiosity"], 7200.0)

    def test_env_quiet_hours_override(self) -> None:
        os.environ["ENGAGEMENT_QUIET_HOURS"] = "23:00-05:00"
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        self.assertEqual(decider.quiet_hours, (23 * 60, 5 * 60))

    # ----- disabled -----------------------------------------------------
    def test_disabled_by_default(self) -> None:
        now = datetime(2026, 4, 21, 8, 30, tzinfo=TZ)
        decider, *_ = _build(fixed_now=now)
        self.assertFalse(decider.enabled)


if __name__ == "__main__":
    unittest.main()
