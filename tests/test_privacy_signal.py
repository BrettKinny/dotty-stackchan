"""Unit tests for `bridge.privacy_signal`.

Verifies the upload-pulse helpers correctly emit `start` / `end` signals
through the installed sender, including the `finally` semantics around
the camera context manager (an exception inside the `async with` block
must NOT swallow the `end` signal — otherwise a vision-API failure
would leave the firmware LED pulsing for the full 2 s failsafe).
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

# Ensure the repo root is importable when running this file directly.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import privacy_signal  # noqa: E402


class TestPrivacySignal(unittest.TestCase):
    def setUp(self) -> None:
        self._calls: list[tuple[str, str]] = []

        async def _capture(kind: str, phase: str) -> None:
            self._calls.append((kind, phase))

        privacy_signal.set_privacy_sender(_capture)

    def tearDown(self) -> None:
        privacy_signal.set_privacy_sender(None)

    def test_camera_upload_pulse_emits_start_then_end(self) -> None:
        async def _run() -> None:
            async with privacy_signal.camera_upload_pulse():
                # Mid-block: start must already have fired.
                self.assertEqual(self._calls, [("camera", "start")])
            self.assertEqual(
                self._calls,
                [("camera", "start"), ("camera", "end")],
            )

        asyncio.run(_run())

    def test_camera_upload_pulse_end_fires_on_exception(self) -> None:
        """If the wrapped block raises, the `end` signal MUST still fire.
        Otherwise a vision-API failure leaves the firmware LED pulsing
        for the full 2 s failsafe before self-healing."""
        async def _run() -> None:
            with self.assertRaises(RuntimeError):
                async with privacy_signal.camera_upload_pulse():
                    raise RuntimeError("vision API blew up")
            self.assertEqual(
                self._calls,
                [("camera", "start"), ("camera", "end")],
            )

        asyncio.run(_run())

    def test_signal_camera_upload_rejects_bad_phase(self) -> None:
        async def _run() -> None:
            await privacy_signal.signal_camera_upload("garbage")
            # The bad phase must NOT reach the sender — no transport call.
            self.assertEqual(self._calls, [])

        asyncio.run(_run())

    def test_no_sender_is_noop(self) -> None:
        """When no sender is installed, signals should silently no-op."""
        privacy_signal.set_privacy_sender(None)

        async def _run() -> None:
            await privacy_signal.signal_camera_upload("start")
            await privacy_signal.signal_mic_upload("end")
            # No assertion — we just need it not to raise.

        asyncio.run(_run())

    def test_sender_exception_is_swallowed(self) -> None:
        """Sender bugs must NEVER propagate up — the firmware self-heals
        and the bridge has no business crashing because the LED hint
        failed."""
        crashing = AsyncMock(side_effect=RuntimeError("network down"))
        privacy_signal.set_privacy_sender(crashing)

        async def _run() -> None:
            # If this raises, the test fails. NEVER raises is the contract.
            await privacy_signal.signal_camera_upload("start")
            crashing.assert_awaited_once_with("camera", "start")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
