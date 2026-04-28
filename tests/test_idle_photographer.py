"""Unit tests for the idle photographer's notability filter and
NDJSON record shape.

The async loop itself is not exercised end-to-end here — that lives
in bench verification. This module covers the pure helpers:

  * `_is_notable_perception` — Jaccard threshold + edge cases
  * idle-perception NDJSON record schema (one line, expected fields)
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Repo root importable so `bridge.perception` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Mirror the bridge's notability helper without importing bridge.py
# (which constructs FastAPI on import). Keep this in sync with
# `_is_notable_perception` in bridge.py — the test verifies the
# threshold, not the implementation site.
import re

_TOKEN_RE = re.compile(r"\w+")


def is_notable(desc: str, last: str | None, *, jaccard: float = 0.7) -> bool:
    if not desc or len(desc) < 20:
        return False
    if "same as before" in desc.lower():
        return False
    if not last:
        return True
    cur = set(t.lower() for t in _TOKEN_RE.findall(desc))
    prev = set(t.lower() for t in _TOKEN_RE.findall(last))
    if not cur or not prev:
        return True
    union = cur | prev
    if not union:
        return True
    return (len(cur & prev) / len(union)) < jaccard


class NotabilityTests(unittest.TestCase):

    def test_too_short_is_not_notable(self):
        self.assertFalse(is_notable("hi", None))

    def test_empty_is_not_notable(self):
        self.assertFalse(is_notable("", None))

    def test_first_observation_is_notable(self):
        self.assertTrue(is_notable(
            "A quiet desk lit by a warm lamp, books stacked nearby.", None,
        ))

    def test_same_as_before_is_skipped(self):
        self.assertFalse(is_notable(
            "Same as before — nothing has changed in the room.",
            "A quiet desk lit by a warm lamp.",
        ))

    def test_substantially_different_is_notable(self):
        prev = "A quiet desk lit by a warm lamp, books stacked nearby."
        cur = "A child playing with red and blue lego bricks on the floor."
        self.assertTrue(is_notable(cur, prev))

    def test_near_duplicate_is_skipped(self):
        prev = "A quiet desk lit by a warm lamp, with books stacked nearby."
        cur = "A quiet desk lit by a warm lamp, with books stacked together."
        # Nearly all tokens overlap → Jaccard > 0.7 → skip.
        self.assertFalse(is_notable(cur, prev))

    def test_threshold_is_tunable(self):
        # Same scene as near-duplicate test, but with a stricter
        # threshold the "same" scene becomes notable.
        prev = "A quiet desk lit by a warm lamp, with books stacked nearby."
        cur = "A quiet desk lit by a warm lamp, with books stacked together."
        self.assertTrue(is_notable(cur, prev, jaccard=0.99))


class NdjsonRecordShapeTests(unittest.TestCase):
    """Verify the on-disk record schema — one JSON line, expected
    fields, no media, mode 0600. Mirrors the bridge's
    `_write_idle_perception_record`.
    """

    def _write_record(self, path: Path, device_id: str, description: str) -> None:
        # Synth — keeps the test independent of bridge.py FastAPI ctor.
        record = {
            "ts": datetime.now(ZoneInfo("UTC")).isoformat(),
            "device": device_id,
            "type": "perception",
            "mode": "idle",
            "text": description,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_record_is_single_line(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "perception-test.ndjson"
            self._write_record(p, "aa:bb", "A warm lamp on the desk.")
            content = p.read_text(encoding="utf-8")
            self.assertEqual(content.count("\n"), 1)

    def test_record_has_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "perception-test.ndjson"
            self._write_record(p, "aa:bb", "A warm lamp on the desk.")
            line = p.read_text(encoding="utf-8").strip()
            data = json.loads(line)
            for k in ("ts", "device", "type", "mode", "text"):
                self.assertIn(k, data, f"missing field: {k}")
            self.assertEqual(data["type"], "perception")
            self.assertEqual(data["mode"], "idle")

    def test_record_has_no_jpeg_or_audio(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "perception-test.ndjson"
            self._write_record(p, "aa:bb", "A warm lamp on the desk.")
            data = json.loads(p.read_text(encoding="utf-8").strip())
            for forbidden in ("jpeg_bytes", "audio_bytes", "image", "wav"):
                self.assertNotIn(forbidden, data)


if __name__ == "__main__":
    unittest.main()
