"""Evidence and crash-safety regression tests. AI-assisted: OpenAI Codex (GPT-6)."""
import importlib.util
import hashlib
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
    ({"segments": []}, "结果: name\nSentenceType.FIRST", "response_transcription_unavailable"),
    (transcript("Dotty"), "", "no_asr_or_no_wake"),
    (transcript("Dotty"), "结果: name", "no_tts"),
    (transcript("other"), "结果: name\nSentenceType.FIRST", "response_mismatch"),
])
def test_independent_evidence_required(speech, logs, failure):
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, speech,
                             logs, 10, after(), "robot")
    assert result["failure"] == failure
    assert result["verdict"] == ("INCONCLUSIVE" if failure == "response_transcription_unavailable" else "FAIL")


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


def test_missing_tool_marker_and_words_cannot_establish_silence():
    result = runner.evaluate({"expected_tool": "think_hard"}, {"segments": []},
                             "结果: think\nSentenceType.FIRST", 10, after(), "robot")
    assert result["failure"] == "response_transcription_unavailable"
    assert result["interaction"] == "INCONCLUSIVE"


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


def receive_frame(frame):
    return ("260912 22:24:45[0.9.3_SiWhPiLononoCh][core.handle.textMessageProcessor]-INFO-收到"
            + frame["type"] + "消息：" + json.dumps(frame))


@pytest.mark.parametrize("frame", [
    {"session_id": "fixture", "type": "listen", "state": "detect", "text": "Hi ESP"},
    {"type": "event", "name": "wake_word_detected", "data": {"phrase": "Hi ESP"}},
])
def test_real_received_wake_frame_proves_wake_with_idle_precondition(frame):
    before = {**after(), "devices": ["robot"]}
    result = runner.evaluate({"require_wake_event": True}, transcript("Dotty"),
                             receive_frame(frame) + "\n结果: name\nSentenceType.FIRST",
                             10, after(), "robot", before=before)
    assert result["wake_event"] == "PASS"
    assert result["wake_precondition"] == "PASS"
    assert result["interaction"] == "PASS"


@pytest.mark.parametrize("false_evidence", [
    receive_frame({"type": "listen", "state": "start", "mode": "auto"}),
    receive_frame({"type": "event", "name": "state_changed", "data": {"state": "talk"}}),
    '结果: {"type":"listen","state":"detect"}',
    'wake_word_detected',
    'PiClient: text wake_word_detected',
    '[core.handle.textMessageProcessor]-INFO-收到listen消息：{"type":"event","name":"wake_word_detected"}',
    '[core.handle.textMessageProcessor]-INFO-收到listen消息：{bad json}',
])
def test_existing_listening_or_keyword_mentions_do_not_prove_wake(false_evidence):
    result = runner.evaluate({"require_wake_event": True}, transcript("Dotty"),
                             false_evidence + "\n结果: name\nSentenceType.FIRST", 10, after(), "robot",
                             before={**after(), "devices": ["robot"]})
    assert result["failure"] == "no_wake_event"
    assert result["interaction"] == "FAIL"


@pytest.mark.parametrize("before", [
    None,
    {**after(current_state="talk", listening=True), "devices": ["robot"]},
    {**after(sensor_stale=True), "devices": ["robot"]},
    {**after(), "devices": ["robot", "other"]},
])
def test_absent_wake_precondition_blocks_even_if_wake_frame_seen(before):
    result = runner.evaluate({"require_wake_event": True}, transcript("Dotty"),
                             receive_frame({"type": "listen", "state": "detect"})
                             + "\n结果: name\nSentenceType.FIRST", 10, after(), "robot", before=before)
    assert result["verdict"] == "BLOCKED"
    assert result["failure"] == "wake_precondition"


def test_warm_followup_does_not_require_cold_wake():
    result = runner.evaluate({"recovery_policy": "ready_for_followup"}, transcript("Dotty"),
                             "结果: name\nSentenceType.FIRST\nSentenceType.LAST", 10,
                             after(current_state="talk", listening=True), "robot",
                             before={**after(current_state="talk", listening=True), "devices": ["robot"]})
    assert result["interaction"] == "PASS"
    assert result["wake_event"] == "NOT_REQUIRED"


def test_run_case_blocks_before_playback_without_changing_robot(tmp_path, monkeypatch):
    commands = []
    def fake_command(argv, **kwargs):
        commands.append(argv)
        assert argv == ["git", "rev-parse", "HEAD"]
        return "fixture-commit\n"
    monkeypatch.setattr(runner, "command", fake_command)
    monkeypatch.setattr(runner, "snapshot", lambda host: {
        **after(current_state="talk", listening=True), "devices": ["robot"]})
    result = runner.run_case(tmp_path, {"host": "unused", "device": "robot"},
                             {"id": "wake", "require_wake_event": True})
    assert result["verdict"] == "BLOCKED"
    assert result["failure"] == "wake_precondition"
    assert result["capture"] == "INCONCLUSIVE"
    assert len(commands) == 1


@pytest.fixture
def prompt_capture_boundary(monkeypatch):
    """Stop at the subprocess boundary; never touch real playback or hardware."""
    captured = []
    def fake_command(argv, **kwargs):
        if argv == ["git", "rev-parse", "HEAD"]:
            return "fixture-commit\n"
        return {("pactl", "get-default-sink"): "fixture-speaker\n",
                ("pactl", "get-sink-volume", "fixture-speaker"): "Volume: 35%\n",
                ("pactl", "get-sink-mute", "fixture-speaker"): "Mute: no\n"}[tuple(argv)]
    def capture(argv, log, session, timeout, env, deadline):
        captured.append({"argv": argv, "env": env,
                         "evidence_at_launch": json.loads((Path(log).parent / "result.json").read_text())})
        raise RuntimeError("fixture_capture_stop")
    monkeypatch.setattr(runner, "command", fake_command)
    monkeypatch.setattr(runner, "snapshot", lambda host: {**after(), "devices": ["robot"]})
    monkeypatch.setattr(runner, "host_clock", lambda host: {"offset_seconds": 0, "uncertainty_seconds": 0})
    monkeypatch.setattr(runner, "guarded_process", capture)
    return {"host": "unused", "device": "robot", "sink": "fixture-speaker", "volume": 35,
            "stop_prompts_at": "2099-01-01T00:00:00+00:00"}, captured


def test_explicit_warm_state_case_blocks_before_capture_when_idle(tmp_path, prompt_capture_boundary):
    config, captured = prompt_capture_boundary
    result = runner.run_case(tmp_path, config, {"id": "sleep", "prompt": "Go to sleep.",
                                               "require_warm_listening": True})
    assert result["verdict"] == "BLOCKED"
    assert result["failure"] == "warm_listening_precondition"
    assert result["warm_listening_precondition"] == "BLOCKED"
    assert result["capture"] == "INCONCLUSIVE"
    assert captured == []


@pytest.mark.parametrize("state", ["idle", "talk", "story_time"])
def test_explicit_warm_precondition_requires_listening_not_talk_state(
        tmp_path, monkeypatch, prompt_capture_boundary, state):
    config, captured = prompt_capture_boundary
    monkeypatch.setattr(runner, "snapshot", lambda host: {
        **after(current_state=state, listening=True), "devices": ["robot"]})
    result = runner.run_case(tmp_path, config, {"id": "sleep", "prompt": "Go to sleep.",
                                               "require_warm_listening": True})
    assert result["failure"] == "fixture_capture_stop"
    assert result["warm_listening_precondition"] == "PASS"
    assert len(captured) == 1


@pytest.mark.parametrize("mapping", [None, {}, {"other-case": {"path": "/unrelated/prompt.wav"}}])
def test_unconfigured_case_cannot_inherit_another_prompt_wav(tmp_path, monkeypatch, prompt_capture_boundary, mapping):
    config, captured = prompt_capture_boundary
    if mapping is not None:
        config["prompt_wavs"] = mapping
    monkeypatch.setenv("DOTTY_AV_PROMPT_WAV", "/unrelated/prompt.wav")
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?"})
    assert result["failure"] == "fixture_capture_stop"
    assert "DOTTY_AV_PROMPT_WAV" not in captured[0]["env"]
    assert result["prompt_provenance"] == {"source": "harness_default", "renderer": "espeak-ng",
                                             "text": "Twelve plus seven?"}


@pytest.mark.parametrize("relative", [False, True])
def test_configured_prompt_is_bound_to_case_and_persisted_before_capture(tmp_path, monkeypatch, prompt_capture_boundary, relative):
    config, captured = prompt_capture_boundary
    wav = tmp_path / "original.wav"
    wav.write_bytes(b"fixture waveform bytes")
    entry = {"path": wav.name if relative else str(wav), "text": "Twelve plus seven?",
             "sha256": hashlib.sha256(wav.read_bytes()).hexdigest(), "renderer": "local Piper fixture"}
    config["prompt_wavs"] = {"arithmetic": entry}
    monkeypatch.setenv("DOTTY_AV_PROMPT_WAV", "/unrelated/prompt.wav")
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": entry["text"]})
    assert result["failure"] == "fixture_capture_stop"
    selected = Path(captured[0]["env"]["DOTTY_AV_PROMPT_WAV"])
    assert selected != wav  # Freeze per-attempt evidence against later source edits.
    assert selected.read_bytes() == wav.read_bytes()
    assert result["prompt_provenance"] == {**entry, "source_path": str(wav),
                                             "path": str(selected), "source": "configured_wav"}
    assert captured[0]["evidence_at_launch"]["prompt_provenance"] == result["prompt_provenance"]
    wav.write_bytes(b"later replacement waveform")
    assert hashlib.sha256(selected.read_bytes()).hexdigest() == entry["sha256"]


@pytest.mark.parametrize("replacement, failure", [
    ({"text": "Twelve plus ten?"}, "prompt_wav_text_mismatch"),
    ({"text": "Twelve plus seven? "}, "prompt_wav_text_mismatch"),
    ({"sha256": "0" * 64}, "prompt_wav_sha256_mismatch"),
])
def test_mismatched_prompt_cannot_reach_capture(tmp_path, prompt_capture_boundary, replacement, failure):
    config, captured = prompt_capture_boundary
    wav = tmp_path / "prompt.wav"
    wav.write_bytes(b"fixture waveform bytes")
    config["prompt_wavs"] = {"arithmetic": {
        "path": str(wav), "text": "Twelve plus seven?",
        "sha256": hashlib.sha256(wav.read_bytes()).hexdigest(), "renderer": "fixture", **replacement}}
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?"})
    assert result["failure"] == failure
    assert result["capture"] == "INCONCLUSIVE"
    assert captured == []


@pytest.mark.parametrize("defect", ["missing_file", "empty_file", "path", "text", "sha256", "renderer",
                                    "blank_renderer", "invalid_hash", "entry_not_object", "map_not_object"])
def test_invalid_prompt_manifest_rejected_before_capture(tmp_path, prompt_capture_boundary, defect):
    config, captured = prompt_capture_boundary
    wav = tmp_path / "prompt.wav"
    if defect != "missing_file":
        wav.write_bytes(b"" if defect == "empty_file" else b"fixture waveform bytes")
    entry = {"path": str(wav), "text": "Twelve plus seven?",
             "sha256": hashlib.sha256(b"fixture waveform bytes").hexdigest(), "renderer": "fixture"}
    if defect in entry:
        del entry[defect]
    elif defect == "blank_renderer":
        entry["renderer"] = " "
    elif defect == "invalid_hash":
        entry["sha256"] = "not-a-sha256"
    config["prompt_wavs"] = {"arithmetic": None if defect == "entry_not_object" else entry}
    if defect == "map_not_object":
        config["prompt_wavs"] = []
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?"})
    assert result["verdict"] == "FAIL"
    assert result["failure"].startswith("prompt_wav")
    assert result["capture"] == "INCONCLUSIVE"
    assert captured == []


@pytest.mark.parametrize("threshold", [None, .25, 0, 1])
def test_response_vad_threshold_does_not_change_prompt_transcription(tmp_path, monkeypatch, prompt_capture_boundary, threshold):
    config, _ = prompt_capture_boundary
    expected = .5 if threshold is None else float(threshold)
    if threshold is not None:
        config["response_vad_threshold"] = threshold
    config.update(python="fixture-python", model="fixture-model",
                  finish_at="2099-01-01T01:00:00+00:00")
    boundary_command = runner.command
    monkeypatch.setattr(runner, "command", lambda argv, **kwargs:
                        "" if argv[0] == "ffmpeg" else boundary_command(argv, **kwargs))
    monkeypatch.setattr(runner, "verify", lambda *args, **kwargs: {"audio_seconds": 40, "video_seconds": 40})
    def logs(host, start, end, directory):
        (directory / "xiaozhi-esp32-server.log").write_text("结果: Twelve plus seven\nSentenceType.FIRST")
    monkeypatch.setattr(runner, "read_log_window", logs)
    transcriptions = {}
    def process(argv, log, session, timeout, env=None, deadline=None):
        directory = Path(log).parent
        if "transcribe" not in argv:
            for name, second in (("recording-start", 0), ("playback-start", 2), ("playback-end", 5)):
                (directory / f"raw.{name}.txt").write_text(f"2026-09-12T12:00:0{second}+00:00")
            return
        label = Path(argv[3]).stem
        transcriptions[label] = argv
        threshold = float(argv[argv.index("--vad-threshold") + 1]) if "--vad-threshold" in argv else .5
        output = Path(argv[argv.index("--output") + 1])
        runner.write_json(output, {**transcript("nineteen" if label == "response" else "Twelve plus seven"),
                                  "vad_parameters": {"threshold": threshold}, "vad_filter": True,
                                  "analysis": {"enabled": "--normalize" in argv}, "model": "fixture-model"})
    monkeypatch.setattr(runner, "guarded_process", process)
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?",
                                               "asr": [["twelve"], ["seven"]], "reply": [["nineteen"]]})
    assert result["interaction"] == "PASS", result
    response = transcriptions["response"]
    assert float(response[response.index("--vad-threshold") + 1]) == expected
    assert "--vad-threshold" not in transcriptions["prompt-heard"]
    assert result["response_vad_threshold"] == expected
    assert result["transcription_analysis"]["response"]["vad_parameters"] == {"threshold": expected}
    assert result["transcription_analysis"]["prompt-heard"]["vad_parameters"] == {"threshold": .5}


@pytest.mark.parametrize("threshold", [-.1, 1.1, float("nan"), float("inf"), -float("inf"),
                                        True, False, None, ".25", [], {}])
def test_invalid_response_vad_threshold_rejected_before_capture(tmp_path, prompt_capture_boundary, threshold):
    config, captured = prompt_capture_boundary
    config["response_vad_threshold"] = threshold
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?"})
    assert result["failure"] == "response_vad_threshold_must_be_finite_number_between_0_and_1"
    assert result["capture"] == "INCONCLUSIVE"
    assert captured == []


@pytest.mark.parametrize("arguments, expected", [([], .5), (["--response-vad-threshold", "0.25"], .25)])
def test_init_persists_response_vad_setting(tmp_path, monkeypatch, arguments, expected):
    monkeypatch.setattr(sys, "argv", ["dotty_overnight.py", "init", "--session", str(tmp_path),
                                     "--host", "fixture@fixture-host", *arguments])
    monkeypatch.setattr(runner, "admin", lambda host, route: {"devices": ["fixture-robot"]})
    monkeypatch.setattr(runner, "command", lambda *args, **kwargs: "fixture-speaker\n")
    runner.main()
    assert runner.load(tmp_path / "config.json")["response_vad_threshold"] == expected


def test_init_requires_explicit_deployment_host(tmp_path, monkeypatch):
    monkeypatch.delenv("DOTTY_TEST_HOST", raising=False)
    session = tmp_path / "not-created"
    monkeypatch.setattr(sys, "argv", ["dotty_overnight.py", "init", "--session", str(session)])
    monkeypatch.setattr(runner, "admin", lambda *args: pytest.fail("must not contact an assumed host"))
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert not session.exists()


def test_init_accepts_explicit_host_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTTY_TEST_HOST", "fixture@fixture-host")
    monkeypatch.setattr(sys, "argv", ["dotty_overnight.py", "init", "--session", str(tmp_path)])
    seen = []
    def admin(host, route):
        seen.append((host, route))
        return {"devices": ["fixture-robot"]}
    monkeypatch.setattr(runner, "admin", admin)
    monkeypatch.setattr(runner, "command", lambda *args, **kwargs: "fixture-speaker\n")
    runner.main()
    assert seen == [("fixture@fixture-host", "devices")]
    assert runner.load(tmp_path / "config.json")["host"] == "fixture@fixture-host"


def test_preflight_reports_response_vad_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "snapshot", lambda host: after())
    monkeypatch.setattr(runner, "space_ok", lambda session: True)
    result = runner.preflight(tmp_path, {"host": "unused", "model": str(tmp_path), "response_vad_threshold": .25})
    assert result["response_vad_threshold"] == .25


def test_preflight_rejects_invalid_vad_before_external_checks(tmp_path, monkeypatch):
    def no_snapshot(*args):
        pytest.fail("invalid config must not access the live host")
    monkeypatch.setattr(runner, "snapshot", no_snapshot)
    with pytest.raises(ValueError, match="response_vad_threshold"):
        runner.preflight(tmp_path, {"host": "unused", "response_vad_threshold": float("nan")})


@pytest.mark.parametrize("command, threshold", [("init", "nan"), ("init", "inf"), ("init", "-0.1"),
                                                ("init", "1.1"), ("run", ".25"), ("resume", ".25")])
def test_vad_cli_cannot_silently_override_existing_config(tmp_path, monkeypatch, command, threshold):
    session = tmp_path / "not-created"
    monkeypatch.setattr(sys, "argv", ["dotty_overnight.py", command, "--session", str(session),
                                      "--response-vad-threshold", threshold])
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert not session.exists()


def test_blocked_prerequisite_does_not_increment_failure_threshold(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    runner.record_attempt(checkpoint, {"id": "wake"},
                          {"verdict": "BLOCKED", "failure": "wake_precondition"})
    assert checkpoint["blocked"] == {"wake": "wake_precondition"}
    assert checkpoint["counts"]["wake"]["failures"] == 0
    assert checkpoint["consecutive_failures"] == 0
    assert checkpoint["completed"] == []


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
    assert result["failure"] == "response_transcription_unavailable"
    assert result["interaction"] == "INCONCLUSIVE"


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
