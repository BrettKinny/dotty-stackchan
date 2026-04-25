"""Unit tests for `bridge.speaker.SpeakerResolver`.

Pure-unit tests — no network, no filesystem. All time and calendar
inputs are injected via fakes so the tests are deterministic.
"""
from __future__ import annotations

import sys
import textwrap
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.household import HouseholdRegistry  # noqa: E402
from bridge.speaker import (  # noqa: E402
    SIG_CALENDAR,
    SIG_PERCEPTION,
    SIG_SELF_ID,
    SIG_STICKY,
    SIG_TIME_OF_DAY,
    SpeakerResolution,
    SpeakerResolver,
)


TZ = ZoneInfo("Australia/Brisbane")


def _registry(yaml_body: str) -> HouseholdRegistry:
    """Write a one-shot YAML file and load a registry. Uses tempfile so
    each test gets its own."""
    import os
    import tempfile
    fd, raw = tempfile.mkstemp(suffix=".yaml")
    os.close(fd)
    path = Path(raw)
    path.write_text(textwrap.dedent(yaml_body), encoding="utf-8")
    return HouseholdRegistry(path=path)


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp()


HOUSEHOLD_YAML = """
people:
  alex:
    display_name: Alex
    self_id_phrases: ["it's alex", "alex here"]
    calendar_prefix: "[Alex]"
    usual_times:
      weekdays: [evening, night]
      weekends: [any]
  sam:
    display_name: Sam
    self_id_phrases: ["it's sam", "sam here"]
    calendar_prefix: "[Sam]"
    usual_times:
      weekdays: [after-school, early-evening]
      weekends: [morning, afternoon]
"""


class TestSelfIdSignal(unittest.TestCase):
    def test_self_id_match_dominates(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        # Even with a strong calendar prior pointing at Sam, "It's Alex"
        # should latch the channel onto Alex.
        events = [{"person": "[Sam]", "summary": "Library day"}]
        clock = lambda: _ts(2026, 4, 25, 9, 0)  # Saturday morning
        r = SpeakerResolver(
            registry=reg,
            calendar_provider=lambda: events,
            clock=clock,
            tz=TZ,
        )
        out = r.resolve("It's Alex, can you check my calendar?", channel="dotty")
        self.assertEqual(out.person_id, "alex")
        self.assertEqual(out.addressee, "Alex")
        self.assertGreaterEqual(out.confidence, 0.9)
        self.assertIn(SIG_SELF_ID, [v.signal for v in out.votes])

    def test_self_id_sets_sticky(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock_t = [_ts(2026, 4, 25, 9, 0)]
        r = SpeakerResolver(
            registry=reg,
            clock=lambda: clock_t[0],
            tz=TZ,
        )
        first = r.resolve("It's Alex.", channel="dotty")
        self.assertEqual(first.person_id, "alex")
        # Advance time within the sticky window — next turn should still
        # resolve to Alex via the sticky signal.
        clock_t[0] += 60
        second = r.resolve("How are you today?", channel="dotty")
        self.assertEqual(second.person_id, "alex")
        signals = {v.signal for v in second.votes}
        self.assertIn(SIG_STICKY, signals)
        self.assertNotIn(SIG_SELF_ID, signals)

    def test_sticky_expires(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock_t = [_ts(2026, 4, 25, 9, 0)]
        r = SpeakerResolver(
            registry=reg,
            clock=lambda: clock_t[0],
            tz=TZ,
            weights={
                # Suppress all priors so the absence of sticky is the
                # only thing controlling the outcome.
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0, SIG_PERCEPTION: 0.0,
            },
        )
        r.resolve("It's Alex.", channel="dotty")
        # Advance 2 hours — well past the default 600s sticky window.
        clock_t[0] += 7200
        out = r.resolve("Hi there!", channel="dotty")
        self.assertIsNone(out.person_id)  # falls back

    def test_self_id_overrides_existing_sticky(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock_t = [_ts(2026, 4, 25, 9, 0)]
        r = SpeakerResolver(registry=reg, clock=lambda: clock_t[0], tz=TZ)
        r.resolve("It's Alex.", channel="dotty")
        clock_t[0] += 60
        # Correction mid-conversation. self-ID matches at leading
        # position only ("wait, it's sam" would NOT trigger). The
        # natural way for a user to correct is to lead with the phrase.
        out = r.resolve("It's Sam, sorry — I confused you.", channel="dotty")
        self.assertEqual(out.person_id, "sam")
        signals = {v.signal for v in out.votes}
        self.assertIn(SIG_SELF_ID, signals)

    def test_per_channel_sticky_isolation(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        r = SpeakerResolver(
            registry=reg,
            clock=lambda: _ts(2026, 4, 25, 9, 0),
            tz=TZ,
            weights={
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0, SIG_PERCEPTION: 0.0,
            },
        )
        r.resolve("It's Alex.", channel="dotty")
        # Discord channel sees no sticky for Alex.
        out = r.resolve("Hi", channel="discord")
        self.assertIsNone(out.person_id)
        # But the Dotty channel still has Alex.
        out_dotty = r.resolve("Hi", channel="dotty")
        self.assertEqual(out_dotty.person_id, "alex")


class TestCalendarSignal(unittest.TestCase):
    def test_calendar_within_window(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        # Saturday 09:00 — Alex has [any] weekend usual_times so he'd
        # also win the time-of-day prior. Use a weekday with no usual
        # times for either, so calendar is the only voter.
        # Tuesday 12:00. Sam's usual_times[weekdays] doesn't include
        # 'afternoon' (his is after-school), Alex's [evening, night]
        # neither — clean slate.
        clock = lambda: _ts(2026, 4, 21, 12, 0)
        # Event tagged [Sam] starting in 15 min.
        ev_start = datetime.fromtimestamp(clock() + 15 * 60, tz=TZ).isoformat()
        events = [{
            "person": "[Sam]",
            "summary": "Library day",
            "start_iso": ev_start,
        }]
        r = SpeakerResolver(
            registry=reg,
            calendar_provider=lambda: events,
            clock=clock,
            tz=TZ,
        )
        out = r.resolve("Hi there!", channel="dotty")
        self.assertEqual(out.person_id, "sam")
        self.assertIn(SIG_CALENDAR, [v.signal for v in out.votes])

    def test_calendar_outside_window_ignored(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 21, 12, 0)
        # Event 3 hours away — well outside the default 30 min window.
        ev_start = datetime.fromtimestamp(clock() + 3 * 3600, tz=TZ).isoformat()
        events = [{
            "person": "[Sam]",
            "summary": "Library day",
            "start_iso": ev_start,
        }]
        r = SpeakerResolver(
            registry=reg,
            calendar_provider=lambda: events,
            clock=clock,
            tz=TZ,
        )
        out = r.resolve("Hi there!", channel="dotty")
        # Sam should NOT be picked just from calendar alone.
        signals_for_sam = [
            v for v in out.votes if v.person_id == "sam" and v.signal == SIG_CALENDAR
        ]
        self.assertEqual(signals_for_sam, [])

    def test_calendar_household_tag_skipped(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 21, 12, 0)
        ev_start = datetime.fromtimestamp(clock() + 5 * 60, tz=TZ).isoformat()
        events = [{
            "person": "_household",
            "summary": "Family dinner",
            "start_iso": ev_start,
        }]
        r = SpeakerResolver(
            registry=reg, calendar_provider=lambda: events,
            clock=clock, tz=TZ,
        )
        out = r.resolve("Hi", channel="dotty")
        cal_votes = [v for v in out.votes if v.signal == SIG_CALENDAR]
        self.assertEqual(cal_votes, [])

    def test_calendar_provider_exception_swallowed(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)

        def _broken():
            raise RuntimeError("calendar API down")

        r = SpeakerResolver(
            registry=reg, calendar_provider=_broken,
            clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ,
        )
        # Must not raise.
        out = r.resolve("Hi there!", channel="dotty")
        self.assertIsInstance(out, SpeakerResolution)


class TestTimeOfDaySignal(unittest.TestCase):
    def test_after_school_picks_kid(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        # Tuesday 16:00 — Sam's `weekdays: [after-school, early-evening]`.
        clock = lambda: _ts(2026, 4, 21, 16, 0)
        r = SpeakerResolver(registry=reg, clock=clock, tz=TZ)
        out = r.resolve("hi", channel="dotty")
        self.assertEqual(out.person_id, "sam")
        self.assertIn(SIG_TIME_OF_DAY, [v.signal for v in out.votes])

    def test_evening_picks_parent(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        # Tuesday 21:00 — Alex's `weekdays: [evening, night]`.
        clock = lambda: _ts(2026, 4, 21, 21, 0)
        r = SpeakerResolver(registry=reg, clock=clock, tz=TZ)
        out = r.resolve("hi", channel="dotty")
        self.assertEqual(out.person_id, "alex")

    def test_any_keyword_matches_any_bucket(self) -> None:
        # Alex on weekends: [any]. Saturday 14:00 should match.
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 25, 14, 0)
        r = SpeakerResolver(registry=reg, clock=clock, tz=TZ)
        out = r.resolve("hi", channel="dotty")
        # Both Alex (weekends:[any]) and Sam (weekends:[morning,afternoon])
        # match. Tie on weight; either is acceptable, but Alex MUST be
        # in the votes.
        votes_alex = [v for v in out.votes if v.person_id == "alex"]
        self.assertTrue(votes_alex, "Alex should have a time-of-day vote on Saturday")

    def test_no_usual_times_no_signal(self) -> None:
        reg = _registry("""
            people:
              x:
                display_name: X
        """)
        clock = lambda: _ts(2026, 4, 21, 16, 0)
        r = SpeakerResolver(registry=reg, clock=clock, tz=TZ)
        out = r.resolve("hi", channel="dotty")
        signals = [v.signal for v in out.votes]
        self.assertNotIn(SIG_TIME_OF_DAY, signals)


class TestPerceptionSignal(unittest.TestCase):
    def test_face_recognized_within_window(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 25, 9, 0)
        events = [{
            "name": "face_recognized",
            "ts": clock() - 5,  # 5 seconds ago
            "data": {"identity": "alex"},
        }]
        r = SpeakerResolver(
            registry=reg, perception_provider=lambda: events,
            clock=clock, tz=TZ,
            weights={
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0,
            },
        )
        out = r.resolve("hi", channel="dotty")
        self.assertEqual(out.person_id, "alex")
        self.assertIn(SIG_PERCEPTION, [v.signal for v in out.votes])

    def test_face_recognized_stale_ignored(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 25, 9, 0)
        events = [{
            "name": "face_recognized",
            "ts": clock() - 600,  # 10 minutes ago — past the window
            "data": {"identity": "alex"},
        }]
        r = SpeakerResolver(
            registry=reg, perception_provider=lambda: events,
            clock=clock, tz=TZ,
            weights={
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0,
            },
        )
        out = r.resolve("hi", channel="dotty")
        self.assertIsNone(out.person_id)

    def test_face_detected_no_identity_no_vote(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 25, 9, 0)
        events = [{
            "name": "face_detected",
            "ts": clock() - 5,
            "data": {},
        }]
        r = SpeakerResolver(
            registry=reg, perception_provider=lambda: events,
            clock=clock, tz=TZ,
            weights={
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0,
            },
        )
        out = r.resolve("hi", channel="dotty")
        # face_detected without identity must not invent a person.
        self.assertIsNone(out.person_id)


class TestCombiner(unittest.TestCase):
    def test_no_signals_yields_fallback(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        # Tuesday 12:00 — neither Alex nor Sam have usual_times here.
        # No calendar, no perception, no self-ID, no sticky.
        clock = lambda: _ts(2026, 4, 21, 12, 0)
        r = SpeakerResolver(registry=reg, clock=clock, tz=TZ)
        out = r.resolve("Hi there", channel="dotty")
        self.assertIsNone(out.person_id)
        self.assertEqual(out.addressee, "_household")

    def test_low_confidence_triggers_ask(self) -> None:
        # Single time-of-day vote (weight 0.15) is below the default
        # 0.5 ask threshold and should trigger ask_clarification.
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 21, 16, 0)  # Tue 16:00 → Sam
        r = SpeakerResolver(registry=reg, clock=clock, tz=TZ)
        out = r.resolve("hi", channel="dotty")
        self.assertEqual(out.person_id, "sam")
        self.assertLess(out.confidence, 0.5)
        self.assertTrue(out.ask_clarification)

    def test_self_id_does_not_trigger_ask(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        r = SpeakerResolver(registry=reg, clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ)
        out = r.resolve("It's Alex.", channel="dotty")
        self.assertFalse(out.ask_clarification)

    def test_runner_up_reported(self) -> None:
        # Tie-ish situation: weekday 16:00, both Alex (no match) and Sam
        # (after-school match). With manual weights we can force a tie.
        reg = _registry(HOUSEHOLD_YAML)
        clock = lambda: _ts(2026, 4, 21, 16, 0)
        ev_start = datetime.fromtimestamp(clock() + 5 * 60, tz=TZ).isoformat()
        events = [{
            "person": "[Alex]",
            "summary": "Quick errand",
            "start_iso": ev_start,
        }]
        r = SpeakerResolver(
            registry=reg, calendar_provider=lambda: events,
            clock=clock, tz=TZ,
        )
        out = r.resolve("hi", channel="dotty")
        # Both signals fire; whoever wins, the other is the runner-up.
        candidates = {out.person_id, out.runner_up_id}
        self.assertIn("alex", candidates)
        self.assertIn("sam", candidates)


class TestForceSetSticky(unittest.TestCase):
    def test_force_set_takes_effect(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        r = SpeakerResolver(
            registry=reg, clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ,
            weights={
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0, SIG_PERCEPTION: 0.0,
            },
        )
        # Initial: no signals -> fallback.
        self.assertIsNone(r.resolve("hi", channel="dotty").person_id)
        # Portal/test override.
        r.force_set_sticky("dotty", None, "alex")
        out = r.resolve("hi", channel="dotty")
        self.assertEqual(out.person_id, "alex")
        self.assertEqual(out.addressee, "Alex")

    def test_clear_sticky(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        r = SpeakerResolver(
            registry=reg, clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ,
            weights={
                SIG_CALENDAR: 0.0, SIG_TIME_OF_DAY: 0.0, SIG_PERCEPTION: 0.0,
            },
        )
        r.resolve("It's Alex.", channel="dotty")
        self.assertEqual(r.peek_sticky("dotty", None), "alex")
        r.clear_sticky("dotty", None)
        self.assertIsNone(r.peek_sticky("dotty", None))


class TestAuditHook(unittest.TestCase):
    def test_audit_hook_called(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        captured = []
        r = SpeakerResolver(registry=reg, clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ)
        r.set_audit_hook(lambda res, ch, txt: captured.append((res, ch, txt)))
        r.resolve("It's Alex.", channel="dotty")
        self.assertEqual(len(captured), 1)
        res, ch, txt = captured[0]
        self.assertEqual(res.person_id, "alex")
        self.assertEqual(ch, "dotty")
        self.assertIn("Alex", txt)

    def test_audit_hook_exception_swallowed(self) -> None:
        reg = _registry(HOUSEHOLD_YAML)
        r = SpeakerResolver(registry=reg, clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ)
        r.set_audit_hook(lambda *args, **kw: (_ for _ in ()).throw(RuntimeError("x")))
        # Must not raise.
        out = r.resolve("It's Alex.", channel="dotty")
        self.assertEqual(out.person_id, "alex")


class TestNoRegistry(unittest.TestCase):
    def test_resolver_without_registry_falls_back(self) -> None:
        r = SpeakerResolver(registry=None, clock=lambda: _ts(2026, 4, 21, 12, 0), tz=TZ)
        out = r.resolve("It's Alex.", channel="dotty")
        # No registry → no self-ID match → fallback.
        self.assertIsNone(out.person_id)
        self.assertEqual(out.addressee, "_household")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
