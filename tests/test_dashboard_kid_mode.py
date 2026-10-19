"""Kid Mode card follows the persisted guardian policy (#169).

The card, PiVoiceLLM, the camera gate and dotty-behaviour all read the same
shared kid-mode state file. The card must show what that file says on every
poll (not a value cached at bridge start), say when no policy is saved yet,
and not report a saved flip as a failure just because the robot's WS was
closed when the LED pip push went out.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-kid-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))

_repo_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bridge_app", _repo_root / "bridge.py")
assert _spec is not None and _spec.loader is not None
bridge_app = importlib.util.module_from_spec(_spec)
sys.modules["bridge_app"] = bridge_app
_spec.loader.exec_module(bridge_app)


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


bridge_app.app.router.lifespan_context = _noop_lifespan

import bridge.dashboard as dash  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class KidModeCardTests(unittest.TestCase):

    def setUp(self):
        saved = dict(dash._state)
        self.addCleanup(lambda: dash._state.update(saved))
        tmp = Path(tempfile.mkdtemp(prefix="dotty-kid-"))
        self.kid_file = tmp / "kid-mode"
        self.smart_file = tmp / "smart-mode"
        for name, value in (("_KID_STATE_FILE", self.kid_file),
                            ("_SMART_STATE_FILE", self.smart_file)):
            original = getattr(bridge_app, name)
            self.addCleanup(lambda n=name, o=original: setattr(bridge_app, n, o))
            setattr(bridge_app, name, value)
        original_kid = bridge_app.KID_MODE
        self.addCleanup(lambda: bridge_app._apply_kid_mode(original_kid))
        # Pin the shared dashboard module to this copy of bridge.py.
        dash._state["kid_mode_getter"] = bridge_app._dashboard_kid_mode_getter
        dash._state["kid_mode_saved_getter"] = bridge_app._kid_mode_saved
        dash._state["smart_mode_getter"] = bridge_app._read_smart_mode
        self.client = TestClient(bridge_app.app)

    def card(self) -> str:
        r = self.client.get("/ui/kid-mode")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_card_follows_file_written_elsewhere(self):
        self.kid_file.write_text("false")
        self.assertIn('data-kid-mode="off"', self.card())
        self.kid_file.write_text("true")
        html = self.card()
        self.assertIn('data-kid-mode="on"', html)
        self.assertIn('data-kid-mode-saved="yes"', html)
        # The bridge's kid-derived globals follow the file too.
        self.assertTrue(bridge_app.KID_MODE)

    def test_missing_policy_is_flagged_as_default(self):
        html = self.card()
        self.assertIn('data-kid-mode-saved="no"', html)
        self.assertIn("default, not saved", html)

    def test_invalid_policy_is_not_treated_as_saved(self):
        self.kid_file.write_text("maybe")
        self.assertIsNone(bridge_app._kid_mode_saved())
        self.assertIn('data-kid-mode-saved="no"', self.card())

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        bridge_app._write_kid_mode(False)
        self.assertEqual(self.kid_file.read_text(), "false")
        self.assertEqual(sorted(p.name for p in self.kid_file.parent.iterdir()),
                         ["kid-mode"])

    def test_smart_mode_card_follows_file(self):
        self.smart_file.write_text("true")
        r = self.client.get("/ui/smart-mode")
        self.assertIn('data-smart-mode="on"', r.text)


class ToggleSetterTests(unittest.TestCase):
    """A saved flip is in force even when the LED pip push finds no WS."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="dotty-kid-set-"))
        self.kid_file = tmp / "kid-mode"
        original_file = bridge_app._KID_STATE_FILE
        self.addCleanup(lambda: setattr(bridge_app, "_KID_STATE_FILE", original_file))
        bridge_app._KID_STATE_FILE = self.kid_file
        original_kid = bridge_app.KID_MODE
        self.addCleanup(lambda: bridge_app._apply_kid_mode(original_kid))
        original_dispatch = bridge_app._dispatch_set_toggle
        self.addCleanup(lambda: setattr(bridge_app, "_dispatch_set_toggle", original_dispatch))
        self.pushed = False

        async def _fake_dispatch(device_id, name, enabled):
            return self.pushed

        bridge_app._dispatch_set_toggle = _fake_dispatch

    def test_unacknowledged_pip_is_ok_with_note(self):
        result = asyncio.run(bridge_app._dashboard_set_kid_mode(False))
        self.assertTrue(result["ok"])
        self.assertFalse(result["device_pushed"])
        self.assertIn("next connects", result["note"])
        self.assertEqual(self.kid_file.read_text(), "false")
        self.assertFalse(bridge_app.KID_MODE)

    def test_acknowledged_pip_has_no_note(self):
        self.pushed = True
        result = asyncio.run(bridge_app._dashboard_set_kid_mode(True))
        self.assertEqual(result, {"ok": True, "device_pushed": True})


class StartupResyncTests(unittest.TestCase):
    """#21: a bridge restart pushes the persisted toggles back to the robot."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="dotty-resync-"))
        for name, fname, value in (("_KID_STATE_FILE", "kid-mode", "false"),
                                   ("_SMART_STATE_FILE", "smart-mode", "true")):
            original = getattr(bridge_app, name)
            self.addCleanup(lambda n=name, o=original: setattr(bridge_app, n, o))
            path = tmp / fname
            path.write_text(value)
            setattr(bridge_app, name, path)
        for name in ("_dispatch_set_toggle", "_XIAOZHI_HOST"):
            original = getattr(bridge_app, name)
            self.addCleanup(lambda n=name, o=original: setattr(bridge_app, n, o))
        self.calls: list[tuple[str, bool]] = []
        self.ack = True

        async def _fake_dispatch(device_id, name, enabled):
            self.calls.append((name, enabled))
            return self.ack

        bridge_app._dispatch_set_toggle = _fake_dispatch
        bridge_app._XIAOZHI_HOST = "xiaozhi.test"

    def test_pushes_persisted_toggles(self):
        result = asyncio.run(bridge_app._startup_resync())
        self.assertEqual(self.calls, [("kid_mode", False), ("smart_mode", True)])
        self.assertEqual(result, {"kid_mode": False, "smart_mode": True, "pushed": True})

    def test_robot_offline_is_not_an_error(self):
        self.ack = False
        result = asyncio.run(bridge_app._startup_resync())
        self.assertFalse(result["pushed"])
        self.assertEqual(len(self.calls), 2)

    def test_no_xiaozhi_host_skips_push(self):
        bridge_app._XIAOZHI_HOST = ""
        result = asyncio.run(bridge_app._startup_resync())
        self.assertEqual(self.calls, [])
        self.assertFalse(result["pushed"])


if __name__ == "__main__":
    unittest.main()
