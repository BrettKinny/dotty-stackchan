"""Regression tests for wake-name corrections on the live ASR text path."""

import importlib.util
import pathlib
import sys
import types
import unittest
from contextlib import contextmanager


_ROOT = pathlib.Path(__file__).parent.parent


def _stub_module(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


@contextmanager
def _container_import_stubs():
    """Install container-only imports for one module load, then restore them."""
    names = (
        "core",
        "core.utils",
        "core.handle",
        "core.utils.util",
        "core.handle.abortHandle",
        "core.handle.intentHandler",
        "core.utils.output_counter",
        "core.handle.sendAudioHandle",
        "core.utils.device_command",
    )
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in names}
    try:
        for package in ("core", "core.utils", "core.handle"):
            _stub_module(package)
        _stub_module("core.utils.util", audio_to_data=lambda *_args, **_kwargs: None)
        _stub_module("core.handle.abortHandle", handleAbortMessage=lambda *_args: None)
        _stub_module("core.handle.intentHandler", handle_user_intent=lambda *_args: None)
        _stub_module(
            "core.utils.output_counter",
            check_device_output_limit=lambda *_args: False,
        )
        _stub_module(
            "core.handle.sendAudioHandle",
            send_stt_message=lambda *_args: None,
            SentenceType=object,
        )
        _stub_module("core.utils.device_command", call_tool=lambda *_args, **_kwargs: None)
        yield
    finally:
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


# receiveAudioHandle.py is a bind-mounted upstream override, so its normal
# imports only exist inside xiaozhi-server. Provide the smallest import surface
# needed to exercise its pure text helpers on the workstation without poisoning
# sys.modules for test modules collected later.
with _container_import_stubs():
    _spec = importlib.util.spec_from_file_location(
        "receive_audio_name_corrections_under_test", _ROOT / "receiveAudioHandle.py"
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)


class TestAsrNameCorrections(unittest.TestCase):
    def test_observed_close_phonetic_variants_are_normalized(self):
        for heard in ("Dottie", "Duddy"):
            with self.subTest(heard=heard):
                corrected = _module._apply_asr_corrections(
                    f"Good night, {heard}."
                )
                self.assertEqual(corrected, "Good night, Dotty.")
                corrected = _module._apply_phrase_corrections(corrected)
                self.assertEqual(
                    _module._detect_state_phrase(corrected),
                    ("sleep", "Goodnight! 😴"),
                )

    def test_ambiguous_real_names_are_not_rewritten(self):
        for name in ("Donny", "Jody", "Jodi", "Claudia"):
            with self.subTest(name=name):
                text = f"Please say hello to {name}."
                self.assertEqual(_module._apply_asr_corrections(text), text)

    def test_alias_matching_is_word_bounded(self):
        text = "Duddybrook is a place."
        self.assertEqual(_module._apply_asr_corrections(text), text)

    def test_name_correction_preserves_goodnight_command_with_punctuation(self):
        """Production order canonicalizes the name, then ignores punctuation."""
        for heard in ("Dottie", "Duddy"):
            with self.subTest(heard=heard):
                text = _module._apply_asr_corrections(f"Good night, {heard}.")
                self.assertEqual(
                    _module._detect_state_phrase(text),
                    ("sleep", "Goodnight! 😴"),
                )

    def test_ambiguous_real_names_do_not_trigger_sleep(self):
        for heard in ("Donny", "Jody", "Jodi", "Claudia"):
            with self.subTest(heard=heard):
                self.assertIsNone(
                    _module._detect_state_phrase(
                        _module._apply_asr_corrections(f"Good night, {heard}.")
                    )
                )


if __name__ == "__main__":
    unittest.main()
