"""Live-device selection and the robot "sensors stale" check (#169).

dotty-behaviour's per-device state also holds ids no robot owns (the
`automation-persistence-probe` left by the #173 acceptance run, the
`unknown` fallback id, devices restored from disk). Those must not decide
the State card or turn the robot status dot amber.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-devices-state-"))
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
from bridge.devices import (  # noqa: E402
    is_probe_device,
    pick_live_device,
    sensor_health,
)
from fastapi.testclient import TestClient  # noqa: E402

ROBOT = "aa:bb:cc:dd:ee:ff"
PROBE = "automation-persistence-probe"

# Shape of the live tower payload behind the bogus "1/2 dev" warning: the
# robot streaming normally, the leftover probe idle for hours.
LIVE_WITH_PROBE = {
    ROBOT: {
        "current_state": "security", "last_state_change_t": 9_000.0,
        "last_event_t": 9_990.0, "sensor_age_s": 10.0,
        "sensor_stale": False, "sensor_seen": True,
    },
    PROBE: {
        "current_state": "idle", "last_state_change_t": 1_000.0,
        "last_event_t": 1_000.0, "sensor_age_s": 9_000.0,
        "sensor_stale": True, "sensor_seen": True,
    },
}


class ProbeIdTests(unittest.TestCase):

    def test_probe_and_fallback_ids(self):
        for did in (PROBE, "test-dev", "smoke_1", "unknown", ""):
            self.assertTrue(is_probe_device(did), did)
        for did in (ROBOT, "dev-1", "robot"):
            self.assertFalse(is_probe_device(did), did)


class PickLiveDeviceTests(unittest.TestCase):

    def test_probe_listed_first_is_not_picked(self):
        pstate = {PROBE: LIVE_WITH_PROBE[PROBE], ROBOT: LIVE_WITH_PROBE[ROBOT]}
        self.assertEqual(pick_live_device(pstate), ROBOT)

    def test_most_recently_active_real_device_wins(self):
        pstate = {
            "old": {"last_state_change_t": 10.0},
            "new": {"last_event_t": 20.0},
        }
        self.assertEqual(pick_live_device(pstate), "new")

    def test_connected_device_beats_newer_activity(self):
        pstate = {"a": {"last_event_t": 10.0}, "b": {"last_event_t": 20.0}}
        self.assertEqual(pick_live_device(pstate, connected=["a"]), "a")

    def test_only_probes_means_no_device(self):
        self.assertIsNone(pick_live_device({PROBE: {}, "unknown": {}}))
        self.assertIsNone(pick_live_device({}))
        self.assertIsNone(pick_live_device("garbage"))  # type: ignore[arg-type]


class SensorHealthTests(unittest.TestCase):

    def test_probe_is_not_judged(self):
        judged, stale, fresh = sensor_health(LIVE_WITH_PROBE, connected=[ROBOT])
        self.assertEqual((judged, stale, fresh), ([ROBOT], [], [ROBOT]))

    def test_restored_never_seen_device_is_not_judged(self):
        pstate = {ROBOT: {"current_state": "sleep", "sensor_age_s": float("inf"),
                          "sensor_stale": True}}
        self.assertEqual(sensor_health(pstate, connected=None), ([], [], []))

    def test_disconnected_robot_is_offline_not_hung(self):
        pstate = {ROBOT: {**LIVE_WITH_PROBE[ROBOT], "sensor_stale": True}}
        self.assertEqual(sensor_health(pstate, connected=[]), ([], [], []))

    def test_connected_stale_robot_is_flagged(self):
        pstate = {ROBOT: {**LIVE_WITH_PROBE[ROBOT], "sensor_stale": True,
                          "sensor_age_s": 600.0}}
        self.assertEqual(sensor_health(pstate, connected=[ROBOT]),
                         ([ROBOT], [ROBOT], []))

    def test_unknown_connection_list_still_judges_seen_devices(self):
        pstate = {ROBOT: {**LIVE_WITH_PROBE[ROBOT], "sensor_stale": True}}
        self.assertEqual(sensor_health(pstate, connected=None)[1], [ROBOT])


class _DashTest(unittest.TestCase):

    def setUp(self):
        saved = dict(dash._state)
        self.addCleanup(lambda: dash._state.update(saved))
        original = dash._xiaozhi_connected_devices
        self.addCleanup(lambda: setattr(dash, "_xiaozhi_connected_devices", original))
        self.connected: list[str] | None = [ROBOT]

        async def _fake_connected():
            return self.connected

        dash._xiaozhi_connected_devices = _fake_connected
        self.pstate = dict(LIVE_WITH_PROBE)
        dash._state["perception_state_getter"] = lambda: self.pstate
        self.client = TestClient(bridge_app.app)

    def robot_dot(self) -> str:
        html = self.client.get("/ui/status-strip?placement=header").text
        start = html.index('hx-get="/ui/host/robot"')
        return html[max(0, start - 400):start + 400]


class StatusStripTests(_DashTest):

    def test_leftover_probe_does_not_turn_robot_dot_amber(self):
        dot = self.robot_dot()
        self.assertNotIn("sensors stale", dot)
        self.assertIn("status-ok", dot)

    def test_connected_stale_robot_still_warns(self):
        self.pstate = {ROBOT: {**LIVE_WITH_PROBE[ROBOT], "sensor_stale": True}}
        dot = self.robot_dot()
        self.assertIn("perception sensors stale (1/1 dev)", dot)
        self.assertIn("status-warn", dot)

    def test_robot_modal_ignores_probe(self):
        html = self.client.get("/ui/host/robot").text
        self.assertIn("live (", html)
        self.assertNotIn("stale", html)

    def test_robot_modal_offline_robot_is_not_stale(self):
        self.connected = []
        html = self.client.get("/ui/host/robot").text
        self.assertIn("not streaming", html)


class StateCardTests(_DashTest):

    def setUp(self):
        super().setUp()
        original = bridge_app._dashboard_perception_state_getter
        self.addCleanup(lambda: setattr(
            bridge_app, "_dashboard_perception_state_getter", original))
        bridge_app._dashboard_perception_state_getter = lambda: {
            PROBE: LIVE_WITH_PROBE[PROBE], ROBOT: LIVE_WITH_PROBE[ROBOT],
        }
        dash._state["state_getter"] = bridge_app._dashboard_state_getter

    def test_state_card_follows_robot_not_probe(self):
        self.assertEqual(bridge_app._dashboard_state_getter(), "security")
        self.assertIn('data-state="security"', self.client.get("/ui/state").text)


if __name__ == "__main__":
    unittest.main()
