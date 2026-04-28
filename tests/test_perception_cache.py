"""Unit tests for bridge.perception.cache — the read-only snapshot
façade over the four bridge per-device caches.

Pure unit. Doesn't import bridge.py; constructs synthetic cache dicts
in the same shape bridge.py uses (`wall_ts` for vision/audio,
`ts_wall` for scene synthesis — both spellings exist in the codebase).
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

# Make the repo root importable so `bridge.perception` resolves whether
# tests are run from the repo root or another working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.perception import PerceptionSnapshot, snapshot
from bridge.perception.cache import (
    AUDIO_AGE_GATE_SEC,
    SCENE_SYNTH_AGE_GATE_SEC,
    VISION_AGE_GATE_SEC,
)


DEVICE = "aa:bb:cc:dd:ee:ff"


def _empty_caches():
    return dict(
        perception_state={},
        vision_cache={},
        audio_cache={},
        scene_synthesis_cache={},
    )


class SnapshotEmptyTests(unittest.TestCase):

    def test_empty_caches_returns_off_state(self):
        snap = snapshot(DEVICE, **_empty_caches())
        self.assertEqual(snap.face, "off")
        self.assertIsNone(snap.face_id)
        self.assertFalse(snap.listening)
        self.assertEqual(snap.state, "idle")
        self.assertIsNone(snap.last_vision_desc)
        self.assertIsNone(snap.last_audio_desc)
        self.assertIsNone(snap.scene_synth)

    def test_empty_snapshot_yields_empty_prompt_block(self):
        snap = snapshot(DEVICE, **_empty_caches())
        self.assertEqual(snap.to_prompt_block(), "")

    def test_none_device_id_safe(self):
        snap = snapshot(None, **_empty_caches())
        self.assertEqual(snap.face, "off")
        self.assertEqual(snap.to_prompt_block(), "")


class FaceStateTests(unittest.TestCase):

    def test_face_present_unknown_id_is_detected(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True,
            "last_face_id": "unknown",
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(snap.face, "detected")
        self.assertIsNone(snap.face_id)

    def test_face_present_with_identity_is_identified(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True,
            "last_face_id": "hudson",
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(snap.face, "identified")
        self.assertEqual(snap.face_id, "hudson")

    def test_face_absent_overrides_identity(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": False,
            "last_face_id": "hudson",
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(snap.face, "off")


class VisionAudioTTLTests(unittest.TestCase):

    def test_fresh_vision_appears(self):
        now = time.time()
        caches = _empty_caches()
        caches["vision_cache"][DEVICE] = {
            "description": "a desk lit by warm lamp",
            "wall_ts": now - 5.0,
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(snap.last_vision_desc, "a desk lit by warm lamp")

    def test_stale_vision_excluded(self):
        now = time.time()
        caches = _empty_caches()
        caches["vision_cache"][DEVICE] = {
            "description": "stale stuff",
            "wall_ts": now - (VISION_AGE_GATE_SEC + 5.0),
        }
        snap = snapshot(DEVICE, **caches)
        self.assertIsNone(snap.last_vision_desc)

    def test_audio_uses_120s_ttl(self):
        now = time.time()
        caches = _empty_caches()
        caches["audio_cache"][DEVICE] = {
            "description": "soft footsteps",
            "wall_ts": now - (AUDIO_AGE_GATE_SEC - 5.0),
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(snap.last_audio_desc, "soft footsteps")

    def test_scene_synthesis_uses_ts_wall_key(self):
        now = time.time()
        caches = _empty_caches()
        # Note: scene synthesis writer uses `ts_wall`, not `wall_ts`.
        caches["scene_synthesis_cache"][DEVICE] = {
            "text": "Hudson is at the kitchen table reading.",
            "ts_wall": now - 30.0,
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(
            snap.scene_synth, "Hudson is at the kitchen table reading."
        )

    def test_stale_scene_synthesis_excluded(self):
        now = time.time()
        caches = _empty_caches()
        caches["scene_synthesis_cache"][DEVICE] = {
            "text": "old synth",
            "ts_wall": now - (SCENE_SYNTH_AGE_GATE_SEC + 60.0),
        }
        snap = snapshot(DEVICE, **caches)
        self.assertIsNone(snap.scene_synth)


class PromptBlockTests(unittest.TestCase):

    def test_block_starts_with_marker_and_ends_with_newline(self):
        now = time.time()
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True, "last_face_id": "hudson",
        }
        snap = snapshot(DEVICE, **caches)
        block = snap.to_prompt_block()
        self.assertTrue(block.startswith("[Current perception] "))
        self.assertTrue(block.endswith("\n"))

    def test_identified_face_renders_name(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True, "last_face_id": "hudson",
        }
        snap = snapshot(DEVICE, **caches)
        self.assertIn("hudson", snap.to_prompt_block())

    def test_detected_unknown_face_renders_unrecognised_phrase(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True, "last_face_id": "",
        }
        snap = snapshot(DEVICE, **caches)
        self.assertIn("unrecognised", snap.to_prompt_block())

    def test_synth_prefers_over_raw_vision_audio(self):
        now = time.time()
        caches = _empty_caches()
        caches["scene_synthesis_cache"][DEVICE] = {
            "text": "The room is calm.", "ts_wall": now,
        }
        caches["vision_cache"][DEVICE] = {
            "description": "raw vision desc", "wall_ts": now,
        }
        caches["audio_cache"][DEVICE] = {
            "description": "raw audio desc", "wall_ts": now,
        }
        block = snapshot(DEVICE, **caches).to_prompt_block()
        self.assertIn("The room is calm.", block)
        self.assertNotIn("raw vision desc", block)
        self.assertNotIn("raw audio desc", block)

    def test_raw_vision_audio_used_when_no_synth(self):
        now = time.time()
        caches = _empty_caches()
        caches["vision_cache"][DEVICE] = {
            "description": "warm desk lamp", "wall_ts": now,
        }
        caches["audio_cache"][DEVICE] = {
            "description": "soft music", "wall_ts": now,
        }
        block = snapshot(DEVICE, **caches).to_prompt_block()
        self.assertIn("warm desk lamp", block)
        self.assertIn("soft music", block)

    def test_block_is_single_line_with_trailing_newline(self):
        now = time.time()
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True, "last_face_id": "hudson",
        }
        caches["scene_synthesis_cache"][DEVICE] = {
            "text": "Cup of tea on the table.", "ts_wall": now,
        }
        block = snapshot(DEVICE, **caches).to_prompt_block()
        # One trailing newline; no embedded newlines (keeps prompt
        # ordering tight regardless of which other blocks are present).
        self.assertEqual(block.count("\n"), 1)


class SnapshotIsFrozen(unittest.TestCase):

    def test_snapshot_is_frozen_dataclass(self):
        snap = snapshot(DEVICE, **_empty_caches())
        self.assertIsInstance(snap, PerceptionSnapshot)
        with self.assertRaises(Exception):
            snap.face = "identified"  # type: ignore[misc]


class FaceMoodTests(unittest.TestCase):

    def test_mood_plumbed_through_when_face_present(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True,
            "last_face_id": "hudson",
            "face_mood": "engaged",
        }
        snap = snapshot(DEVICE, **caches)
        self.assertEqual(snap.face_mood, "engaged")

    def test_mood_cleared_when_face_absent(self):
        # If face_lost handling missed the mood pop somehow, the
        # snapshot still scrubs it so a stale mood can't ride into
        # the prompt after the person leaves.
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": False,
            "face_mood": "engaged",  # stale
        }
        snap = snapshot(DEVICE, **caches)
        self.assertIsNone(snap.face_mood)

    def test_mood_renders_in_prompt_block(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "face_present": True,
            "last_face_id": "hudson",
            "face_mood": "tired",
        }
        block = snapshot(DEVICE, **caches).to_prompt_block()
        self.assertIn("hudson", block)
        self.assertIn("tired", block)


class StoryFramingTests(unittest.TestCase):

    def test_story_framing_appears_in_story_time(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "current_state": "story_time",
        }
        block = snapshot(DEVICE, **caches).to_prompt_block()
        self.assertIn("inside the story", block)
        self.assertTrue(block.startswith("[Current perception]"))

    def test_no_story_framing_in_idle(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "current_state": "idle",
        }
        # Empty perception in idle still returns "" — story framing is
        # the only thing that promotes a no-signal snapshot to a
        # non-empty block.
        self.assertEqual(snapshot(DEVICE, **caches).to_prompt_block(), "")

    def test_no_story_framing_in_talk(self):
        caches = _empty_caches()
        caches["perception_state"][DEVICE] = {
            "current_state": "talk",
            "face_present": True,
            "last_face_id": "hudson",
        }
        block = snapshot(DEVICE, **caches).to_prompt_block()
        self.assertNotIn("inside the story", block)


if __name__ == "__main__":
    unittest.main()
