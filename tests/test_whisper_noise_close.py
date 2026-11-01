"""#105: close the session after a run of noise-like ASR turns.

AI-assisted (Claude). Maintainer direction 2026-10-06: close the session on
room noise (touch to wake) rather than keep listening. Real quiet speech must
survive: it scored no_speech <= 0.32 and avg_logprob >= -0.9 on the robot.
"""
import asyncio
import types

import numpy as np

from tests.test_whisper_no_speech_gate import _load


def _provider(module, config=None):
    config = config or {}
    p = module.ASRProvider.__new__(module.ASRProvider)
    p.no_speech_threshold = module._no_speech_threshold(config)
    p.hotwords = None
    p.noise_no_speech = module._float_option(config, "noise_no_speech", module.DEFAULT_NOISE_NO_SPEECH)
    p.noise_logprob = module._float_option(config, "noise_avg_logprob", module.DEFAULT_NOISE_LOGPROB)
    p.noise_runs = module.NoiseRunTracker(module._int_option(config, "noise_turn_limit", 3))
    return p


def _run_turns(module, turns, closes):
    async def go():
        provider = _provider(module)
        module._close_session_for_noise = lambda sid, n: closes.append(sid) or asyncio.sleep(0)
        outs = []
        for args in turns:
            segs = [types.SimpleNamespace(text=args[0], no_speech_prob=args[1], avg_logprob=args[2])]
            provider._transcribe_blocking = lambda audio, segs=segs: (segs, types.SimpleNamespace(language_probability=1.0))
            art = types.SimpleNamespace(pcm_bytes=np.zeros(16000, dtype=np.int16).tobytes(), file_path="x.wav")
            out, _ = await provider.speech_to_text([], "s", artifacts=art)
            outs.append(out)
        await asyncio.sleep(0)
        return outs
    return asyncio.run(go())


NOISE = (" and then the senator said", 0.5, -0.4)
QUIET_CHILD = (" can I have a story", 0.3, -0.5)


def test_three_noise_turns_close_the_session_and_blank_the_last():
    module, closes = _load(), []
    outs = _run_turns(module, [NOISE, NOISE, NOISE], closes)
    assert closes == ["s"]
    assert outs[0]["content"] and outs[1]["content"]
    assert outs[2] == {"content": ""}


def test_quiet_real_speech_never_counts():
    module, closes = _load(), []
    outs = _run_turns(module, [QUIET_CHILD] * 6, closes)
    assert closes == []
    assert all(o["content"] for o in outs)


def test_real_turn_between_noise_resets_the_run():
    module, closes = _load(), []
    _run_turns(module, [NOISE, NOISE, QUIET_CHILD, NOISE, NOISE], closes)
    assert closes == []


def test_rejected_and_empty_turns_count_as_noise():
    module, closes = _load(), []
    # no_speech 0.81 is dropped by the existing gate -> empty content
    _run_turns(module, [(" Thank you.", 0.81, -0.3)] * 3, closes)
    assert closes == ["s"]


def test_low_confidence_decode_counts():
    module = _load()
    assert module._is_noise_like("mumble", 0.1, -1.4, 0.45, -1.0)
    assert not module._is_noise_like("hello dotty", 0.1, -0.9, 0.45, -1.0)


def test_runs_far_apart_are_separate_episodes():
    module = _load()
    tracker = module.NoiseRunTracker(3, window_s=90)
    assert not tracker.record("s", True, 0)
    assert not tracker.record("s", True, 10)
    assert not tracker.record("s", True, 200)  # restarts: count 1
    assert not tracker.record("s", True, 210)
    assert tracker.record("s", True, 220)


def test_limit_zero_disables():
    module = _load()
    tracker = module.NoiseRunTracker(0)
    assert not any(tracker.record("s", True, i) for i in range(10))


def test_sessions_are_tracked_separately():
    module = _load()
    tracker = module.NoiseRunTracker(3)
    for sid in ("a", "b", "a", "b"):
        assert not tracker.record(sid, True, 0)
