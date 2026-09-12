"""Evidence and crash-safety regression tests. AI-assisted: OpenAI Codex (GPT-6)."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("dotty_overnight", SCRIPTS / "dotty_overnight.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def after(**state):
    return {"errors": {}, "services_running": True, "perception": {"robot": {
        "sensor_stale": False, "current_state": "idle", "listening": False, **state}}}


def transcript(text):
    return {"segments": [{"text": text, "start": 0, "end": 2,
                           "no_speech_prob": .01, "avg_logprob": -.1}]}


def test_substring_is_not_correct_answer():
    assert not runner.matches("I know the answer", [["no"]])
    assert runner.matches("No, not necessarily.", [["no"]])


@pytest.mark.parametrize("speech,logs,failure", [
    ({"segments": []}, "结果: name\nSentenceType.FIRST", "no_audible_response"),
    (transcript("Dotty"), "", "no_asr_or_no_wake"),
    (transcript("Dotty"), "结果: name", "no_tts"),
    (transcript("other"), "结果: name\nSentenceType.FIRST", "response_mismatch"),
])
def test_independent_evidence_required(speech, logs, failure):
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, speech,
                             logs, 10, after(), "robot")
    assert result["failure"] == failure
    assert result["verdict"] == "FAIL"


def test_no_visual_evidence_means_no_overall_pass():
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("Dotty"),
                             "结果: name\nSentenceType.FIRST", 10, after(), "robot")
    assert result["interaction"] == "PASS"
    assert result["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("state", [after(current_state="talk", listening=True), after()])
def test_ready_for_followup_accepts_ended_tts_and_fresh_resting_state(state):
    result = runner.evaluate({"recovery_policy": "ready_for_followup", "reply": [["Dotty"]]},
                             transcript("Dotty"), "结果: name\nSentenceType.FIRST\nSentenceType.LAST",
                             10, state, "robot")
    assert result["recovery"] == "PASS"
    assert result["interaction"] == "PASS"
    assert result["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("logs", ["SentenceType.FIRST", "SentenceType.LAST\nSentenceType.FIRST",
                                  "SentenceType.LAST\n发送第一段语音:"])
def test_ready_for_followup_rejects_missing_or_prior_turn_last(logs):
    result = runner.evaluate({"recovery_policy": "ready_for_followup"}, transcript("Dotty"),
                             "结果: name\n" + logs, 10,
                             after(current_state="talk", listening=True), "robot")
    assert result["recovery"] == "FAIL"
    assert result["failure"] == "not_ready_for_followup"


@pytest.mark.parametrize("state", [
    after(current_state="talk", listening=False),
    after(current_state="idle", listening=True),
    after(current_state="sleep", listening=False),
    after(sensor_stale=True),
    {**after(), "services_running": False},
    {**after(), "errors": {"bridge": "unavailable"}},
])
def test_ready_for_followup_rejects_unhealthy_or_wrong_state(state):
    result = runner.evaluate({"recovery_policy": "ready_for_followup"}, transcript("Dotty"),
                             "结果: name\nSentenceType.FIRST\nSentenceType.LAST", 10, state, "robot")
    assert result["recovery"] == "FAIL"


def test_tool_requires_concrete_call_marker_not_spoken_name():
    result = runner.evaluate({"expected_tool": "think_hard"}, transcript("I used think_hard"),
                             "结果: please use think_hard\nSentenceType.FIRST", 10, after(), "robot")
    assert result["expected_tool_call"] == "INCONCLUSIVE"
    assert result["interaction"] == "INCONCLUSIVE"
    assert result["verdict"] == "INCONCLUSIVE"


def test_tool_marker_is_exact_and_does_not_prove_execution():
    logs = "结果: think\nSentenceType.FIRST\nPiClient: tool call name=think_hard id=call_123"
    result = runner.evaluate({"expected_tool": "think_hard"}, transcript("No"), logs, 10, after(), "robot")
    assert result["expected_tool_call"] == "PASS"
    assert result["tool_execution"] == "INCONCLUSIVE"
    other = runner.evaluate({"expected_tool": "think"}, transcript("No"), logs, 10, after(), "robot")
    assert other["expected_tool_call"] == "INCONCLUSIVE"


def test_missing_tool_marker_does_not_hide_known_acoustic_failure():
    result = runner.evaluate({"expected_tool": "think_hard"}, {"segments": []},
                             "结果: think\nSentenceType.FIRST", 10, after(), "robot")
    assert result["failure"] == "no_audible_response"
    assert result["interaction"] == "FAIL"


def test_response_word_limit_is_strict_without_asserting_creative_quality():
    logs = "结果: joke\nSentenceType.FIRST"
    result = runner.evaluate({"max_response_words": 3}, transcript("I'm a robot"), logs, 10, after(), "robot")
    assert result["response_word_count"] == 3
    assert result["response_length"] == "PASS"
    assert result["semantic_quality"] == "INCONCLUSIVE"
    assert result["verdict"] == "INCONCLUSIVE"
    longer = runner.evaluate({"max_response_words": 3}, transcript("I'm a tiny robot"), logs, 10, after(), "robot")
    assert longer["response_word_count"] == 4
    assert longer["failure"] == "response_too_long"
    assert longer["verdict"] == "FAIL"


@pytest.mark.parametrize("limit", [0, -1, True, "3", 3.5])
def test_invalid_word_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="positive integer"):
        runner.evaluate({"max_response_words": limit}, transcript("hello"),
                        "结果: hello\nSentenceType.FIRST", 10, after(), "robot")


def test_emoji_stripped_tts_does_not_imply_bad_expression():
    result = runner.evaluate({}, transcript("Dotty"), "结果: name\n发送第一段语音: Dotty",
                             10, after(), "robot")
    assert result["expression"] == "INCONCLUSIVE"
    assert result["visual"] == "INCONCLUSIVE"


def test_cannot_pool_unrelated_asr_requests():
    result = runner.evaluate({"asr": [["purple"], ["robot"]]}, transcript("yes"),
                             "结果: purple\n结果: robot\nSentenceType.FIRST", 10, after(), "robot")
    assert result["failure"] == "asr_mismatch"


def test_missing_listening_state_is_not_recovery():
    state = after()
    del state["perception"]["robot"]["listening"]
    result = runner.evaluate({"asr": [["name"]]}, transcript("Dotty"),
                             "结果: name\nSentenceType.FIRST", 10, state, "robot")
    assert result["recovery"] == "FAIL"


def test_low_confidence_hallucination_is_not_audible_response():
    speech = transcript("Dotty")
    speech["segments"][0]["avg_logprob"] = -2
    result = runner.evaluate({}, speech, "结果: name\nSentenceType.FIRST", 10, after(), "robot")
    assert result["failure"] == "no_audible_response"


def test_playback_window_crosses_midnight(tmp_path):
    for name, stamp in {
        "recording-start": "2026-09-12T23:59:56+00:00",
        "playback-start": "2026-09-12T23:59:59+00:00",
        "playback-end": "2026-09-13T00:00:05+00:00",
    }.items():
        (tmp_path / f"raw.{name}.txt").write_text(stamp)
    assert runner.playback_window(tmp_path, 30) == (3, 9.4)
    with pytest.raises(ValueError, match="truncated"):
        runner.playback_window(tmp_path, 9)


def test_host_timestamp_preserves_date_and_offset():
    assert runner.host_timestamp("2026-09-12T23:59:59+00:00", {"offset_seconds": 2}) == "2026-09-13T00:00:01+00:00"


def test_interrupted_case_quarantined_before_active_cleared(tmp_path):
    result_path = tmp_path / "cases" / "unique-attempt" / "result.json"
    runner.write_json(result_path, {"case_id": "memory-write", "verdict": "INCONCLUSIVE"})
    runner.write_json(tmp_path / "active.json", {"case": "unique-attempt"})
    checkpoint = runner.restore_checkpoint(tmp_path)
    assert checkpoint["completed"] == []
    assert checkpoint["quarantined"] == ["memory-write"]
    assert runner.load(tmp_path / "active.json") == {}
    assert runner.load(result_path)["failure"] == "interrupted_case_requires_review"
    assert runner.restore_checkpoint(tmp_path)["quarantined"] == ["memory-write"]


def test_unknown_interrupted_identity_blocks_replay(tmp_path):
    runner.write_json(tmp_path / "active.json", {"case": "missing-attempt"})
    with pytest.raises(RuntimeError, match="identity unknown"):
        runner.restore_checkpoint(tmp_path)
    assert runner.load(tmp_path / "active.json")


def test_checkpointed_attempt_not_quarantined_after_clear_crash(tmp_path):
    runner.write_json(tmp_path / "checkpoint.json", {"last_attempt": "finished-attempt"})
    runner.write_json(tmp_path / "active.json", {"case": "finished-attempt", "case_id": "identity"})
    checkpoint = runner.restore_checkpoint(tmp_path)
    assert checkpoint["quarantined"] == []
    assert runner.load(tmp_path / "active.json") == {}


def test_success_requires_streak_and_all_evidence(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    case = {"id": "identity"}
    good = dict.fromkeys(("capture", "playback", "interaction", "recovery"), "PASS")
    runner.record_attempt(checkpoint, case, good)
    runner.record_attempt(checkpoint, case, {**good, "playback": "INCONCLUSIVE"})
    runner.record_attempt(checkpoint, case, good)
    runner.record_attempt(checkpoint, case, good)
    assert checkpoint["completed"] == []
    runner.record_attempt(checkpoint, case, good)
    assert checkpoint["completed"] == ["identity"]


def test_failure_threshold_quarantines_and_pauses(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    for _ in range(3):
        runner.record_attempt(checkpoint, {"id": "identity"}, {"verdict": "FAIL"})
    assert checkpoint["consecutive_failures"] == 3
    assert checkpoint["quarantined"] == ["identity"]
    assert checkpoint["completed"] == []


def test_operator_interruption_is_not_automatically_retried(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    runner.record_attempt(checkpoint, {"id": "memory-write"}, {"exception": "InterruptedError"})
    assert checkpoint["quarantined"] == ["memory-write"]


def test_device_lock_prevents_two_output_sessions():
    config = {"device": "pytest-lock-only-no-hardware"}
    with runner.device_lock(config):
        with pytest.raises(BlockingIOError):
            with runner.device_lock(config):
                pass


def test_guarded_process_does_not_leave_child_on_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STOP", True)
    with pytest.raises(InterruptedError):
        runner.guarded_process([sys.executable, "-c", "import time; time.sleep(30)"],
                               tmp_path / "process.log", tmp_path, 60)


def test_deadline_prevents_launch(tmp_path):
    with pytest.raises(TimeoutError, match="before process launch"):
        runner.guarded_process(["this-command-must-never-start"], tmp_path / "log",
                               tmp_path, 60, deadline=0)
    assert not (tmp_path / "log").exists()
