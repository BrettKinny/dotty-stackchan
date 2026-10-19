"""WhisperLocal `hotwords` bias for the wake-name (#164).

AI-assisted (Claude Opus 5.5). Whisper repeatedly heard "Dotty" as Jody,
Jodi, Claudia, Donny, Dottie or Duddy in the 2026-07-11 UAT. faster-whisper
>= 1.0 accepts a `hotwords` decoder bias; older builds would raise on the
unknown keyword for every utterance, so the provider must drop it there.
"""
import importlib.util
import pathlib
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

import numpy as np
import pytest

_ROOT = pathlib.Path(__file__).parent.parent
_STUBS = ("faster_whisper", "config", "config.logger", "core", "core.providers",
          "core.providers.asr", "core.providers.asr.base", "core.providers.asr.dto",
          "core.providers.asr.dto.dto")


class _ModernModel:
    """faster-whisper >= 1.0 transcribe signature (subset)."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, audio, language=None, beam_size=5, condition_on_previous_text=True,
                   vad_filter=False, initial_prompt=None, hotwords=None):
        type(self).calls.append({"initial_prompt": initial_prompt, "hotwords": hotwords})
        return iter([]), types.SimpleNamespace(language_probability=1.0)


class _LegacyModel(_ModernModel):
    """Pre-1.0 signature: no hotwords keyword."""

    def transcribe(self, audio, language=None, beam_size=5, condition_on_previous_text=True,
                   vad_filter=False, initial_prompt=None):
        type(self).calls.append({"initial_prompt": initial_prompt})
        return iter([]), types.SimpleNamespace(language_probability=1.0)


@contextmanager
def _stubs(model_cls):
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in _STUBS}
    try:
        for name in _STUBS:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["faster_whisper"].WhisperModel = model_cls
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


def _provider(model_cls, config):
    model_cls.calls = []
    with _stubs(model_cls):
        spec = importlib.util.spec_from_file_location(
            "whisper_local_hotwords_under_test", _ROOT / "custom-providers/asr/whisper_local.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = module.ASRProvider(config, delete_audio_file=True)
    provider._transcribe_blocking(np.zeros(16000, dtype=np.float32))
    return provider, model_cls.calls


def test_configured_wake_name_is_passed_to_warm_up_and_every_decode():
    _provider_obj, calls = _provider(_ModernModel, {"hotwords": "Dotty"})
    assert [c["hotwords"] for c in calls] == ["Dotty", "Dotty"]


@pytest.mark.parametrize("value", [None, "", "  ", "<ROBOT_NAME>", 7])
def test_absent_or_unrendered_hotwords_leave_decoding_unchanged(value):
    config = {} if value is None else {"hotwords": value}
    _provider_obj, calls = _provider(_ModernModel, config)
    assert [c["hotwords"] for c in calls] == [None, None]


def test_list_form_is_joined():
    _provider_obj, calls = _provider(_ModernModel, {"hotwords": ["Dotty", "StackChan"]})
    assert calls[-1]["hotwords"] == "Dotty StackChan"


def test_legacy_faster_whisper_ignores_hotwords_instead_of_failing_every_turn():
    _provider_obj, calls = _provider(_LegacyModel, {"hotwords": "Dotty"})
    assert len(calls) == 2
    assert all("hotwords" not in c for c in calls)
