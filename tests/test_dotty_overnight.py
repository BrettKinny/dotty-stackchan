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
        "sensor_stale": False, "current_state": "idle", "listening": False,
        "sensor_age_s": 0, "last_event_t": 1000, "last_chat_status_t": 1000, **state}}}


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
    monkeypatch.setattr(runner, "brain_rows", lambda host, start, end: [])
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


def test_fresh_state_event_cannot_revalidate_old_listening_status(
        tmp_path, monkeypatch, prompt_capture_boundary):
    config, captured = prompt_capture_boundary
    monkeypatch.setattr(runner, "snapshot", lambda host: {
        **after(current_state="idle", listening=True, last_event_t=1500,
                last_chat_status_t=1000, sensor_age_s=1), "devices": ["robot"]})
    result = runner.run_case(tmp_path, config, {"id": "sleep", "prompt": "Go to sleep.",
                                               "require_warm_listening": True})
    assert result["verdict"] == "BLOCKED"
    assert result["failure"] == "warm_listening_precondition"
    assert result["warm_listening_status_age_seconds"] == 501
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


def test_window_metrics_keep_prompt_and_response_separate_without_verdict(tmp_path, monkeypatch):
    media = tmp_path / "raw.mp4"
    sidecar = tmp_path / "raw.capture.wav"
    sidecar.write_bytes(b"fixture")
    calls = []
    def metrics(path, start, duration):
        calls.append((Path(path).name, start, duration))
        return {"clipping_ratio": .006 if start == 0 else 0,
                "channel_policy": "native; no downmix or resampling"}
    monkeypatch.setattr(runner, "audio_metrics", metrics)
    result = runner.capture_window_metrics(media, 53, 6.4, 6.8)
    assert calls == [("raw.mp4", 0, 6.4), ("raw.mp4", 6.8, 46.2),
                     ("raw.capture.wav", 0, 6.4), ("raw.capture.wav", 6.8, 46.2)]
    assert result["container_audio"]["prompt"]["clipping_ratio"] == .006
    assert result["container_audio"]["response"]["clipping_ratio"] == 0
    assert result["lossless_capture"]["source"] == str(sidecar)
    assert "verdict" not in result and "interaction" not in result
    assert "not sample-aligned" in result["timing_note"]


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
    monkeypatch.setattr(runner, "audio_metrics", lambda *args, **kwargs: {"fixture": True})
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
    assert response[response.index("--hotwords") + 1] == "Dotty"
    assert "--hotwords" not in transcriptions["prompt-heard"]
    assert result["response_vad_threshold"] == expected
    assert result["transcription_analysis"]["response"]["vad_parameters"] == {"threshold": expected}
    assert result["transcription_analysis"]["prompt-heard"]["vad_parameters"] == {"threshold": .5}


NAME = "What is your name? Please answer in one short sentence."
REPEAT = "Repeat these five words: purple, robot, seven, window, banana."


@pytest.mark.parametrize("prompt, segments, expected", [
    # 2026-10-21 V01: prompt played at 2.00-4.85 s after launch, heard at 1.04-3.94 s.
    (NAME, [{"text": "What is your name?", "start": 1.04, "end": 3.94}], (4.49, .96)),
    # V02: a long prompt; the crop still moves by the lag less the margin.
    (REPEAT, [{"text": "Repeat these", "start": 1.04, "end": 2.9}, {"text": "banana", "start": 3.6, "end": 6.08}],
     (6.56, .96)),
    # A late-heard prompt word bounds the crop: never before its end + 0.3 s.
    (NAME, [{"text": "What is your name?", "start": 1.04, "end": 4.6}], (4.9, .96)),
    # 2026-10-21 review: the applied lag is capped at 1.5 s (less the margin).
    (NAME, [{"text": "What is your name?", "start": .3, "end": 3.2}], (3.95, 1.7)),
    # Nothing heard, nothing from the prompt heard, or a lag that cannot be right.
    (NAME, [], (None, None)),
    (NAME, [{"text": " ", "start": 0.5, "end": 1}], (None, None)),
    (NAME, [{"text": "Hello there, okay.", "start": 1.04, "end": 3.94}], (None, None)),
    (NAME, [{"text": "What is your name?", "start": 2.5, "end": 4}], (None, -.5)),
])
def test_response_crop_corrected_by_capture_lag(prompt, segments, expected):
    offset = 5.25 if not any(s["end"] > 5 for s in segments) else 7.32
    got = runner.corrected_response_offset(2.0, offset, segments, prompt)
    assert got == ((offset if expected[0] is None else expected[0]), expected[1])
    assert got[0] <= offset


def test_stray_pre_prompt_utterance_does_not_set_capture_lag():
    # 2026-10-21 review: someone saying "okay" before playback once set a 1.8 s
    # lag and cut the crop into the prompt. Only prompt words measure the lag.
    segments = [{"text": "Okay then.", "start": .2, "end": .7},
                {"text": "What is your name?", "start": 1.04, "end": 3.94}]
    assert runner.corrected_response_offset(2.0, 5.25, segments, NAME) == (4.49, .96)
    # ...and a stray utterance after the prompt does not move the floor either.
    segments.append({"text": "Hello there.", "start": 4.2, "end": 4.8})
    assert runner.corrected_response_offset(2.0, 5.25, segments, NAME) == (4.49, .96)


@pytest.mark.parametrize("vad_text, novad_text, used", [
    ("", "nineteen", True), ("", "", False),
    # VAD dropped the short first sentence that held the answer.
    ("it is a number", "nineteen it is a number", True),
    ("it is a number", "it is a number too", False),
    # An invented word in the silence (no-speech 0.65) is not reply evidence.
    ("it is a number", "UNSURE:nineteen", False),
])
def test_vad_response_missing_the_reply_retried_without_vad(tmp_path, monkeypatch, prompt_capture_boundary,
                                                            vad_text, novad_text, used):
    config, _ = prompt_capture_boundary
    config.update(python="fixture-python", model="fixture-model",
                  finish_at="2099-01-01T01:00:00+00:00")
    boundary_command = runner.command
    monkeypatch.setattr(runner, "command", lambda argv, **kwargs:
                        "" if argv[0] == "ffmpeg" else boundary_command(argv, **kwargs))
    monkeypatch.setattr(runner, "verify", lambda *args, **kwargs: {"audio_seconds": 40, "video_seconds": 40})
    monkeypatch.setattr(runner, "audio_metrics", lambda *args, **kwargs: {"fixture": True})
    def logs(host, start, end, directory):
        (directory / "xiaozhi-esp32-server.log").write_text(
            "结果: Twelve plus seven\nSentenceType.FIRST, nineteen")
    monkeypatch.setattr(runner, "read_log_window", logs)
    calls = []
    def process(argv, log, session, timeout, env=None, deadline=None):
        directory = Path(log).parent
        if "transcribe" not in argv:
            for name, second in (("recording-start", 0), ("playback-start", 2), ("playback-end", 5)):
                (directory / f"raw.{name}.txt").write_text(f"2026-09-12T12:00:0{second}+00:00")
            return
        output = Path(argv[argv.index("--output") + 1])
        calls.append(output.name)
        text = {"prompt-heard.json": "Twelve plus seven", "response.json": vad_text,
                "response-novad.json": novad_text}[output.name]
        if text.startswith("UNSURE:"):
            runner.write_json(output, {"segments": [{"text": text[7:], "start": 30, "end": 31,
                                                     "no_speech_prob": .65, "avg_logprob": -.7}]})
            return
        runner.write_json(output, transcript(text) if text else {"segments": []})
    monkeypatch.setattr(runner, "guarded_process", process)
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?",
                                               "asr": [["twelve"], ["seven"]], "reply": [["nineteen"]]})
    assert calls == ["prompt-heard.json", "response.json", "response-novad.json"]
    assert result["transcription_analysis"]["response_fallback"] == {"vad_filter": False, "used": used}
    if used:
        assert result["interaction"] == "PASS", result
    elif vad_text:
        assert result["interaction"] != "PASS" and result["failure"].startswith("response_")
    else:
        assert result["failure"] == "response_transcription_unavailable"


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
    runner.record_attempt(checkpoint, {"id": "song"},
                          {"verdict": "BLOCKED", "failure": "song_not_in_catalogue"})
    assert checkpoint["blocked"] == {"song": "song_not_in_catalogue"}
    assert checkpoint["counts"]["song"]["failures"] == 0
    assert checkpoint["consecutive_failures"] == 0
    assert checkpoint["completed"] == []


def test_slow_cold_wake_precondition_is_retried_not_blocked_for_the_night(tmp_path):
    # 2026-10-21 review: one conversation slow to time out must not block W01.
    checkpoint = runner.restore_checkpoint(tmp_path)
    blocked = {"verdict": "BLOCKED", "failure": "wake_precondition"}
    runner.record_attempt(checkpoint, {"id": "wake"}, blocked)
    assert checkpoint["blocked"] == {}
    assert runner.eligible_cases([{"id": "wake"}], checkpoint) == [{"id": "wake"}]
    assert checkpoint["counts"]["wake"]["failures"] == 0 and checkpoint["consecutive_failures"] == 0
    runner.record_attempt(checkpoint, {"id": "wake"}, blocked)
    runner.record_attempt(checkpoint, {"id": "wake"}, blocked)
    assert checkpoint["unresolved"] == ["wake"] and checkpoint["quarantined"] == []


def test_stale_listening_status_is_retried_not_blocked(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    blocked = {"verdict": "BLOCKED", "failure": "warm_listening_precondition"}
    runner.record_attempt(checkpoint, {"id": "song"}, blocked)
    assert checkpoint["blocked"] == {}
    assert checkpoint["counts"]["song"]["failures"] == 0
    assert checkpoint["consecutive_failures"] == 0
    runner.record_attempt(checkpoint, {"id": "song"}, blocked)
    runner.record_attempt(checkpoint, {"id": "song"}, blocked)
    assert checkpoint["unresolved"] == ["song"]


def test_disconnected_robot_is_retried_not_failed(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    blocked = {"verdict": "BLOCKED", "failure": "device_disconnected"}
    for _ in range(3):
        runner.record_attempt(checkpoint, {"id": "identity"}, blocked)
    counts = checkpoint["counts"]["identity"]
    assert counts["failures"] == 0 and counts.get("inconclusive", 0) == 0
    assert checkpoint["quarantined"] == [] and checkpoint["unresolved"] == []
    # ...but a robot that stays gone pauses the run like any repeated failure.
    assert checkpoint["consecutive_failures"] == 3


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


def test_fail_verdict_is_never_an_acoustic_pass(tmp_path):
    # 2026-10-21 review: the catch-all except sets verdict FAIL after evaluation
    # may already have marked every field PASS. That is a failure, not a pass.
    checkpoint = runner.restore_checkpoint(tmp_path)
    crashed = {**dict.fromkeys(("capture", "playback", "interaction", "recovery"), "PASS"),
               "verdict": "FAIL", "failure": "frame extraction failed", "exception": "CalledProcessError"}
    for _ in range(3):
        runner.record_attempt(checkpoint, {"id": "identity"}, crashed)
    counts = checkpoint["counts"]["identity"]
    assert (counts["streak"], counts["failures"]) == (0, 3)
    assert checkpoint["quarantined"] == ["identity"]
    assert checkpoint["completed"] == []


@pytest.mark.parametrize("raised", [InterruptedError("operator stopped session"),
                                    runner.DeadlineReached("session deadline reached")])
def test_operator_stop_or_deadline_is_not_a_product_failure(tmp_path, prompt_capture_boundary, monkeypatch, raised):
    # 2026-10-21 review: stopping the session mid-case must not quarantine it.
    config, captured = prompt_capture_boundary
    def capture(argv, log, session, timeout, env, deadline):
        captured.append(argv)
        raise raised
    monkeypatch.setattr(runner, "guarded_process", capture)
    result = runner.run_case(tmp_path, config, {"id": "identity", "prompt": "What is your name?"})
    assert result["verdict"] == result["interaction"] == "INCONCLUSIVE"
    assert result["failure"] == "interrupted"
    checkpoint = runner.restore_checkpoint(tmp_path / "ledger")  # run_case leaves active.json
    good = dict.fromkeys(("capture", "playback", "interaction", "recovery"), "PASS")
    runner.record_attempt(checkpoint, {"id": "identity"}, good)
    for _ in range(3):
        runner.record_attempt(checkpoint, {"id": "identity"}, result)
    counts = checkpoint["counts"]["identity"]
    assert counts["streak"] == 0 and counts["failures"] == 0
    assert checkpoint["consecutive_failures"] == 0
    assert checkpoint["quarantined"] == [] and checkpoint["unresolved"] == []


def test_transcription_timeout_is_inconclusive_not_quarantine(tmp_path, monkeypatch, prompt_capture_boundary):
    config, _ = prompt_capture_boundary
    config.update(python="fixture-python", model="fixture-model", finish_at="2099-01-01T01:00:00+00:00")
    boundary_command = runner.command
    monkeypatch.setattr(runner, "command", lambda argv, **kwargs:
                        "" if argv[0] == "ffmpeg" else boundary_command(argv, **kwargs))
    monkeypatch.setattr(runner, "verify", lambda *args, **kwargs: {"audio_seconds": 40, "video_seconds": 40})
    monkeypatch.setattr(runner, "read_log_window", lambda host, start, end, directory:
                        (directory / "xiaozhi-esp32-server.log").write_text(""))
    def process(argv, log, session, timeout, env=None, deadline=None):
        directory = Path(log).parent
        if "transcribe" in argv:
            raise TimeoutError(f"process exceeded {timeout}s")
        for name, second in (("recording-start", 0), ("playback-start", 2), ("playback-end", 5)):
            (directory / f"raw.{name}.txt").write_text(f"2026-09-12T12:00:0{second}+00:00")
    monkeypatch.setattr(runner, "guarded_process", process)
    result = runner.run_case(tmp_path, config, {"id": "arithmetic", "prompt": "Twelve plus seven?"})
    assert result["capture"] == "PASS"
    assert result["verdict"] == result["interaction"] == "INCONCLUSIVE"
    assert result["failure"] == "harness_timeout"
    checkpoint = runner.restore_checkpoint(tmp_path / "ledger")  # run_case leaves active.json
    for _ in range(3):
        runner.record_attempt(checkpoint, {"id": "arithmetic"}, result)
    assert checkpoint["quarantined"] == [] and checkpoint["counts"]["arithmetic"]["failures"] == 0
    assert checkpoint["consecutive_failures"] == 0
    # A timeout that keeps recurring is set aside rather than retried for ever.
    assert checkpoint["unresolved"] == ["arithmetic"]


def test_speaker_check_failure_in_harness_is_not_a_pass(tmp_path, monkeypatch, prompt_capture_boundary):
    # 2026-10-21 review: dotty-av-test.sh exits 3 when the sink volume or mute
    # moved; guarded_process turns that into an error the runner records as FAIL.
    config, _ = prompt_capture_boundary
    def capture(argv, log, session, timeout, env, deadline):
        raise RuntimeError("process exit 3; see capture.log")
    monkeypatch.setattr(runner, "guarded_process", capture)
    result = runner.run_case(tmp_path, config, {"id": "identity", "prompt": "What is your name?"})
    assert result["verdict"] == "FAIL" and result["capture"] == "INCONCLUSIVE"
    assert result["failure"] == "process exit 3; see capture.log"
    checkpoint = runner.restore_checkpoint(tmp_path / "ledger")  # run_case leaves active.json
    runner.record_attempt(checkpoint, {"id": "identity"}, result)
    assert checkpoint["counts"]["identity"]["streak"] == 0
    assert checkpoint["counts"]["identity"]["failures"] == 1


def test_guarded_process_exit_status_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="process exit 3"):
        runner.guarded_process([sys.executable, "-c", "raise SystemExit(3)"],
                               tmp_path / "process.log", tmp_path, 60)


def test_snapshot_evidence_drops_recognised_identity(monkeypatch):
    # 2026-10-21 review: before.json/after.json must not name a family member.
    perception = {"robot": {"current_state": "idle", "listening": False, "sensor_stale": False,
                            "sensor_age_s": 1.5, "sensor_seen": True, "last_event_t": 1000,
                            "last_event_name": "face_recognized", "last_chat_status_t": 990,
                            "last_state_change_t": 900, "face_present": True,
                            "last_face_id": "Brett", "last_face_recognized_t": 999,
                            "last_sound_dir": "left"}}
    def ssh(host, argv, timeout=30, **kwargs):
        url = argv[-1]
        if "perception/state" in url:
            return json.dumps(perception)
        if url.endswith("/health"):
            return "{}"
        return "/x true 0 sha\n" * 4
    monkeypatch.setattr(runner, "ssh", ssh)
    monkeypatch.setattr(runner, "admin", lambda host, route, body=None: {"devices": ["robot"]})
    snap = runner.snapshot("unused")
    state = snap["perception"]["robot"]
    assert "last_face_id" not in state and "last_face_recognized_t" not in state
    assert "Brett" not in json.dumps(snap)
    assert state == {k: v for k, v in perception["robot"].items() if k in runner.PERCEPTION_FIELDS}
    assert runner.wake_precondition(snap, "robot")


def test_perception_events_evidence_drops_recognised_identity(monkeypatch):
    monkeypatch.setattr(runner, "perception_recent", lambda host, device: [
        {"ts": 1000, "name": "face_recognized", "data": {"identity": "Brett", "confidence": .9}},
        {"ts": 1001, "name": "state_changed", "data": {"state": "talk"}}])
    recorder = runner.PerceptionRecorder("unused", "robot")
    recorder.poll()
    assert "Brett" not in json.dumps(recorder.events())
    assert runner.state_changes(recorder.events(), 0, 2000) == [(1001.0, "talk")]


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


# --- #182: unattended operation and evaluator false alarms. AI-assisted: Claude. ---

def test_reference_mic_mishearing_is_inconclusive_when_service_tts_matches():
    logs = "结果: name\n发送音频消息: SentenceType.FIRST, My name is Dotty\nSentenceType.LAST"
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("Your name is Dadi"),
                             logs, 2, after(), "robot")
    assert result["failure"] == "response_transcript_disagrees_with_tts"
    assert result["interaction"] == result["verdict"] == "INCONCLUSIVE"
    assert result["service_tts_text"] == "My name is Dotty"


def test_mic_match_cannot_pass_when_logged_tts_says_something_else():
    # 2026-10-21 review: the server's own TTS text lacks the answer, so a
    # reference-mic transcript that matches (room speech) is not a pass.
    logs = "结果: name\n发送音频消息: SentenceType.FIRST, I am a little robot\nSentenceType.LAST"
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("My name is Dotty"),
                             logs, 2, after(), "robot")
    assert result["response_match"] is True and result["service_tts_match"] is False
    assert result["failure"] == "service_tts_disagrees_with_expected"
    assert result["interaction"] == result["verdict"] == "INCONCLUSIVE"
    runner.apply_contract(result, {"reply": [["Dotty"]]},
                          runner.assess_contract({"reply": [["Dotty"]]}, result, {}, []))
    assert result["interaction"] == "INCONCLUSIVE"


def test_absent_service_tts_text_leaves_mic_match_unchanged():
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("My name is Dotty"),
                             "结果: name\nSentenceType.FIRST\nSentenceType.LAST", 2, after(), "robot")
    assert result["service_tts_text"] == "" and result["failure"] is None
    assert result["interaction"] == "PASS"


def test_wrong_reply_still_fails_when_service_tts_also_lacks_the_answer():
    logs = "结果: name\n发送音频消息: SentenceType.FIRST, I like trains\nSentenceType.LAST"
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("I like trains"),
                             logs, 2, after(), "robot")
    assert result["failure"] == "response_mismatch"
    assert result["interaction"] == "FAIL"


def test_extra_recognised_speech_makes_the_case_inconclusive():
    logs = ("结果: what is your name\n识别文本: what is your name\n"
            "发送音频消息: SentenceType.FIRST, My name is Dotty\nSentenceType.LAST\n"
            "结果: I made a house\n识别文本: I made a house\n")
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("My name is Dotty"),
                             logs, 2, after(), "robot")
    assert result["extra_speech"] == ["I made a house"]
    assert result["failure"] == "extra_speech_in_capture"
    assert result["interaction"] == result["verdict"] == "INCONCLUSIVE"


def test_rejected_or_empty_recognitions_are_not_extra_speech():
    logs = ("结果: what is your name\n识别文本: what is your name\n"
            "发送音频消息: SentenceType.FIRST, My name is Dotty\nSentenceType.LAST\n"
            "结果: Thank you.\nASR-REJECT no_speech=0.811 > 0.60 | dropped 'Thank you.'\n结果: \n")
    result = runner.evaluate({"asr": [["name"]], "reply": [["Dotty"]]}, transcript("My name is Dotty"),
                             logs, 2, after(), "robot")
    assert result["extra_speech"] == []
    assert result["failure"] is None


@pytest.mark.parametrize("heard,asr_ok,expected", [
    ("repeat purple robot", True, ("PASS", "reference_mic")),
    ("repeat purple brisket", True, ("PASS", "robot_asr")),
    ("repeat purple brisket", False, ("INCONCLUSIVE", None)),
    ("", False, ("INCONCLUSIVE", None)),
])
def test_playback_is_proven_by_reference_mic_or_by_the_robot_hearing_it(heard, asr_ok, expected):
    assert runner.playback_verdict(heard, asr_ok, [["robot"]]) == expected


def test_inconclusive_attempt_resets_streak_without_counting_as_failure(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    good = dict.fromkeys(("capture", "playback", "interaction", "recovery"), "PASS")
    runner.record_attempt(checkpoint, {"id": "identity"}, good)
    for _ in range(4):
        runner.record_attempt(checkpoint, {"id": "identity"}, {**good, "interaction": "INCONCLUSIVE"})
    counts = checkpoint["counts"]["identity"]
    assert (counts["streak"], counts["failures"]) == (0, 0)
    assert checkpoint["consecutive_failures"] == 0
    assert checkpoint["quarantined"] == []


def test_mic_opener_refreshes_a_stale_listening_status_before_capture(
        tmp_path, monkeypatch, prompt_capture_boundary):
    config, captured = prompt_capture_boundary
    calls = []
    snapshots = iter([
        {**after(), "devices": ["robot"]},                                    # idle, mic closed
        {**after(current_state="talk", listening=True, sensor_age_s=1), "devices": ["robot"]},   # too fresh
        {**after(current_state="talk", listening=True, sensor_age_s=5), "devices": ["robot"]},   # settled
    ])
    monkeypatch.setattr(runner, "snapshot", lambda host: next(snapshots))
    monkeypatch.setattr(runner, "admin", lambda host, route, body=None: calls.append((route, body)) or {"ok": True})
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    result = runner.run_case(tmp_path, {**config, "mic_opener": "admin_say"},
                             {"id": "identity", "prompt": "What is your name?",
                              "require_warm_listening": True})
    assert calls == [("say", {"device_id": "robot", "text": "Ready."})]
    assert result["mic_opened_by"] == "admin_say"
    assert result["warm_listening_precondition"] == "PASS"
    assert len(captured) == 1                     # reached the capture boundary
    assert result["failure"] == "fixture_capture_stop"


def test_mic_opener_that_never_opens_the_mic_still_blocks(tmp_path, monkeypatch, prompt_capture_boundary):
    config, captured = prompt_capture_boundary
    monkeypatch.setattr(runner, "admin", lambda host, route, body=None: {"ok": True})
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(runner, "MIC_OPEN_POLLS", 3)
    result = runner.run_case(tmp_path, {**config, "mic_opener": "admin_say"},
                             {"id": "identity", "prompt": "What is your name?",
                              "require_warm_listening": True})
    assert result["verdict"] == "BLOCKED"
    assert result["failure"] == "warm_listening_precondition"
    assert captured == []


def test_init_persists_mic_opener(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "admin", lambda host, route, body=None: {"devices": ["robot"]})
    monkeypatch.setattr(runner, "command", lambda argv, **kwargs: "fixture-speaker\n")
    monkeypatch.setattr(sys, "argv", ["dotty_overnight.py", "init", "--session", str(tmp_path),
                                      "--host", "user@host", "--mic-opener", "admin_say"])
    runner.main()
    assert json.loads((tmp_path / "config.json").read_text())["mic_opener"] == "admin_say"


def test_misheard_prompt_is_an_asr_mismatch_not_extra_speech():
    logs = ("结果: Repeat these words purple frispen\n识别文本: Repeat these words purple frispen\n"
            "发送音频消息: SentenceType.FIRST, purple frispen\nSentenceType.LAST\n")
    result = runner.evaluate({"asr": [["purple"], ["Brisbane"]], "reply": [["purple"]]},
                             transcript("purple frispen"), logs, 2, after(), "robot")
    assert result["extra_speech"] == []
    assert result["failure"] == "asr_mismatch"
    assert result["interaction"] == "FAIL"


def test_repeatedly_inconclusive_case_is_set_aside_instead_of_retried_forever(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    murky = {**dict.fromkeys(("capture", "playback", "recovery"), "PASS"), "interaction": "INCONCLUSIVE"}
    for _ in range(2):
        runner.record_attempt(checkpoint, {"id": "repeat"}, murky)
    assert checkpoint["unresolved"] == []
    runner.record_attempt(checkpoint, {"id": "repeat"}, murky)
    assert checkpoint["unresolved"] == ["repeat"]
    assert checkpoint["quarantined"] == [] and checkpoint["counts"]["repeat"]["failures"] == 0
    assert runner.eligible_cases([{"id": "repeat"}, {"id": "other"}], checkpoint) == [{"id": "other"}]


def test_a_pass_clears_the_inconclusive_count(tmp_path):
    checkpoint = runner.restore_checkpoint(tmp_path)
    good = dict.fromkeys(("capture", "playback", "interaction", "recovery"), "PASS")
    murky = {**good, "interaction": "INCONCLUSIVE"}
    for attempt in (murky, murky, good, murky, murky):
        runner.record_attempt(checkpoint, {"id": "repeat"}, attempt)
    assert checkpoint["unresolved"] == []


def test_cold_wake_settles_after_the_robot_just_went_idle(monkeypatch):
    # 2026-10-23: a wake word played 4 s after the conversation timed out (the
    # robot was still reconnecting) was missed; the next one, 80 s later, woke.
    states = iter([False, False, True, True])
    sleeps = []
    monkeypatch.setattr(runner, "snapshot", lambda host: {"cold": next(states)})
    monkeypatch.setattr(runner, "wake_precondition", lambda snap, device: snap["cold"])
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    snap = runner.await_cold({"host": "h", "device": "robot"}, 60)
    assert snap == {"cold": True}
    assert runner.COLD_SETTLE_SECONDS in sleeps


def test_already_cold_robot_is_not_made_to_wait(monkeypatch):
    sleeps = []
    monkeypatch.setattr(runner, "snapshot", lambda host: {"cold": True})
    monkeypatch.setattr(runner, "wake_precondition", lambda snap, device: snap["cold"])
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    assert runner.await_cold({"host": "h", "device": "robot"}, 60) == {"cold": True}
    assert sleeps == []
