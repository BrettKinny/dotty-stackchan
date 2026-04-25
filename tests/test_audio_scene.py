"""Unit tests for `bridge.audio_scene.AudioSceneClassifier`.

Pure-unit tests — no real model file, no real tflite-runtime. We poke
at the buffer / cooldown / threshold logic by stubbing the inference
worker and by injecting fake score arrays directly. The goal is to
prove the scaffold's contracts (buffering, threshold, cooldown,
optional-tflite degradation) hold without dragging in numpy/tflite as
hard test deps.
"""
from __future__ import annotations

import importlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure the repo root is importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import audio_scene as audio_scene_mod  # noqa: E402
from bridge.audio_scene import AudioSceneClassifier  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _RecordingBus:
    """Captures emitted events for later assertion."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


def _make_classifier(
    bus: _RecordingBus | None = None,
    *,
    threshold: float = 0.4,
    cooldown_sec: float = 5.0,
    frame_size: int = 15360,
) -> AudioSceneClassifier:
    return AudioSceneClassifier(
        bus=bus,
        model_path="/nonexistent/yamnet.tflite",
        sample_rate=16000,
        frame_size=frame_size,
        threshold=threshold,
        cooldown_sec=cooldown_sec,
        device_id="testdev",
    )


class _FakeArray:
    """Tiny stand-in for a numpy 1-D array. Implements just enough of
    the surface used by `_consume_scores` so the tests don't need
    numpy installed."""

    def __init__(self, data: list[float]) -> None:
        self._data = list(data)
        self.ndim = 1

    @property
    def shape(self) -> tuple[int, ...]:
        return (len(self._data),)

    def __getitem__(self, idx: int) -> float:
        return self._data[idx]

    def reshape(self, *_args, **_kwargs) -> "_FakeArray":
        return self


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class BufferingTests(unittest.TestCase):
    """feed() should accumulate PCM and only schedule inference when
    a full frame is buffered."""

    def test_half_frame_does_not_schedule_inference(self) -> None:
        bus = _RecordingBus()
        clf = _make_classifier(bus, frame_size=16)  # 32 bytes per frame
        # Force "started" state but skip the executor since we only
        # need to inspect buffering behaviour and `_inflight`.
        clf._started = True

        with patch.object(audio_scene_mod, "_TFLITE_AVAILABLE", True), \
             patch.object(audio_scene_mod, "_NUMPY_AVAILABLE", True), \
             patch.object(clf, "_executor", _FakeExecutor()) as ex:
            clf.feed(b"\x00" * 16)
            self.assertEqual(clf.buffered_bytes, 16)
            self.assertEqual(len(ex.submissions), 0)

            clf.feed(b"\x00" * 16)
            self.assertEqual(clf.buffered_bytes, 0)
            self.assertEqual(len(ex.submissions), 1)

    def test_extra_bytes_kept_for_next_frame(self) -> None:
        clf = _make_classifier(frame_size=16)  # 32-byte frame
        clf._started = True
        with patch.object(audio_scene_mod, "_TFLITE_AVAILABLE", True), \
             patch.object(audio_scene_mod, "_NUMPY_AVAILABLE", True), \
             patch.object(clf, "_executor", _FakeExecutor()):
            clf.feed(b"\x00" * 40)  # 32 → frame, 8 leftover
            self.assertEqual(clf.buffered_bytes, 8)


class ThresholdTests(unittest.TestCase):
    """_consume_scores should drop predictions below the threshold."""

    def test_low_confidence_filtered_out(self) -> None:
        bus = _RecordingBus()
        clf = _make_classifier(bus, threshold=0.4)
        # YAMNet display_name "Doorbell" is at index 356 in our curated
        # map. Build a fake scores vector with sub-threshold confidence
        # there and verify nothing was emitted.
        scores = _FakeArray([0.0] * 521)
        scores._data[356] = 0.2  # below 0.4 threshold

        with patch.object(audio_scene_mod, "_np", _FakeNp()):
            clf._consume_scores(scores, device_id="testdev")
        self.assertEqual(bus.events, [])

    def test_high_confidence_emits(self) -> None:
        bus = _RecordingBus()
        clf = _make_classifier(bus, threshold=0.4)
        scores = _FakeArray([0.0] * 521)
        scores._data[356] = 0.9  # well over threshold

        with patch.object(audio_scene_mod, "_np", _FakeNp()):
            clf._consume_scores(scores, device_id="testdev")
        self.assertEqual(len(bus.events), 1)
        ev = bus.events[0]
        self.assertEqual(ev["name"], "sound_event")
        self.assertEqual(ev["data"]["kind"], "doorbell")
        self.assertEqual(ev["data"]["raw_class"], "Doorbell")
        self.assertEqual(ev["data"]["source"], "yamnet")
        self.assertEqual(ev["device_id"], "testdev")
        self.assertGreaterEqual(ev["data"]["confidence"], 0.4)


class CooldownTests(unittest.TestCase):
    """Per-class cooldown should suppress repeat emits and clear
    after the configured window."""

    def test_repeat_within_cooldown_blocked(self) -> None:
        bus = _RecordingBus()
        clf = _make_classifier(bus, threshold=0.4, cooldown_sec=5.0)
        scores = _FakeArray([0.0] * 521)
        scores._data[356] = 0.9

        with patch.object(audio_scene_mod, "_np", _FakeNp()):
            clf._consume_scores(scores, device_id="testdev")
            clf._consume_scores(scores, device_id="testdev")
        self.assertEqual(len(bus.events), 1)

    def test_emit_after_cooldown(self) -> None:
        bus = _RecordingBus()
        clf = _make_classifier(bus, threshold=0.4, cooldown_sec=0.05)
        scores = _FakeArray([0.0] * 521)
        scores._data[356] = 0.9

        with patch.object(audio_scene_mod, "_np", _FakeNp()):
            clf._consume_scores(scores, device_id="testdev")
            time.sleep(0.06)
            clf._consume_scores(scores, device_id="testdev")
        self.assertEqual(len(bus.events), 2)

    def test_cooldown_is_per_class(self) -> None:
        """Different kinds should not share a cooldown bucket."""
        bus = _RecordingBus()
        clf = _make_classifier(bus, threshold=0.4, cooldown_sec=5.0)
        # 356 → Doorbell ("doorbell"), 360 → Knock ("knock")
        scores = _FakeArray([0.0] * 521)
        scores._data[356] = 0.9
        scores._data[360] = 0.9

        with patch.object(audio_scene_mod, "_np", _FakeNp()):
            clf._consume_scores(scores, device_id="testdev")
        kinds = sorted(ev["data"]["kind"] for ev in bus.events)
        self.assertIn("doorbell", kinds)
        self.assertIn("knock", kinds)


class DegradationTests(unittest.TestCase):
    """Module must import and `feed()` must no-op when tflite-runtime
    is missing or the model file can't be loaded."""

    def test_module_imports_without_tflite(self) -> None:
        # Re-import under a faked-missing tflite to ensure the import
        # path doesn't raise. We mutate the cached module's flags
        # since the actual import already happened.
        with patch.object(audio_scene_mod, "_TFLITE_AVAILABLE", False):
            mod = importlib.reload(audio_scene_mod)
            self.assertTrue(hasattr(mod, "AudioSceneClassifier"))

        # restore by re-importing once more (without the patch active)
        importlib.reload(audio_scene_mod)

    def test_feed_is_noop_without_tflite(self) -> None:
        bus = _RecordingBus()
        clf = _make_classifier(bus, frame_size=16)
        clf._started = True
        with patch.object(audio_scene_mod, "_TFLITE_AVAILABLE", False):
            clf.feed(b"\x00" * 64)  # 4 full frames
        self.assertEqual(clf.buffered_bytes, 0)
        self.assertEqual(bus.events, [])

    def test_ensure_model_handles_missing_file(self) -> None:
        clf = _make_classifier()
        # _ensure_model should return False (model file does not exist)
        # without raising, and not retry on subsequent calls.
        with patch.object(audio_scene_mod, "_tflite_interpreter_cls",
                          object()):  # truthy, will hit the isfile check
            self.assertFalse(clf._ensure_model())
            self.assertFalse(clf._ensure_model())  # retry-once policy


# ---------------------------------------------------------------------------
# Test stubs
# ---------------------------------------------------------------------------
class _FakeFuture:
    def done(self) -> bool:
        return True


class _FakeExecutor:
    """Stand-in for ThreadPoolExecutor that records submissions
    instead of running them."""

    def __init__(self) -> None:
        self.submissions: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((fn, args, kwargs))
        return _FakeFuture()

    def shutdown(self, *_a, **_kw) -> None:
        pass


class _FakeNp:
    """Implements only `asarray` — the one numpy call inside
    `_consume_scores`. Returns the input unchanged so our `_FakeArray`
    flows through."""

    def asarray(self, x):
        return x


if __name__ == "__main__":
    unittest.main()
