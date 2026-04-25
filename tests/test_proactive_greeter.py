"""Unit tests for `bridge.proactive_greeter.ProactiveGreeter`.

Pure-unit tests — no network, no filesystem outside `tmp_path`-style
temp directories. Uses `unittest.mock.AsyncMock` for the LLM and TTS
dependencies and a small fake perception bus.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

# Ensure the repo root is importable when running this file directly.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.proactive_greeter import (  # noqa: E402
    ProactiveGreeter,
    _parse_window,
)


class _FakeBus:
    """Minimal stand-in for the perception bus."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.subscribed = 0
        self.unsubscribed = 0

    def subscribe(self) -> asyncio.Queue:
        self.subscribed += 1
        return self.queue

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.unsubscribed += 1


class _FakeCalendar:
    """Calendar cache stub — empty events, never raises."""

    def __init__(self, events=None, summary=None) -> None:
        self._events = events or []
        self._summary = summary or []

    def get_events(self):
        return list(self._events)

    def summarize_for_prompt(self, events, *, person=None, include_household=True):
        return list(self._summary)


def _greeter(
    *,
    bus=None,
    llm=None,
    calendar=None,
    tts=None,
    kid_mode=lambda: True,
    state_path: Path | None = None,
    fixed_now: datetime | None = None,
):
    """Build a greeter with sensible defaults for tests.

    `fixed_now` (if provided) freezes both the wall-clock and TZ-aware
    "now" used for window checks via a clock callable + a custom tz.
    """
    bus = bus or _FakeBus()
    llm = llm or AsyncMock(return_value="Hi there!")
    calendar = calendar or _FakeCalendar()
    tts = tts or AsyncMock()
    if state_path is None:
        # Use a per-test temp file by default.
        td = tempfile.TemporaryDirectory()
        state_path = Path(td.name) / "greeter_state.json"
        # Keep td alive on the greeter to avoid GC mid-test.
    g = ProactiveGreeter(
        perception_bus=bus,
        llm_client=llm,
        calendar_cache=calendar,
        tts_pusher=tts,
        kid_mode_provider=kid_mode,
        clock=(lambda: fixed_now.timestamp()) if fixed_now else __import__("time").time,
        tz=fixed_now.tzinfo if fixed_now else ZoneInfo("Australia/Brisbane"),
    )
    # Override the state path to the temp file.
    g._state_path = state_path  # type: ignore[attr-defined]
    g._state = {}  # type: ignore[attr-defined]
    if fixed_now is not None:
        # Monkey-patch _current_window to use fixed_now.
        original = g._current_window
        tz = fixed_now.tzinfo

        def _patched():
            minutes = fixed_now.hour * 60 + fixed_now.minute
            for name, (start, end) in g.windows.items():
                if start <= minutes < end:
                    return name
            return None

        g._current_window = _patched  # type: ignore[assignment]
        g._today_key = lambda: fixed_now.strftime("%Y-%m-%d")  # type: ignore[assignment]
    return g, bus, llm, calendar, tts


def _evt(name="face_recognized", identity="Hudson", device_id="dev-1", ts=1000.0):
    return {
        "name": name,
        "device_id": device_id,
        "ts": ts,
        "data": {"identity": identity},
    }


class ParseWindowTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_parse_window("06:00-09:00"), (360, 540))

    def test_invalid(self):
        self.assertIsNone(_parse_window("nope"))
        self.assertIsNone(_parse_window("25:00-26:00"))


class GreeterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Ensure env doesn't bleed in from the host.
        for k in [
            "GREETER_ENABLED",
            "GREETER_USE_FACE_DETECTED",
            "GREETER_GREET_UNKNOWN",
            "GREETER_COOLDOWN_HOURS",
            "GREETER_PER_DAY_MAX",
            "GREETER_MORNING_WINDOW",
            "GREETER_AFTERNOON_WINDOW",
            "GREETER_EVENING_WINDOW",
            "GREETER_STATE_PATH",
            "GREETER_GREETING_MAX_WORDS",
        ]:
            os.environ.pop(k, None)

    async def test_greets_known_face_in_window(self):
        # Tuesday 07:30 in morning window.
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        await g._handle(_evt(identity="Hudson"))
        self.assertEqual(tts.await_count, 1)
        args, _ = tts.call_args
        self.assertEqual(args[0], "dev-1")
        self.assertIn("Hi there!", args[1])

    async def test_cooldown_blocks_repeat(self):
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        # Per-day cap defaults to 1, so two events must collapse to one.
        await g._handle(_evt(identity="Hudson", ts=now.timestamp()))
        await g._handle(_evt(identity="Hudson", ts=now.timestamp() + 5))
        self.assertEqual(tts.await_count, 1)

    async def test_cooldown_window_blocks_within_seconds(self):
        # Per-day cap raised so we isolate the cooldown check.
        os.environ["GREETER_PER_DAY_MAX"] = "10"
        os.environ["GREETER_COOLDOWN_HOURS"] = "1"
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        await g._handle(_evt(identity="Hudson", ts=now.timestamp()))
        # 30 min later — still inside 1 h cooldown.
        await g._handle(_evt(identity="Hudson", ts=now.timestamp() + 1800))
        self.assertEqual(tts.await_count, 1)

    async def test_outside_window_skipped(self):
        # 02:00 — between evening (19-21) and morning (06-09).
        now = datetime(2026, 4, 21, 2, 0, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        await g._handle(_evt(identity="Hudson"))
        self.assertEqual(tts.await_count, 0)
        llm.assert_not_awaited()

    async def test_unknown_face_default_skipped(self):
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        await g._handle(_evt(identity="unknown"))
        self.assertEqual(tts.await_count, 0)

    async def test_unknown_face_optin_greets(self):
        os.environ["GREETER_GREET_UNKNOWN"] = "true"
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        await g._handle(_evt(identity="unknown"))
        self.assertEqual(tts.await_count, 1)
        # No LLM call for unknown — we always use the canned line.
        llm.assert_not_awaited()
        _, text = tts.call_args.args
        self.assertIn("Hello", text)

    async def test_template_fallback_when_llm_raises(self):
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        broken_llm = AsyncMock(side_effect=RuntimeError("openrouter down"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now, llm=broken_llm)
        await g._handle(_evt(identity="Hudson"))
        self.assertEqual(tts.await_count, 1)
        _, text = tts.call_args.args
        self.assertEqual(text, "Good morning, Hudson!")

    async def test_face_detected_promotes_to_unknown_when_enabled(self):
        os.environ["GREETER_USE_FACE_DETECTED"] = "true"
        os.environ["GREETER_GREET_UNKNOWN"] = "true"
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        ev = {
            "name": "face_detected",
            "device_id": "dev-1",
            "ts": now.timestamp(),
            "data": {},
        }
        await g._handle(ev)
        self.assertEqual(tts.await_count, 1)

    async def test_face_detected_ignored_by_default(self):
        # use_face_detected default is False → face_detected events ignored.
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now)
        ev = {
            "name": "face_detected",
            "device_id": "dev-1",
            "ts": now.timestamp(),
            "data": {},
        }
        await g._handle(ev)
        self.assertEqual(tts.await_count, 0)

    async def test_state_persists_round_trip(self):
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            g, bus, llm, cal, tts = _greeter(
                fixed_now=now, state_path=state_path,
            )
            await g._handle(_evt(identity="Hudson", ts=now.timestamp()))
            self.assertTrue(state_path.exists())
            contents = json.loads(state_path.read_text())
            day = now.strftime("%Y-%m-%d")
            self.assertIn(day, contents)
            self.assertIn("Hudson", contents[day])

    async def test_corrupt_state_starts_fresh(self):
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            state_path.write_text("{not valid json")
            g, bus, llm, cal, tts = _greeter(
                fixed_now=now, state_path=state_path,
            )
            # Reload should have been clean.
            g._state = g._load_state()  # type: ignore[attr-defined]
            self.assertEqual(g._state, {})  # type: ignore[attr-defined]
            # And greeting should still fire.
            await g._handle(_evt(identity="Hudson", ts=now.timestamp()))
            self.assertEqual(tts.await_count, 1)

    async def test_tts_failure_swallowed(self):
        now = datetime(2026, 4, 21, 7, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        broken_tts = AsyncMock(side_effect=RuntimeError("network"))
        g, bus, llm, cal, tts = _greeter(fixed_now=now, tts=broken_tts)
        # Must NOT raise.
        await g._handle(_evt(identity="Hudson"))
        broken_tts.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
