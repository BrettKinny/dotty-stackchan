"""Unit tests for the sleep dreamer + dance reflector NDJSON writers
and the dream-text SUMMARY parser.

Pure unit — does NOT exercise the perception consumer loops
end-to-end (those are bench-verified). Covers the helpers:
  * `_split_dream_text` — parses SUMMARY: line out of LLM reply
  * Dream NDJSON record shape
  * Dance NDJSON record shape
  * Dream schedule fractions (1/(N+1), 2/(N+1), …, N/(N+1))
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Mirror of `_split_dream_text` in bridge.py — kept local so the test
# doesn't import the FastAPI app. Keep in sync.
def split_dream_text(raw: str) -> tuple[str, str | None]:
    if not raw:
        return "", None
    text = raw.rstrip()
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if line.lower().startswith("summary:"):
            summary = line.split(":", 1)[1].strip()
            full_text = "\n".join(lines[:i]).rstrip()
            return full_text, (summary or None)
    return text, None


def schedule_fractions(window_s: float, n: int) -> list[float]:
    """Mirror of the `_schedule` math in `_perception_sleep_dreamer`.
    Returns the list of delays in seconds."""
    if n <= 0:
        return []
    return [window_s * (i / (n + 1)) for i in range(1, n + 1)]


class SplitDreamTextTests(unittest.TestCase):

    def test_summary_extracted_when_present(self):
        raw = (
            "I drift through a corridor of mirrors, each one showing a "
            "different version of the kitchen.\n"
            "\n"
            "The clocks all read different times.\n"
            "\n"
            "SUMMARY: A dream of mirrored kitchens with desynchronised clocks."
        )
        full, summary = split_dream_text(raw)
        self.assertNotIn("SUMMARY:", full)
        self.assertEqual(
            summary, "A dream of mirrored kitchens with desynchronised clocks.",
        )

    def test_no_summary_returns_none(self):
        raw = "A dream with no trailing summary line.\n\nIt just ends."
        full, summary = split_dream_text(raw)
        self.assertIn("just ends", full)
        self.assertIsNone(summary)

    def test_empty_raw_returns_empty(self):
        full, summary = split_dream_text("")
        self.assertEqual(full, "")
        self.assertIsNone(summary)

    def test_summary_is_case_insensitive(self):
        raw = "Body of the dream.\nsummary: lowercase summary."
        full, summary = split_dream_text(raw)
        self.assertEqual(summary, "lowercase summary.")
        self.assertEqual(full, "Body of the dream.")


class DreamRecordShapeTests(unittest.TestCase):

    def _record(self, **kwargs) -> dict:
        return {
            "ts": datetime.now(ZoneInfo("UTC")).isoformat(),
            "type": "dream",
            "dream_id": kwargs.get("dream_id", "abc123"),
            "seed": kwargs.get("seed", "Murakami"),
            "summary": kwargs.get("summary", "test summary"),
            "full_text": kwargs.get("full_text", "the full dream text"),
        }

    def test_record_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dreams-test.ndjson"
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(self._record()) + "\n")
            data = json.loads(p.read_text(encoding="utf-8").strip())
            for k in ("ts", "type", "dream_id", "seed", "summary", "full_text"):
                self.assertIn(k, data)
            self.assertEqual(data["type"], "dream")

    def test_summary_can_be_empty_string(self):
        # When LLM omits SUMMARY: we persist "" rather than null —
        # callers can filter on truthy vs not.
        rec = self._record(summary="")
        self.assertEqual(rec["summary"], "")


class DanceRecordShapeTests(unittest.TestCase):

    def test_dance_record_required_fields(self):
        rec = {
            "ts": datetime.now(ZoneInfo("UTC")).isoformat(),
            "type": "dance",
            "device": "aa:bb",
            "dance": "wiggle",
            "reflection": "That was joyful and silly.",
        }
        for k in ("ts", "type", "device", "dance", "reflection"):
            self.assertIn(k, rec)
        self.assertEqual(rec["type"], "dance")


class ScheduleFractionsTests(unittest.TestCase):

    def test_three_dreams_at_25_50_75_percent(self):
        # 8h window, 3 dreams → 25/50/75% = 7200/14400/21600s
        delays = schedule_fractions(28800.0, 3)
        self.assertEqual(len(delays), 3)
        self.assertAlmostEqual(delays[0], 7200.0, places=2)
        self.assertAlmostEqual(delays[1], 14400.0, places=2)
        self.assertAlmostEqual(delays[2], 21600.0, places=2)

    def test_three_minute_bench_window(self):
        # 180s window for bench testing → fires at 45/90/135s.
        delays = schedule_fractions(180.0, 3)
        self.assertEqual(len(delays), 3)
        self.assertAlmostEqual(delays[0], 45.0, places=2)
        self.assertAlmostEqual(delays[1], 90.0, places=2)
        self.assertAlmostEqual(delays[2], 135.0, places=2)

    def test_zero_count_returns_empty(self):
        self.assertEqual(schedule_fractions(28800.0, 0), [])

    def test_single_dream_at_midpoint(self):
        # N=1 → 1/(1+1) = 50% of the window.
        delays = schedule_fractions(28800.0, 1)
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 14400.0, places=2)

    def test_delays_strictly_within_window(self):
        # All delays must be > 0 and < window. With 3 dreams the
        # last one fires at 75% of the window — never on or after it.
        delays = schedule_fractions(28800.0, 3)
        for d in delays:
            self.assertGreater(d, 0)
            self.assertLess(d, 28800.0)


if __name__ == "__main__":
    unittest.main()
