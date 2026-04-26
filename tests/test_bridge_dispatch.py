"""Unit tests for bridge.purr_player dispatch logic.

Pure unit — no network, no real filesystem, no bridge.py import.
Verifies the dispatch contract for head_pet_started → purr audio and
the cooldown / sound-suppression invariants.

Sound-localiser dispatch tests (sound_event → head-turn) are deferred
pending extraction of _perception_sound_turner from bridge.py.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.purr_player import dispatch_purr_audio, run_purr_consumer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine on a fresh event loop and return the result."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _fake_path(*, exists: bool = True) -> MagicMock:
    p = MagicMock(spec=Path)
    p.exists.return_value = exists
    p.__str__ = lambda self: "/fake/bridge/assets/purr.opus"
    return p


# ---------------------------------------------------------------------------
# dispatch_purr_audio
# ---------------------------------------------------------------------------

class DispatchPurrAudioTests(unittest.TestCase):
    """dispatch_purr_audio sends the correct HTTP request and handles failure."""

    def test_returns_false_when_no_host(self):
        result = _run(
            dispatch_purr_audio("device-1", purr_path=_fake_path(), unraid_host="")
        )
        self.assertFalse(result)

    def test_returns_false_when_host_falls_back_to_empty_module_default(self):
        # unraid_host=None falls back to module-level _XIAOZHI_HOST which is
        # "" in test environments (UNRAID_HOST not set).
        result = _run(
            dispatch_purr_audio("device-1", purr_path=_fake_path(), unraid_host=None)
        )
        self.assertFalse(result)

    def test_returns_true_on_2xx(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            result = _run(
                dispatch_purr_audio(
                    "device-1",
                    purr_path=_fake_path(),
                    unraid_host="192.0.2.1",
                    unraid_port=8003,
                )
            )
        self.assertTrue(result)

    def test_returns_false_on_4xx(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"
        with patch("requests.post", return_value=mock_resp):
            result = _run(
                dispatch_purr_audio(
                    "device-1", purr_path=_fake_path(), unraid_host="192.0.2.1"
                )
            )
        self.assertFalse(result)

    def test_returns_false_on_5xx(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "unavailable"
        with patch("requests.post", return_value=mock_resp):
            result = _run(
                dispatch_purr_audio(
                    "device-1", purr_path=_fake_path(), unraid_host="192.0.2.1"
                )
            )
        self.assertFalse(result)

    def test_returns_false_on_network_error(self):
        with patch("requests.post", side_effect=ConnectionError("refused")):
            result = _run(
                dispatch_purr_audio(
                    "device-1", purr_path=_fake_path(), unraid_host="192.0.2.1"
                )
            )
        self.assertFalse(result)

    def test_posts_to_correct_url(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            _run(
                dispatch_purr_audio(
                    "dev-42",
                    purr_path=_fake_path(),
                    unraid_host="10.0.0.1",
                    unraid_port=8003,
                )
            )
        url = mock_post.call_args.args[0]
        self.assertEqual(url, "http://10.0.0.1:8003/xiaozhi/admin/play-asset")

    def test_posts_device_id_in_payload(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            _run(
                dispatch_purr_audio(
                    "dev-42", purr_path=_fake_path(), unraid_host="10.0.0.1"
                )
            )
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["device_id"], "dev-42")

    def test_posts_asset_path_in_payload(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            _run(
                dispatch_purr_audio(
                    "dev-42", purr_path=_fake_path(), unraid_host="10.0.0.1"
                )
            )
        payload = mock_post.call_args.kwargs["json"]
        self.assertIn("asset", payload)
        self.assertIsInstance(payload["asset"], str)


# ---------------------------------------------------------------------------
# run_purr_consumer
# ---------------------------------------------------------------------------

class PurrConsumerTests(unittest.TestCase):
    """run_purr_consumer dispatches purr on head_pet_started with cooldown."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()
        asyncio.set_event_loop(None)

    async def _drain(
        self,
        events: list,
        *,
        cooldown_sec: float = 5.0,
        duration_sec: float = 2.0,
        state: dict | None = None,
    ) -> tuple[list[str], dict]:
        dispatches: list[str] = []
        if state is None:
            state = {}
        q: asyncio.Queue = asyncio.Queue()
        for ev in events:
            q.put_nowait(ev)

        async def capture(device_id: str) -> bool:
            dispatches.append(device_id)
            return True

        task = asyncio.create_task(
            run_purr_consumer(
                lambda: q,
                state,
                cooldown_sec=cooldown_sec,
                duration_sec=duration_sec,
                dispatch_fn=capture,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return dispatches, state

    def test_head_pet_started_triggers_dispatch(self):
        dispatches, _ = self.loop.run_until_complete(
            self._drain(
                [{"name": "head_pet_started", "device_id": "dev-1", "ts": 1000.0}]
            )
        )
        self.assertEqual(dispatches, ["dev-1"])

    def test_cooldown_blocks_repeat_within_window(self):
        events = [
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1000.0},
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1003.0},
        ]
        dispatches, _ = self.loop.run_until_complete(
            self._drain(events, cooldown_sec=5.0)
        )
        self.assertEqual(len(dispatches), 1)

    def test_cooldown_allows_second_after_window(self):
        events = [
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1000.0},
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1006.0},
        ]
        dispatches, _ = self.loop.run_until_complete(
            self._drain(events, cooldown_sec=5.0)
        )
        self.assertEqual(len(dispatches), 2)

    def test_ignores_non_pet_event_types(self):
        events = [
            {"name": "sound_event", "device_id": "dev-1", "ts": 1000.0},
            {"name": "face_detected", "device_id": "dev-1", "ts": 1001.0},
            {"name": "face_lost", "device_id": "dev-1", "ts": 1002.0},
        ]
        dispatches, _ = self.loop.run_until_complete(self._drain(events))
        self.assertEqual(dispatches, [])

    def test_ignores_blank_device_id(self):
        dispatches, _ = self.loop.run_until_complete(
            self._drain(
                [{"name": "head_pet_started", "device_id": "", "ts": 1000.0}]
            )
        )
        self.assertEqual(dispatches, [])

    def test_ignores_unknown_device_id(self):
        dispatches, _ = self.loop.run_until_complete(
            self._drain(
                [{"name": "head_pet_started", "device_id": "unknown", "ts": 1000.0}]
            )
        )
        self.assertEqual(dispatches, [])

    def test_last_purr_t_recorded_in_state(self):
        _, state = self.loop.run_until_complete(
            self._drain(
                [{"name": "head_pet_started", "device_id": "dev-1", "ts": 100.0}]
            )
        )
        self.assertAlmostEqual(state["dev-1"]["last_purr_t"], 100.0)

    def test_last_chat_t_extended_for_sound_suppression(self):
        """last_chat_t must equal ts + duration_sec so the sound-localiser
        skips head-turn commands while the purr plays."""
        _, state = self.loop.run_until_complete(
            self._drain(
                [{"name": "head_pet_started", "device_id": "dev-1", "ts": 100.0}],
                duration_sec=2.0,
            )
        )
        self.assertAlmostEqual(state["dev-1"]["last_chat_t"], 102.0, delta=0.01)

    def test_separate_devices_have_independent_cooldowns(self):
        events = [
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1000.0},
            {"name": "head_pet_started", "device_id": "dev-2", "ts": 1001.0},
        ]
        dispatches, _ = self.loop.run_until_complete(
            self._drain(events, cooldown_sec=5.0)
        )
        self.assertCountEqual(dispatches, ["dev-1", "dev-2"])

    def test_zero_cooldown_allows_immediate_repeat(self):
        events = [
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1000.0},
            {"name": "head_pet_started", "device_id": "dev-1", "ts": 1000.1},
        ]
        dispatches, _ = self.loop.run_until_complete(
            self._drain(events, cooldown_sec=0.0)
        )
        self.assertEqual(len(dispatches), 2)


if __name__ == "__main__":
    unittest.main()
