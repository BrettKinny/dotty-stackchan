"""Unit tests for `bridge.rich_mcp.RichMCPToolSurface`.

Pure unit tests — no network, no WS, no filesystem. The
``ws_send_func`` dependency is replaced with ``unittest.mock.AsyncMock``.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

# Allow running this file directly: ``python tests/test_rich_mcp.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.rich_mcp import (  # noqa: E402
    RichMCPToolSurface,
    _KID_MODE_FILTERED,
    _TOOL_DEFINITIONS,
)


# All 14 firmware tools the surface must know about.
EXPECTED_TOOLS = {
    "get_device_status",
    "audio_speaker.set_volume",
    "screen.set_brightness",
    "screen.set_theme",
    "camera.take_photo",
    "robot.get_head_angles",
    "robot.set_head_angles",
    "robot.set_led_color",
    "robot.set_led_multi",
    "robot.get_privacy_state",
    "robot.create_reminder",
    "robot.get_reminders",
    "robot.stop_reminder",
    "robot.face_unlock",
    "robot.face_enroll",
    "robot.face_forget",
    "robot.face_list",
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class CatalogueTests(unittest.TestCase):
    """The static tool catalogue meets the contract."""

    def test_all_expected_tools_present(self):
        # Tasks.md says 14 firmware tools; we ship every one plus the
        # face_* writes that landed late in the same session.
        self.assertEqual(set(_TOOL_DEFINITIONS.keys()), EXPECTED_TOOLS)
        self.assertEqual(len(_TOOL_DEFINITIONS), 17)

    def test_tool_definitions_have_valid_shape(self):
        for name, defn in _TOOL_DEFINITIONS.items():
            with self.subTest(tool=name):
                self.assertIn("description", defn)
                self.assertIsInstance(defn["description"], str)
                self.assertGreater(len(defn["description"]), 10)
                self.assertIn("parameters", defn)
                params = defn["parameters"]
                self.assertEqual(params.get("type"), "object")
                self.assertIn("properties", params)
                self.assertIsInstance(params["properties"], dict)


class ToolsForLLMTests(unittest.TestCase):
    """`tools_for_llm` honours kid-mode filtering."""

    def test_includes_all_when_kid_mode_false(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        tools = surface.tools_for_llm()
        names = {t["name"] for t in tools}
        self.assertEqual(names, EXPECTED_TOOLS)
        self.assertEqual(len(tools), len(_TOOL_DEFINITIONS))

    def test_excludes_filtered_when_kid_mode_true(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: True)
        tools = surface.tools_for_llm()
        names = {t["name"] for t in tools}
        for filtered in _KID_MODE_FILTERED:
            self.assertNotIn(filtered, names, f"{filtered} must be hidden in kid-mode")
        # The remaining count is the catalogue minus the filter set.
        self.assertEqual(len(tools), len(_TOOL_DEFINITIONS) - len(_KID_MODE_FILTERED))
        # Specific kid-mode-allowed tools are still present.
        self.assertIn("robot.set_head_angles", names)
        self.assertIn("robot.set_led_color", names)
        self.assertIn("audio_speaker.set_volume", names)
        self.assertIn("robot.face_list", names)  # read-only is fine

    def test_kid_mode_provider_raising_defaults_to_kid_mode(self):
        """Defensive: a faulty provider must fail safe (more filtering)."""
        def boom() -> bool:
            raise RuntimeError("nope")

        surface = RichMCPToolSurface(kid_mode_provider=boom)
        tools = surface.tools_for_llm()
        names = {t["name"] for t in tools}
        # Camera + face writes hidden because we defaulted to kid_mode=True.
        self.assertNotIn("camera.take_photo", names)
        self.assertNotIn("robot.face_enroll", names)

    def test_each_tool_definition_serialises(self):
        """The OpenAI/Anthropic shape requires name + description + parameters."""
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        for tool in surface.tools_for_llm():
            with self.subTest(name=tool["name"]):
                self.assertIn("name", tool)
                self.assertIn("description", tool)
                self.assertIn("parameters", tool)
                self.assertEqual(tool["parameters"]["type"], "object")


class DispatchTests(unittest.TestCase):
    """`dispatch` enforces the allowlist + isolates tool failures."""

    def setUp(self) -> None:
        # Each test gets its own loop so AsyncMock works cleanly.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_rejects_tool_not_in_allowlist(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        send = AsyncMock(return_value=None)
        out = self.loop.run_until_complete(
            surface.dispatch("robot.delete_everything", {}, send)
        )
        self.assertEqual(out["error"], "tool not in allowlist")
        self.assertEqual(out["tool"], "robot.delete_everything")
        send.assert_not_called()

    def test_rejects_kid_mode_filtered_tool(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: True)
        send = AsyncMock(return_value=None)
        out = self.loop.run_until_complete(
            surface.dispatch("camera.take_photo", {"question": "hi"}, send)
        )
        self.assertEqual(out["error"], "tool not in allowlist")
        self.assertEqual(out.get("reason"), "kid_mode_filter")
        send.assert_not_called()

    def test_rejects_non_dict_arguments(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        send = AsyncMock(return_value=None)
        out = self.loop.run_until_complete(
            surface.dispatch("get_device_status", "not a dict", send)
        )
        self.assertIn("error", out)
        send.assert_not_called()

    def test_returns_send_result_on_success(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        sentinel = {"frame": "sent"}
        send = AsyncMock(return_value=sentinel)
        out = self.loop.run_until_complete(
            surface.dispatch(
                "robot.set_led_color",
                {"red": 168, "green": 0, "blue": 0},
                send,
            )
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["tool"], "robot.set_led_color")
        self.assertIs(out["result"], sentinel)
        send.assert_awaited_once_with(
            "robot.set_led_color",
            {"red": 168, "green": 0, "blue": 0},
        )

    def test_returns_error_on_send_raising(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        send = AsyncMock(side_effect=RuntimeError("ws closed"))
        out = self.loop.run_until_complete(
            surface.dispatch(
                "robot.set_head_angles",
                {"yaw": 0, "pitch": 0, "speed": 150},
                send,
            )
        )
        self.assertEqual(out["error"], "dispatch failed")
        self.assertIn("ws closed", out["exception"])
        # Critically, no exception escaped — that would break the LLM turn.

    def test_kid_mode_filtered_tool_still_dispatches_in_adult_mode(self):
        surface = RichMCPToolSurface(kid_mode_provider=lambda: False)
        send = AsyncMock(return_value=None)
        out = self.loop.run_until_complete(
            surface.dispatch("camera.take_photo", {"question": "what is this"}, send)
        )
        self.assertTrue(out["ok"])
        send.assert_awaited_once()


class AvailableToolNamesTests(unittest.TestCase):
    def test_returns_full_catalogue_regardless_of_mode(self):
        kid = RichMCPToolSurface(kid_mode_provider=lambda: True)
        adult = RichMCPToolSurface(kid_mode_provider=lambda: False)
        self.assertEqual(kid.available_tool_names(), EXPECTED_TOOLS)
        self.assertEqual(adult.available_tool_names(), EXPECTED_TOOLS)


if __name__ == "__main__":
    unittest.main()
