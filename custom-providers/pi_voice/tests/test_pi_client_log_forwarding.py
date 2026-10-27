"""PiClient's INFO lines must reach xiaozhi-server's log.

AI-assisted (Claude Opus 5.5). PiClient logs through stdlib ``logging``;
xiaozhi-server prints only its loguru logger and leaves the stdlib root at
WARNING, so ``PiClient: tool call name=...`` (the A/V runner's evidence that
the agent chose a tool) was silently dropped on the robot, 2026-10-20.
"""
from __future__ import annotations

import logging
import os
import sys
import unittest

PROVIDER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PROVIDER_DIR))
sys.path.insert(0, os.path.dirname(os.path.dirname(PROVIDER_DIR)))

from pi_voice import pi_client, pi_voice  # noqa: E402


class _LoguruLike:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.lines.append(("info", message))

    def warning(self, message: str) -> None:
        self.lines.append(("warning", message))


class PiClientLogForwardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client_logger = pi_client.logger
        self.saved = (list(self.client_logger.handlers), self.client_logger.level,
                      self.client_logger.propagate)
        self.client_logger.handlers = [
            h for h in self.client_logger.handlers
            if not isinstance(h, pi_voice._ForwardToProviderLog)]

    def tearDown(self) -> None:
        handlers, level, propagate = self.saved
        self.client_logger.handlers = handlers
        self.client_logger.setLevel(level)
        self.client_logger.propagate = propagate

    def test_tool_call_line_reaches_the_provider_logger(self) -> None:
        target = _LoguruLike()
        pi_voice._forward_pi_client_logs(target)
        self.client_logger.info("PiClient: tool call name=%s id=%s", "remember_person", "c1")
        self.client_logger.warning("PiClient: could not disable auto-compaction")
        self.client_logger.debug("PiClient: unhandled frame type=%r", "x")
        self.assertEqual(target.lines, [
            ("info", "PiClient: tool call name=remember_person id=c1"),
            ("warning", "PiClient: could not disable auto-compaction"),
        ])

    def test_installs_once(self) -> None:
        target = _LoguruLike()
        pi_voice._forward_pi_client_logs(target)
        pi_voice._forward_pi_client_logs(target)
        self.client_logger.info("PiClient: spawning pi process")
        self.assertEqual(len(target.lines), 1)

    def test_stdlib_target_is_left_alone(self) -> None:
        before = list(self.client_logger.handlers)
        pi_voice._forward_pi_client_logs(logging.getLogger("pi_voice"))
        self.assertEqual(self.client_logger.handlers, before)


if __name__ == "__main__":
    unittest.main()
