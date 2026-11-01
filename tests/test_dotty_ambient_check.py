"""Ambient check against a simulated behaviour service. AI-assisted: Claude (Opus 5.5)."""
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("dotty_ambient_check", SCRIPTS / "dotty_ambient_check.py")
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)
from . import test_dotty_mcp_check as mcp_fixture  # noqa: E402


class Host(mcp_fixture.FakeRemote):
    def __init__(self, robot, consumer_on=True, state="idle", moves_head=True):
        super().__init__(robot)
        self.consumer_on, self.state, self.moves_head = consumer_on, state, moves_head
        self.events, self.face = [], True

    def run(self, argv, input=None, check=True, timeout=None):
        if argv[0] == "curl" and "-d" in argv:
            body = argv[argv.index("-d") + 1]
            try:
                event = json.loads(body)
            except ValueError:
                return types.SimpleNamespace(stdout="422", returncode=0)
            if "name" not in event:
                return types.SimpleNamespace(stdout="422", returncode=0)
            if event["name"] == "sound_event" and self.consumer_on and self.state == "idle" \
                    and event["data"].get("direction") in ("left", "right"):
                self.events.append({"name": "head_turn", "ts": 9e12})
                if self.moves_head:
                    self.robot.yaw = -45 if event["data"]["direction"] == "left" else 45
            if event["name"] == "face_lost":
                self.face = False
                self.events.append({"name": "face_lost", "ts": 9e12})
            return types.SimpleNamespace(stdout="204", returncode=0)
        return super().run(argv, input, check, timeout)

    def get_json(self, url):
        if "/recent/" in url:
            return self.events
        return {"robot": {"current_state": self.state, "listening": False, "face_present": self.face}}

    def http_status(self, url):
        return 200


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    monkeypatch.setattr(mcp_fixture.mc.time, "sleep", lambda s: None)


def statuses(host):
    host.face = False
    return [r["status"] for r in ac.run_checks(host, "robot").rows]


def test_enabled_consumer_moves_head_and_head_is_returned_home():
    robot = mcp_fixture.FakeRobot()
    assert statuses(Host(robot)) == ["PASS", "PASS", "PASS", "PASS"]
    assert robot.yaw == 0


def test_disabled_or_gated_consumer_is_skipped_never_passed():
    assert statuses(Host(mcp_fixture.FakeRobot(), consumer_on=False))[:2] == ["SKIP", "SKIP"]
    assert statuses(Host(mcp_fixture.FakeRobot(), state="sleep"))[:2] == ["SKIP", "SKIP"]


def test_head_that_does_not_follow_the_event_fails():
    assert statuses(Host(mcp_fixture.FakeRobot(), moves_head=False))[0] == "FAIL"
