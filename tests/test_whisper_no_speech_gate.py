"""WhisperLocal must not hand near-silence hallucinations to the LLM.

AI-assisted (Claude). Observed on the physical robot 2026-10-05: 1.3 s of
near-silence after Dotty finished speaking was transcribed as "Thank you."
with no_speech_prob=0.81, and Dotty replied to it. Every real utterance
captured that day scored <= 0.32.
"""
import asyncio
import importlib.util
import pathlib
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

import numpy as np

_ROOT = pathlib.Path(__file__).parent.parent
_STUBS = ("faster_whisper", "config", "config.logger", "core", "core.providers",
          "core.providers.asr", "core.providers.asr.base", "core.providers.asr.dto",
          "core.providers.asr.dto.dto")


@contextmanager
def _stubs():
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in _STUBS}
    try:
        for name in _STUBS:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["faster_whisper"].WhisperModel = MagicMock()
        sys.modules["config.logger"].setup_logging = lambda: MagicMock()
        sys.modules["core.providers.asr.base"].ASRProviderBase = type(
            "ASRProviderBase", (), {"__init__": lambda self: None})
        sys.modules["core.providers.asr.dto.dto"].InterfaceType = types.SimpleNamespace(LOCAL="local")
        yield
    finally:
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _load():
    with _stubs():
        spec = importlib.util.spec_from_file_location(
            "whisper_local_under_test", _ROOT / "custom-providers/asr/whisper_local.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _transcribe(no_speech_probs, text, config=None):
    module = _load()
    provider = module.ASRProvider.__new__(module.ASRProvider)
    provider.no_speech_threshold = module._no_speech_threshold(config or {})
    segments = [types.SimpleNamespace(text=text if i == 0 else "", no_speech_prob=p, avg_logprob=-0.5)
                for i, p in enumerate(no_speech_probs)]
    provider._transcribe_blocking = lambda audio: (segments, types.SimpleNamespace(language_probability=1.0))
    artifacts = types.SimpleNamespace(pcm_bytes=np.zeros(16000, dtype=np.int16).tobytes(), file_path="x.wav")
    result, _path = asyncio.run(provider.speech_to_text([], "session", artifacts=artifacts))
    return result


def test_high_no_speech_transcript_is_dropped():
    assert _transcribe([0.811], " Thank you.") == {"content": ""}


def test_real_speech_passes_through():
    assert _transcribe([0.32], " See you soon!") == {"content": "See you soon!"}


def test_threshold_is_configurable_and_can_be_disabled():
    assert _transcribe([0.811], " Thank you.", {"no_speech_threshold": 1.0}) == {"content": "Thank you."}
    assert _transcribe([0.5], " Hello.", {"no_speech_threshold": 0.4}) == {"content": ""}
