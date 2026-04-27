"""Smoke tests for bridge.security_watch.

Pure unit — no network, no real filesystem persistence (uses tmp_path),
no bridge.py import. Verifies:
  * dispatchers POST to the right URLs and degrade gracefully on 404
  * NDJSON record is text-only (no jpeg/audio bytes ever land in it)
  * state_changed=security spins up a per-device timer; non-security
    state cancels it
  * the cycle records `audio_capture_pending` when the audio relay 404s
    (the expected v1 path until firmware ships self.audio.capture_clip)
  * the in-memory ring buffer caps at RING_BUFFER_SIZE
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import security_watch  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class DispatchTakePhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the one-shot warning latch so tests are order-independent.
        security_watch._PHOTO_RELAY_MISSING_LOGGED = False

    def test_returns_false_when_no_host(self):
        self.assertFalse(_run(security_watch.dispatch_take_photo(
            "dev-1", question="hi", xiaozhi_host="",
        )))

    def test_posts_to_admin_take_photo_route(self):
        mock_resp = MagicMock(); mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mp:
            ok = _run(security_watch.dispatch_take_photo(
                "dev-42", question="describe",
                xiaozhi_host="10.0.0.1", xiaozhi_port=8003,
            ))
        self.assertTrue(ok)
        url = mp.call_args.args[0]
        self.assertEqual(url, "http://10.0.0.1:8003/xiaozhi/admin/take-photo")
        payload = mp.call_args.kwargs["json"]
        self.assertEqual(payload, {"device_id": "dev-42", "question": "describe"})

    def test_404_logs_one_shot_warning_and_returns_false(self):
        mock_resp = MagicMock(); mock_resp.status_code = 404; mock_resp.text = ""
        with patch("requests.post", return_value=mock_resp):
            self.assertFalse(_run(security_watch.dispatch_take_photo(
                "dev-1", question="x", xiaozhi_host="10.0.0.1",
            )))
        self.assertTrue(security_watch._PHOTO_RELAY_MISSING_LOGGED)


class DispatchCaptureAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        security_watch._AUDIO_RELAY_MISSING_LOGGED = False

    def test_404_returns_false_and_latches_warning(self):
        # This is the expected production path until the firmware adds
        # self.audio.capture_clip + the xiaozhi relay route.
        mock_resp = MagicMock(); mock_resp.status_code = 404; mock_resp.text = ""
        with patch("requests.post", return_value=mock_resp):
            self.assertFalse(_run(security_watch.dispatch_capture_audio(
                "dev-1", duration_ms=5000, xiaozhi_host="10.0.0.1",
            )))
        self.assertTrue(security_watch._AUDIO_RELAY_MISSING_LOGGED)

    def test_posts_duration_in_payload(self):
        mock_resp = MagicMock(); mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mp:
            _run(security_watch.dispatch_capture_audio(
                "dev-1", duration_ms=5000,
                xiaozhi_host="10.0.0.1", xiaozhi_port=8003,
            ))
        url = mp.call_args.args[0]
        self.assertEqual(url, "http://10.0.0.1:8003/xiaozhi/admin/capture-audio")
        self.assertEqual(mp.call_args.kwargs["json"]["duration_ms"], 5000)


class WriteSecurityRecordTests(unittest.TestCase):
    def test_appends_ndjson_line_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            record = {
                "ts": "2026-04-27T19:00:00+10:00",
                "device": "AA:BB",
                "photo_desc": "an empty room",
                "audio_transcript": None,
                "audio_classification": None,
                "errors": [],
            }
            path = security_watch.write_security_record(record, log_dir=log_dir)
            self.assertIsNotNone(path)
            line = path.read_text().strip()
            parsed = json.loads(line)
            self.assertEqual(parsed["photo_desc"], "an empty room")
            # Hard invariant: no media-byte fields in the persisted record.
            for forbidden in ("jpeg_bytes", "audio_bytes", "raw_audio", "wav_bytes"):
                self.assertNotIn(forbidden, parsed)
            # File mode 0600.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class RunCaptureCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        security_watch.RECENT_CYCLES.clear()

    def test_cycle_records_audio_pending_when_audio_dispatch_fails(self):
        # Simulate the v1 prod path: photo dispatch+poll succeed, audio
        # dispatch returns False (firmware tool not yet present).
        photo = AsyncMock(return_value=True)
        audio = AsyncMock(return_value=False)
        poll = AsyncMock(return_value="a person stands near the door")
        writes: list[dict] = []
        def writer(rec, **kw): writes.append(rec); return Path("/dev/null")

        rec = _run(security_watch._run_capture_cycle(
            "dev-1",
            photo_dispatch=photo,
            audio_dispatch=audio,
            vision_poll=poll,
            write_record=writer,
        ))
        self.assertEqual(rec["device"], "dev-1")
        self.assertEqual(rec["photo_desc"], "a person stands near the door")
        self.assertIsNone(rec["audio_transcript"])
        self.assertIn("audio_capture_pending", rec["errors"])
        self.assertEqual(len(writes), 1)
        self.assertEqual(security_watch.RECENT_CYCLES[-1]["device"], "dev-1")

    def test_cycle_records_photo_dispatch_failed_on_relay_miss(self):
        photo = AsyncMock(return_value=False)
        audio = AsyncMock(return_value=False)
        poll = AsyncMock(return_value=None)
        rec = _run(security_watch._run_capture_cycle(
            "dev-1",
            photo_dispatch=photo,
            audio_dispatch=audio,
            vision_poll=poll,
            write_record=lambda *a, **k: None,
        ))
        self.assertIn("photo_dispatch_failed", rec["errors"])
        # poll should never be called when dispatch failed
        poll.assert_not_called()


class StateGatedConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        security_watch.RECENT_CYCLES.clear()
        security_watch.stop_all_timers()

    def tearDown(self) -> None:
        security_watch.stop_all_timers()

    def test_state_changed_security_starts_timer_other_state_stops(self):
        async def scenario():
            q: asyncio.Queue[dict] = asyncio.Queue()
            cycle = AsyncMock(return_value={
                "ts": "x", "device": "dev-1", "photo_desc": "",
                "audio_transcript": None, "audio_classification": None,
                "errors": [],
            })
            consumer = asyncio.create_task(security_watch.run_security_consumer(
                lambda: q, lambda _q: None,
                interval_sec=0.05,
                cycle_runner=cycle,
            ))
            await q.put({
                "name": "state_changed", "device_id": "dev-1",
                "ts": 0.0, "data": {"state": "security"},
            })
            await asyncio.sleep(0.15)
            self.assertIn("dev-1", security_watch._DEVICE_TIMERS)
            self.assertGreaterEqual(cycle.call_count, 1)
            await q.put({
                "name": "state_changed", "device_id": "dev-1",
                "ts": 1.0, "data": {"state": "idle"},
            })
            await asyncio.sleep(0.05)
            t = security_watch._DEVICE_TIMERS.get("dev-1")
            # stop_device_timer pops it on cancellation
            self.assertIsNone(t)
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
        _run(scenario())

    def test_non_state_events_are_ignored(self):
        async def scenario():
            q: asyncio.Queue[dict] = asyncio.Queue()
            cycle = AsyncMock()
            consumer = asyncio.create_task(security_watch.run_security_consumer(
                lambda: q, lambda _q: None, cycle_runner=cycle,
            ))
            await q.put({"name": "face_detected", "device_id": "dev-1"})
            await q.put({"name": "sound_event", "device_id": "dev-1"})
            await asyncio.sleep(0.05)
            self.assertNotIn("dev-1", security_watch._DEVICE_TIMERS)
            cycle.assert_not_called()
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
        _run(scenario())


class RingBufferTests(unittest.TestCase):
    def test_ring_buffer_caps_and_returns_newest_first(self):
        security_watch.RECENT_CYCLES.clear()
        # Fill past capacity to exercise the deque maxlen.
        for i in range(security_watch.RING_BUFFER_SIZE + 5):
            security_watch.RECENT_CYCLES.append({"i": i})
        recent = security_watch.get_recent_cycles(limit=3)
        self.assertEqual(len(recent), 3)
        # Newest first
        self.assertGreater(recent[0]["i"], recent[-1]["i"])
        self.assertLessEqual(
            len(security_watch.RECENT_CYCLES),
            security_watch.RING_BUFFER_SIZE,
        )


if __name__ == "__main__":
    unittest.main()
