"""Unit tests for /api/perception/state staleness annotation.

Pure unit — exercises the _annotate helper logic extracted from the
perception_state endpoint without importing bridge.py or starting FastAPI.
"""
from __future__ import annotations

import math
import unittest


_PERCEPTION_STALE_THRESHOLD_S: float = 30.0


def _annotate(raw: dict, *, now: float) -> dict:
    """Mirror of the _annotate closure inside perception_state()."""
    out = dict(raw)
    last_t = out.get("last_event_t")
    if last_t is None:
        age = float("inf")
    else:
        age = max(0.0, now - last_t)
    out["sensor_age_s"] = age
    out["sensor_stale"] = age > _PERCEPTION_STALE_THRESHOLD_S
    return out


class PerceptionStateAnnotationTests(unittest.TestCase):

    def test_fresh_event_not_stale(self):
        now = 1000.0
        state = {"last_event_t": now - 1.0, "face_present": True}
        result = _annotate(state, now=now)
        self.assertFalse(result["sensor_stale"])
        self.assertAlmostEqual(result["sensor_age_s"], 1.0)

    def test_event_exactly_at_threshold_not_stale(self):
        """Age == threshold is NOT stale (boundary: > not >=)."""
        now = 1000.0
        state = {"last_event_t": now - _PERCEPTION_STALE_THRESHOLD_S}
        result = _annotate(state, now=now)
        self.assertFalse(result["sensor_stale"])
        self.assertAlmostEqual(result["sensor_age_s"], _PERCEPTION_STALE_THRESHOLD_S)

    def test_event_one_second_past_threshold_is_stale(self):
        now = 1000.0
        state = {"last_event_t": now - (_PERCEPTION_STALE_THRESHOLD_S + 1.0)}
        result = _annotate(state, now=now)
        self.assertTrue(result["sensor_stale"])
        self.assertAlmostEqual(
            result["sensor_age_s"], _PERCEPTION_STALE_THRESHOLD_S + 1.0
        )

    def test_missing_last_event_t_is_stale(self):
        state = {"face_present": False}
        result = _annotate(state, now=1000.0)
        self.assertTrue(result["sensor_stale"])
        self.assertTrue(math.isinf(result["sensor_age_s"]))

    def test_empty_dict_is_stale(self):
        result = _annotate({}, now=1000.0)
        self.assertTrue(result["sensor_stale"])
        self.assertTrue(math.isinf(result["sensor_age_s"]))

    def test_original_dict_not_mutated(self):
        state = {"last_event_t": 990.0}
        original_keys = set(state.keys())
        _annotate(state, now=1000.0)
        self.assertEqual(set(state.keys()), original_keys)

    def test_existing_fields_preserved(self):
        state = {
            "last_event_t": 999.0,
            "face_present": True,
            "last_face_t": 999.0,
            "last_event_name": "face_detected",
        }
        result = _annotate(state, now=1000.0)
        self.assertEqual(result["face_present"], True)
        self.assertEqual(result["last_face_t"], 999.0)
        self.assertEqual(result["last_event_name"], "face_detected")

    def test_age_clamped_to_zero_when_future_timestamp(self):
        now = 1000.0
        state = {"last_event_t": now + 5.0}
        result = _annotate(state, now=now)
        self.assertEqual(result["sensor_age_s"], 0.0)
        self.assertFalse(result["sensor_stale"])

    def test_30_seconds_minus_epsilon_not_stale(self):
        now = 1000.0
        state = {"last_event_t": now - 29.999}
        result = _annotate(state, now=now)
        self.assertFalse(result["sensor_stale"])

    def test_30_seconds_plus_epsilon_is_stale(self):
        now = 1000.0
        state = {"last_event_t": now - 30.001}
        result = _annotate(state, now=now)
        self.assertTrue(result["sensor_stale"])


if __name__ == "__main__":
    unittest.main()
