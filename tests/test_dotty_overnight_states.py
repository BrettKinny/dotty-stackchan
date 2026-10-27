"""State-case escape and evidence tests. AI-assisted: Claude (Opus 5.5).

Synthetic perception events only; no SSH, robot, audio or camera.
"""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("dotty_overnight_states", SCRIPTS / "dotty_overnight.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def plenty_of_disk(monkeypatch):
    # CI runners can have less free disk than space_ok's 10 GiB floor.
    monkeypatch.setattr(runner, "space_ok", lambda session: True)

CATALOGUE = {c["id"]: c for c in json.loads((SCRIPTS / "dotty-av-cases.json").read_text())["cases"]}
CLOCK = {"offset_seconds": 0, "uncertainty_seconds": 0}
CONFIG = {"host": "unused", "device": "robot"}


class FakeRobot:
    """Echoes admin set-state as firmware state_changed events, unless deaf."""

    def __init__(self, monkeypatch, state="idle", deaf=False):
        self.state, self.deaf, self.events, self.calls = state, deaf, [], []
        self.t = 1000.0
        monkeypatch.setattr(runner.time, "time", lambda: self.t)
        monkeypatch.setattr(runner.time, "sleep", self.sleep)
        monkeypatch.setattr(runner, "admin", self.admin)
        monkeypatch.setattr(runner, "perception_recent", lambda host, device: list(reversed(self.events)))
        monkeypatch.setattr(runner, "perception_device_state",
                            lambda host, device: {"current_state": self.state})
        clock = iter(range(0, 10_000_000))
        monkeypatch.setattr(runner.time, "monotonic", lambda: float(next(clock)))

    def sleep(self, seconds):
        self.t += seconds

    def admin(self, host, route, body=None):
        self.calls.append((route, body))
        if route == "set-state" and not self.deaf:
            self.t += .2
            self.state = body["state"]
            self.events.append({"ts": self.t, "name": "state_changed", "data": {"state": body["state"]}})
        return {"ok": True}


def test_apply_state_needs_fresh_echo_not_old_event(monkeypatch):
    robot = FakeRobot(monkeypatch, state="sleep", deaf=True)
    robot.events.append({"ts": 10.0, "name": "state_changed", "data": {"state": "idle"}})
    robot.state = "idle"  # current_state already idle, but only from an old event
    result = runner.apply_state(CONFIG, CLOCK, "idle", attempts=2, timeout=3)
    assert result["verified"] is False
    assert [r for r, _ in robot.calls] == ["set-state", "set-state"]


def test_apply_state_confirms_echo(monkeypatch):
    robot = FakeRobot(monkeypatch, state="sleep")
    result = runner.apply_state(CONFIG, CLOCK, "idle")
    assert result["verified"] and result["attempts"] == 1
    assert robot.state == "idle"


def test_failed_escape_pauses_session(tmp_path, monkeypatch):
    FakeRobot(monkeypatch, state="sleep", deaf=True)
    monkeypatch.setattr(runner, "journal", lambda session, event: None)
    result = runner.restore_state(tmp_path, CONFIG, CATALOGUE["S04-sleep"], CLOCK)
    assert result["verified"] is False
    assert "not confirmed idle" in json.loads((tmp_path / "PAUSE").read_text())["reason"]


def test_dance_waits_for_natural_end_before_forcing(tmp_path, monkeypatch):
    robot = FakeRobot(monkeypatch, state="idle")
    result = runner.restore_state(tmp_path, CONFIG, CATALOGUE["S06-dance"], CLOCK)
    assert result == {"verified": True, "method": "natural", "target": "idle"}
    assert robot.calls == []
    robot.state = "dance"
    forced = runner.restore_state(tmp_path, CONFIG, CATALOGUE["S06-dance"], CLOCK)
    assert forced["verified"] and forced["force_admin"] == [{"route": "abort", "status": "OK"}]
    assert [r for r, _ in robot.calls] == ["abort", "set-state"]


def test_state_changes_respect_window():
    events = [{"ts": 5, "name": "state_changed", "data": {"state": "sleep"}},
              {"ts": 15, "name": "state_changed", "data": {"state": "story_time"}},
              {"ts": 16, "name": "chat_status", "data": {"listening": True}}]
    assert runner.state_changes(events, 10, 20) == [(15.0, "story_time")]


def test_expected_state_missing_fails_case():
    case = CATALOGUE["S02-story-time"]
    logs = "结果: Tell me a story about a brave little teapot\nSentenceType.FIRST\nSentenceType.LAST"
    def judged(observed):
        result = runner.evaluate(case, {"segments": [{"text": "Story time!", "start": 0, "end": 1,
                                                      "no_speech_prob": .01, "avg_logprob": -.1}]},
                                 logs, 10, {}, "robot", recovered_override=True)
        result["observed_states"] = observed
        runner.apply_contract(result, case, runner.assess_contract(case, result, {}, []))
        return result
    assert judged(["story_time", "idle"])["interaction"] == "PASS"
    assert judged(["talk"])["failure"] == "expected_state_not_observed:story_time"


def test_unrestored_state_is_recovery_failure():
    case = CATALOGUE["S03-security"]
    result = runner.evaluate(case, {"segments": [{"text": "Watching", "start": 0, "end": 1,
                                                  "no_speech_prob": .01, "avg_logprob": -.1}]},
                             "结果: keep watch\nSentenceType.FIRST", 10, {}, "robot", recovered_override=False)
    assert result["failure"] == "state_not_restored"
    assert result["recovery"] == "FAIL"


@pytest.mark.parametrize("logs, events, state, failure", [
    ("", [], "sleep", None),
    ("识别文本: wake up what is your name", [], "sleep", "voice_processed_while_sleep"),
    ("SentenceType.FIRST", [], "sleep", "spoke_while_sleep"),
    ("", [{"ts": 15, "name": "state_changed", "data": {"state": "idle"}}], "sleep", "voice_left_sleep"),
    ("", [], "idle", "voice_left_sleep"),
])
def test_sleep_silence_contract(logs, events, state, failure):
    case = CATALOGUE["S05-sleep-ignores-voice"]
    after = {"perception": {"robot": {"current_state": state}}}
    result = runner.evaluate_silence(case, {"segments": []}, logs, events, (10, 20), after, "robot")
    assert result["failure"] == failure
    assert result["interaction"] == ("PASS" if failure is None else "FAIL")


@pytest.fixture
def boundary(monkeypatch):
    captured = []

    def fake_command(argv, **kwargs):
        if argv == ["git", "rev-parse", "HEAD"]:
            return "fixture-commit\n"
        return {("pactl", "get-default-sink"): "fixture-speaker\n",
                ("pactl", "get-sink-volume", "fixture-speaker"): "Volume: 35%\n",
                ("pactl", "get-sink-mute", "fixture-speaker"): "Mute: no\n"}[tuple(argv)]

    def capture(argv, log, session, timeout, env, deadline):
        captured.append(argv)
        raise InterruptedError("operator stopped session")
    monkeypatch.setattr(runner, "command", fake_command)
    monkeypatch.setattr(runner, "host_clock", lambda host: CLOCK)
    monkeypatch.setattr(runner, "guarded_process", capture)
    monkeypatch.setattr(runner, "brain_rows", lambda host, start, end: [])
    return {**CONFIG, "sink": "fixture-speaker", "volume": 35,
            "stop_prompts_at": "2099-01-01T00:00:00+00:00"}, captured


def snapshot_for(robot, listening=False):
    return lambda host: {"errors": {}, "services_running": True, "devices": ["robot"],
                         "perception": {"robot": {"sensor_stale": False, "current_state": robot.state,
                                                  "listening": listening, "sensor_age_s": 0,
                                                  "last_event_t": 1000, "last_chat_status_t": 1000}}}


def test_interrupted_sleep_case_still_wakes_robot(tmp_path, monkeypatch, boundary):
    config, captured = boundary
    robot = FakeRobot(monkeypatch, state="idle")
    monkeypatch.setattr(runner, "snapshot", snapshot_for(robot))
    result = runner.run_case(tmp_path, config, CATALOGUE["S05-sleep-ignores-voice"])
    assert result["setup"]["verified"] and len(captured) == 1
    assert result["exception"] == "InterruptedError"
    assert result["cleanup"]["state"]["verified"]
    assert robot.state == "idle"
    assert [b["state"] for r, b in robot.calls if r == "set-state"] == ["sleep", "idle"]


def test_cold_wake_waits_passively_then_blocks(tmp_path, monkeypatch, boundary):
    config, captured = boundary
    robot = FakeRobot(monkeypatch, state="talk")
    monkeypatch.setattr(runner, "snapshot", snapshot_for(robot, listening=True))
    result = runner.run_case(tmp_path, config, CATALOGUE["W01-cold-wake"])
    assert result["verdict"] == "BLOCKED" and result["failure"] == "wake_precondition"
    assert robot.calls == [] and captured == []


def test_cold_wake_proceeds_once_conversation_times_out(tmp_path, monkeypatch, boundary):
    config, captured = boundary
    robot = FakeRobot(monkeypatch, state="talk")
    snaps = iter([snapshot_for(robot, True)("h")] * 3 + [snapshot_for(robot)("h")] * 50)
    def snap(host):
        value = next(snaps)
        robot.state = value["perception"]["robot"]["current_state"] = (
            "talk" if value["perception"]["robot"]["listening"] else "idle")
        return value
    monkeypatch.setattr(runner, "snapshot", snap)
    result = runner.run_case(tmp_path, config, CATALOGUE["W01-cold-wake"])
    assert len(captured) == 1 and result["awaited_cold_seconds"] == 200
    assert robot.calls == []


def test_interrupted_runner_restores_state_on_next_run(tmp_path, monkeypatch):
    checkpoint = {"needs_restore": "S04-sleep"}
    runner.write_json(tmp_path / "checkpoint.json", checkpoint)
    robot = FakeRobot(monkeypatch, state="sleep")
    monkeypatch.setattr(runner, "host_clock", lambda host: CLOCK)
    monkeypatch.setattr(runner, "device_lock", lambda config: __import__("contextlib").nullcontext())
    config = {**CONFIG, "stop_prompts_at": "2000-01-01T00:00:00+00:00", "seed": 1}
    runner.run(tmp_path, config, list(CATALOGUE.values()), once=True)
    assert robot.state == "idle"
    assert "needs_restore" not in json.loads((tmp_path / "checkpoint.json").read_text())


@pytest.mark.parametrize("woke, failure", [(False, None), (True, "voice_left_sleep")])
def test_sleep_hold_is_judged_before_cleanup_wakes_robot(tmp_path, monkeypatch, boundary, woke, failure):
    # 2026-10-21 review: the after snapshot follows cleanup's set-state idle, so
    # judging S05 from it failed every attempt. Judge the held state instead.
    config, _ = boundary
    config.update(python="fixture-python", model="fixture-model", finish_at="2099-01-01T01:00:00+00:00")
    robot = FakeRobot(monkeypatch, state="idle")
    monkeypatch.setattr(runner, "snapshot", snapshot_for(robot))
    fixture_command = runner.command
    monkeypatch.setattr(runner, "command", lambda argv, **kwargs:
                        "" if argv[0] == "ffmpeg" else fixture_command(argv, **kwargs))
    monkeypatch.setattr(runner, "verify", lambda *args, **kwargs: {"audio_seconds": 40, "video_seconds": 40})
    monkeypatch.setattr(runner, "audio_metrics", lambda *args, **kwargs: {"fixture": True})
    monkeypatch.setattr(runner, "read_log_window", lambda host, start, end, directory:
                        (directory / "xiaozhi-esp32-server.log").write_text(""))

    def process(argv, log, session, timeout, env=None, deadline=None):
        directory = Path(log).parent
        if "transcribe" not in argv:
            for name, second in (("recording-start", 0), ("playback-start", 2), ("playback-end", 5)):
                (directory / f"raw.{name}.txt").write_text(f"2026-09-12T12:00:0{second}+00:00")
            if woke:
                robot.state = "idle"  # woke without an echo the recorder saw
            return
        output = Path(argv[argv.index("--output") + 1])
        calls.append(argv)
        heard = output.name == "prompt-heard.json"
        runner.write_json(output, {"segments": [{"text": "Wake up. What is your name?", "start": 0, "end": 3,
                                                 "no_speech_prob": .01, "avg_logprob": -.1}] if heard else []})
    calls = []
    monkeypatch.setattr(runner, "guarded_process", process)
    result = runner.run_case(tmp_path, config, CATALOGUE["S05-sleep-ignores-voice"])
    # 2026-10-23: biased toward "Dotty", Whisper heard a lone "Dotty" in the
    # silence and S05 came out inconclusive. Silence gets no name bias.
    assert all("--hotwords" not in argv for argv in calls)
    assert result["setup"]["verified"] and result["cleanup"]["state"]["verified"]
    assert robot.state == "idle"  # cleanup still woke the robot afterwards
    directory = tmp_path / "cases" / result["id"]
    assert runner.load(directory / "held.json")["perception"]["robot"]["current_state"] == (
        "idle" if woke else "sleep")
    assert result["failure"] == failure
    assert result["interaction"] == ("FAIL" if woke else "PASS")
    assert result["state_after_capture"] == ("idle" if woke else "sleep")
