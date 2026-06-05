"""Unit tests for the OpenAICompat LLM provider (openai_compat.py).

Two audit findings:
  - the kid-mode/emoji turn-suffix was only appended to the last *user* turn,
    so a no-user-turn request (greeter/system injection) ran unconstrained;
  - a leading whitespace-only stream delta was yielded to TTS before the
    emoji-prefix check, breaking the firmware's leading-glyph face contract.

Container-only imports (config.logger, core.providers.llm.base,
core.utils.textUtils) are stubbed with controlled values so the logic is
exercised deterministically. `requests` is real and patched per-test.
"""
import importlib.util as _ilu
import pathlib
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ── Stub container-only imports BEFORE loading the module ─────────────────────
sys.modules.setdefault("config", MagicMock())
_logger_mod = types.ModuleType("config.logger")
_logger_mod.setup_logging = lambda: MagicMock()  # type: ignore[attr-defined]
sys.modules["config.logger"] = _logger_mod

for _n in ("core", "core.providers", "core.providers.llm", "core.utils"):
    sys.modules.setdefault(_n, MagicMock())

_base_mod = types.ModuleType("core.providers.llm.base")


class _StubBase:
    pass


_base_mod.LLMProviderBase = _StubBase  # type: ignore[attr-defined]
sys.modules["core.providers.llm.base"] = _base_mod

# Controlled textUtils stubs (a fixed emoji set + a sentinel suffix).
_FALLBACK = "😐"
_ALLOWED = ("😊", "😆", "😢", "😮", "🤔", "😐", "😍", "😴", "😠")
_SUFFIX = " [[SUFFIX]]"
_tu = types.ModuleType("core.utils.textUtils")
_tu.ALLOWED_EMOJIS = _ALLOWED  # type: ignore[attr-defined]
_tu.FALLBACK_EMOJI = _FALLBACK  # type: ignore[attr-defined]
_tu._SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")  # type: ignore[attr-defined]
_tu.build_turn_suffix = lambda kid_mode: _SUFFIX  # type: ignore[attr-defined]
sys.modules["core.utils.textUtils"] = _tu

# ── Load the module under test by path ───────────────────────────────────────
_OC_PY = (
    pathlib.Path(__file__).parent.parent
    / "custom-providers"
    / "openai_compat"
    / "openai_compat.py"
)
_spec = _ilu.spec_from_file_location("openai_compat_under_test", _OC_PY)
_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _provider():
    return _mod.LLMProvider({"url": "http://x/v1", "model": "m"})


def _sse(*contents):
    """Build SSE 'data:' lines for a sequence of delta content strings."""
    import json
    lines = []
    for c in contents:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    lines.append("data: [DONE]")
    return lines


def _patched_stream(prov, lines):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.iter_lines = lambda decode_unicode=True: iter(lines)
    with patch.object(_mod.requests, "post", return_value=resp):
        return list(prov._response_stream([{"role": "user", "content": "hi"}]))


class TestTurnSuffixPlacement(unittest.TestCase):

    def test_suffix_on_last_user_turn(self):
        msgs = _provider()._build_messages(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
        )
        self.assertTrue(msgs[-1]["content"].endswith(_SUFFIX))
        self.assertEqual(msgs[-1]["role"], "user")

    def test_suffix_falls_to_last_message_when_no_user_turn(self):
        # Greeter-style: dialogue ends with a non-user message.
        msgs = _provider()._build_messages(
            [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "greet"}]
        )
        joined = "".join(m["content"] for m in msgs)
        self.assertIn(_SUFFIX, joined)
        self.assertTrue(msgs[-1]["content"].endswith(_SUFFIX))

    def test_suffix_synthesized_when_dialogue_empty(self):
        msgs = _provider()._build_messages([])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], _SUFFIX)

    def test_suffix_appears_exactly_once(self):
        msgs = _provider()._build_messages(
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"}]
        )
        self.assertEqual("".join(m["content"] for m in msgs).count(_SUFFIX), 1)
        # ...and it lands on the *last* user turn, not the first.
        self.assertTrue(msgs[2]["content"].endswith(_SUFFIX))


class TestStreamEmojiOrdering(unittest.TestCase):

    def test_leading_whitespace_never_precedes_emoji(self):
        out = _patched_stream(_provider(), _sse("  ", "Hi there."))
        # Nothing whitespace-only may be surfaced before the emoji.
        self.assertTrue(out[0].strip(), f"first chunk was whitespace: {out!r}")
        self.assertTrue(out[0].startswith(_FALLBACK))
        self.assertEqual("".join(out), f"{_FALLBACK} Hi there.")

    def test_existing_emoji_not_double_prefixed(self):
        out = _patched_stream(_provider(), _sse("😊 Hello"))
        self.assertTrue(out[0].startswith("😊"))
        self.assertNotIn(_FALLBACK, "".join(out))

    def test_emoji_prepended_when_first_chunk_has_no_emoji(self):
        out = _patched_stream(_provider(), _sse("Hello"))
        self.assertEqual(out[0], f"{_FALLBACK} ")
        self.assertEqual("".join(out), f"{_FALLBACK} Hello")

    def test_all_whitespace_stream_emits_fallback(self):
        out = _patched_stream(_provider(), _sse("  ", "\n"))
        # No real content ever arrived → the no-response fallback fires, and
        # nothing whitespace-only leaked out ahead of it.
        self.assertTrue(all(c.strip() for c in out), f"whitespace leaked: {out!r}")
        self.assertIn(_FALLBACK, "".join(out))


if __name__ == "__main__":
    unittest.main()
