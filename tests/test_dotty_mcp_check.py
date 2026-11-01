"""MCP coverage check against a simulated robot. AI-assisted: Claude (Opus 5.5).

No SSH, robot or audio: a fake Docker host answers mcp-call like the firmware
and logs result frames in xiaozhi's format.
"""
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("dotty_mcp_check", SCRIPTS / "dotty_mcp_check.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)

SESSION = "sess-1"


def frame(request_id, text, error=False):
    return "2026-11-01T10:00:00Z x[core]-INFO-收到mcp消息：" + json.dumps({
        "session_id": SESSION, "type": "mcp",
        "payload": {"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": error}}})


class FakeRobot:
    def __init__(self, tools=None, refuse_theme=True, drop_reminder_stop=False,
                 create_reply="json", fail_next_list=False):
        self.create_reply, self.fail_next_list = create_reply, fail_next_list
        self.volume, self.brightness, self.theme, self.yaw = 40, 60, "dark", 0
        self.reminders, self.next_id, self.request, self.lines = {}, 7, 0, []
        self.tools = tools if tools is not None else sorted(mc.EXPECTED_TOOLS)
        self.refuse_theme, self.drop_stop = refuse_theme, drop_reminder_stop
        self.lines.append("x-INFO-收到mcp消息：" + json.dumps({"session_id": SESSION, "payload": {
            "id": 2, "result": {"tools": [{"name": n} for n in self.tools]}}}))

    def reply(self, tool, args):
        if tool == "self.get_device_status":
            return json.dumps({"audio_speaker": {"volume": self.volume},
                               "screen": {"brightness": self.brightness, "theme": self.theme},
                               "network": {"ssid": "SECRET-NET"}}), False
        if tool == "self.audio_speaker.set_volume":
            self.volume = args["volume"]
        elif tool == "self.screen.set_brightness":
            self.brightness = args["brightness"]
        elif tool == "self.screen.set_theme":
            if args["theme"] not in ("dark", "light") and self.refuse_theme:
                return "false", True
            self.theme = args["theme"]
        elif tool == "self.robot.get_head_angles":
            return json.dumps({"yaw": self.yaw, "pitch": 0}), False
        elif tool == "self.robot.set_led_multi" and args["index"] > 5:
            return "false", True
        elif tool == "self.robot.create_reminder":
            self.reminders[self.next_id] = args["message"]
            self.next_id += 1
            if self.create_reply == "prose":  # 2026-11-01: the real reply had no parseable id
                return "Reminder created", False
            return json.dumps({"id": self.next_id - 1}), False
        elif tool == "self.robot.get_reminders":
            return json.dumps([{"id": i, "message": m} for i, m in self.reminders.items()]), False
        elif tool == "self.robot.stop_reminder" and not self.drop_stop:
            self.reminders.pop(args["id"], None)
        return "true", False


class FakeRemote:
    def __init__(self, robot):
        self.robot = robot

    def admin(self, method, route, body=None, with_token=True):
        if route == "set-head-angles":
            self.robot.yaw = body["yaw"]
            return {"status": 200, "body": {"ok": True, **body}, "token_configured": True}
        if body["tool"] == "self.robot.get_reminders" and self.robot.fail_next_list and self.robot.reminders:
            self.robot.fail_next_list = False
            raise RuntimeError("connection lost mid-check")
        self.robot.request += 1
        text, error = self.robot.reply(body["tool"], body["arguments"])
        self.robot.lines.append(frame(self.robot.request, text, error))
        return {"status": 200, "token_configured": True, "body": {
            "ok": True, "request_id": self.robot.request, "session_id": SESSION}}

    def run(self, argv, input=None, check=True, timeout=None):
        return types.SimpleNamespace(stdout="\n".join(self.robot.lines), returncode=0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)


def rows(robot, **kw):
    checks = mc.run_checks(FakeRemote(robot), "robot", **kw)
    return {r["check"].split()[0]: r for r in checks.rows}, checks


def test_healthy_robot_passes_and_everything_is_restored():
    robot = FakeRobot()
    by, checks = rows(robot)
    assert not checks.failed, checks.table()
    assert [r["status"] for r in checks.rows].count("PASS") == len(checks.rows)
    assert (robot.volume, robot.brightness, robot.theme, robot.yaw) == (40, 60, "dark", 0)
    assert robot.reminders == {}  # created ahead of its due time, then stopped
    assert "SECRET-NET" not in checks.table()  # field names only, never values


def test_missing_tool_and_accepted_invalid_input_fail():
    robot = FakeRobot(tools=sorted(mc.EXPECTED_TOOLS - {"self.robot.stop_reminder"}), refuse_theme=False)
    by, checks = rows(robot)
    assert by["tools/list"]["status"] == "FAIL" and "stop_reminder" in by["tools/list"]["detail"]
    assert by["M04"]["status"] == "FAIL"


def test_unstoppable_reminder_is_a_failure():
    by, _ = rows(FakeRobot(drop_reminder_stop=True))
    assert by["M13/M14/M15"]["status"] == "FAIL"


def test_unanswered_call_is_reported_not_assumed(monkeypatch):
    robot = FakeRobot()
    remote = FakeRemote(robot)
    original = remote.admin
    remote.admin = lambda *a, **k: {**original(*a, **k), "body": {"ok": True, "request_id": 999, "session_id": SESSION}}
    ticks = iter(range(0, 10000, 5))
    monkeypatch.setattr(mc.time, "monotonic", lambda: next(ticks))
    checks = mc.run_checks(remote, "robot")
    assert checks.rows[-1]["status"] == "FAIL" and "no device reply" in checks.rows[-1]["detail"]


def test_parsers_tolerate_noise():
    assert mc.parse_mcp_frames("noise\nx收到mcp消息：{not json\n") == []
    assert mc.reminder_ids('[{"id": 3}, {"id": 4}]') == {3, 4}
    assert mc.reminder_ids("reminder id: 12") == {12}


def test_reminder_is_stopped_even_when_the_create_reply_has_no_id():
    # 2026-11-01 22:26: the id was not parsed, stop was never sent, and the
    # popup covered the face for two hours.
    robot = FakeRobot(create_reply="prose")
    by, checks = rows(robot)
    assert robot.reminders == {}
    assert by["M13/M14/M15"]["status"] == "PASS", checks.table()


def test_reminder_is_stopped_when_the_check_dies_partway():
    robot = FakeRobot(fail_next_list=True)
    remote = FakeRemote(robot)
    by = {r["check"].split()[0]: r for r in mc.run_checks(remote, "robot").rows}
    assert robot.reminders == {}  # the sweep ran from the finally block
    assert by["M13/M14/M15"]["status"] == "FAIL"


def test_a_reminder_that_cannot_be_stopped_is_reported_loudly():
    by, _ = rows(FakeRobot(drop_reminder_stop=True))
    row = by["M13/M14/M15"]
    assert row["status"] == "FAIL" and "dismiss it by hand" in row["detail"]
