"""Tests for the shared kid-mode content filter on the live voice path (#157).

Covers the three layers introduced by #157:
  1. the pure core in custom-providers/textUtils.py (content_filter_match +
     the sentence-buffered filter_tts_stream stream wrapper),
  2. bridge/text.py's content_filter() wrapper now delegating to the shared
     matcher (behaviour unchanged: same replacement, ring still records),
  3. the two live providers (PiVoiceLLM, OpenAICompat) wrapping their
     TTS-bound streams — hit / clean / kid-off, mirroring
     tests/test_dashboard_say_filter.py from #146.
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CUSTOM_PROVIDERS = _REPO_ROOT / "custom-providers"

# ── Real textUtils, loaded by path (the dash in custom-providers makes it
# unimportable as a package) ──────────────────────────────────────────────
_spec = _ilu.spec_from_file_location("dotty_textUtils_cf", _CUSTOM_PROVIDERS / "textUtils.py")
assert _spec is not None and _spec.loader is not None
tu = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(tu)

REPLACEMENT = tu.CONTENT_FILTER_REPLACEMENT
FALLBACK = tu.FALLBACK_EMOJI


class TestContentFilterMatch(unittest.TestCase):

    def test_clean_text_returns_none(self):
        self.assertIsNone(tu.content_filter_match("😊 What a lovely day for a picnic."))

    def test_redirect_tier_hit(self):
        hit = tu.content_filter_match("well shit happens")
        assert hit is not None
        tier, match = hit
        self.assertEqual(tier, "redirect")
        self.assertEqual(match.group().lower(), "shit")

    def test_alert_tier_wins_over_redirect(self):
        # Both tiers present — the highest severity must be reported.
        hit = tu.content_filter_match("that shit had cocaine in it")
        assert hit is not None
        self.assertEqual(hit[0], "alert")

    def test_word_boundaries_respected(self):
        # "Scunthorpe problem" guard: no substring matches inside clean words.
        self.assertIsNone(tu.content_filter_match("the grass is greener"))


def _run(chunks, kid_mode=True, on_hit=None):
    return list(tu.filter_tts_stream(iter(chunks), kid_mode, on_hit=on_hit))


class TestFilterTtsStream(unittest.TestCase):

    def test_kid_mode_off_is_transparent(self):
        chunks = ["😊 He", "llo cocaine. ", "More text."]
        self.assertEqual(_run(chunks, kid_mode=False), chunks)

    def test_clean_stream_preserves_total_text(self):
        chunks = ["😊 Hi", " there. How are", " you today? ", "Good."]
        out = _run(chunks)
        self.assertEqual("".join(out), "".join(chunks))

    def test_hit_in_first_sentence_replaces_whole_turn(self):
        out = _run(["😊 I love coc", "aine. It is great."])
        self.assertEqual(out, [REPLACEMENT])

    def test_hit_mid_turn_keeps_earlier_sentences_and_strips_emoji(self):
        out = _run(["😊 Hello there. ", "Anyway, cocaine is fun. ", "More."])
        self.assertEqual(out[0], "😊 Hello there. ")
        self.assertEqual(len(out), 2)
        # One-emoji contract: the mid-turn replacement must not introduce a
        # second emoji.
        self.assertFalse(out[1].startswith(FALLBACK))
        self.assertIn("something fun instead", out[1])

    def test_hit_in_unterminated_tail_is_caught_on_flush(self):
        # No sentence boundary ever arrives — the flush path must still check.
        out = _run(["😊 tell me about her", "oin"])
        self.assertEqual(out, [REPLACEMENT])

    def test_blocked_term_straddling_chunks_is_caught(self):
        out = _run(["😊 fen", "tanyl is a drug. ", "Next sentence."])
        self.assertEqual(out, [REPLACEMENT])

    def test_nothing_after_hit_is_emitted(self):
        seen = []
        out = _run(["😊 cocaine. ", "shit. ", "clean tail."], on_hit=lambda t, m: seen.append(t))
        self.assertEqual(out, [REPLACEMENT])
        self.assertEqual(seen, ["alert"], "filter must stop at the first hit")

    def test_on_hit_receives_tier_and_match(self):
        seen = []
        _run(["😊 porn. "], on_hit=lambda tier, match: seen.append((tier, match.group())))
        self.assertEqual(seen, [("log", "porn")])


class TestBridgeWrapperDelegates(unittest.TestCase):
    """bridge/text.py behaviour is unchanged after the #157 extraction."""

    def setUp(self):
        sys.path.insert(0, str(_REPO_ROOT))
        import bridge.text as btext
        self.btext = btext

    def test_blocked_returns_shared_replacement_and_records_ring(self):
        before = len(self.btext.recent_content_filter_hits())
        out = self.btext.content_filter("there was cocaine somewhere")
        self.assertEqual(out, REPLACEMENT)
        hits = self.btext.recent_content_filter_hits()
        self.assertEqual(len(hits), before + 1)
        self.assertEqual(hits[0]["tier"], "alert")
        self.assertEqual(hits[0]["rule"], "cocaine")

    def test_clean_returns_none(self):
        self.assertIsNone(self.btext.content_filter("a perfectly fine sentence"))

    def test_no_duplicated_regexes_left_in_bridge(self):
        src = (_REPO_ROOT / "bridge" / "text.py").read_text(encoding="utf-8")
        self.assertNotIn("cocaine", src, "tier regexes must live only in textUtils.py")


class TestPiVoiceWiring(unittest.TestCase):
    """PiVoiceLLM.response() output passes through the shared filter."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(_CUSTOM_PROVIDERS / "pi_voice"))
        sys.path.insert(0, str(_CUSTOM_PROVIDERS))
        from pi_voice import LLMProvider  # noqa: E402
        cls.LLMProvider = LLMProvider

    class _FakeClient:
        def __init__(self, chunks):
            self._chunks = chunks

        def new_session(self):
            pass

        def iter_turn_text(self, prompt):
            yield from self._chunks

        def recent_stderr(self):
            return []

        def close(self):
            pass

    def _respond(self, chunks, kid_mode):
        env = {"DOTTY_KID_MODE": "true" if kid_mode else "false"}
        with patch.dict(os.environ, env):
            provider = self.LLMProvider({}, client=self._FakeClient(chunks))
        return list(provider.response("s", [{"role": "user", "content": "hi"}]))

    def test_kid_on_blocked_turn_replaced(self):
        out = self._respond(["😊 Sure, coc", "aine is a stimulant. ", "It acts on..."], True)
        self.assertEqual(out, [REPLACEMENT])

    def test_kid_on_clean_turn_text_preserved(self):
        chunks = ["😊 Dogs say ", "woof. Cats say meow."]
        out = self._respond(chunks, True)
        self.assertEqual("".join(out), "".join(chunks))

    def test_kid_off_passthrough(self):
        chunks = ["😊 cocaine is a stimulant."]
        self.assertEqual(self._respond(chunks, False), chunks)


class TestOpenAICompatWiring(unittest.TestCase):
    """OpenAICompat.response() output passes through the shared filter."""

    @classmethod
    def setUpClass(cls):
        # Stub the container-only imports (same discipline as
        # test_openai_compat.py: install, exec, restore) — but back the
        # textUtils stub with the REAL module so the real filter runs.
        stubbed = (
            "config", "config.logger",
            "core", "core.providers", "core.providers.llm",
            "core.providers.llm.base", "core.utils", "core.utils.textUtils",
        )
        missing = object()
        saved = {k: sys.modules.get(k, missing) for k in stubbed}

        sys.modules.setdefault("config", MagicMock())
        logger_mod = types.ModuleType("config.logger")
        logger_mod.setup_logging = lambda: MagicMock()
        sys.modules["config.logger"] = logger_mod
        for n in ("core", "core.providers", "core.providers.llm", "core.utils"):
            sys.modules.setdefault(n, MagicMock())
        base_mod = types.ModuleType("core.providers.llm.base")

        class _StubBase:
            pass

        base_mod.LLMProviderBase = _StubBase
        sys.modules["core.providers.llm.base"] = base_mod
        sys.modules["core.utils.textUtils"] = tu

        try:
            with patch.dict(os.environ, {"DOTTY_KID_MODE": "true"}):
                spec = _ilu.spec_from_file_location(
                    "openai_compat_cf_test",
                    _CUSTOM_PROVIDERS / "openai_compat" / "openai_compat.py",
                )
                assert spec is not None and spec.loader is not None
                cls.mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(cls.mod)
        finally:
            for k, v in saved.items():
                if v is missing:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def _respond(self, contents, kid_mode=True):
        import json as _json
        lines = [
            "data: " + _json.dumps({"choices": [{"delta": {"content": c}}]})
            for c in contents
        ] + ["data: [DONE]"]
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.iter_lines = lambda decode_unicode=True: iter(lines)
        provider = self.mod.LLMProvider({"url": "http://x/v1", "model": "m"})
        with patch.object(self.mod, "KID_MODE", kid_mode), \
             patch.object(self.mod.requests, "post", return_value=resp):
            return list(provider.response("s", [{"role": "user", "content": "hi"}]))

    def test_kid_on_blocked_turn_replaced(self):
        out = self._respond(["😊 I love coc", "aine. It is great."])
        self.assertEqual(out, [REPLACEMENT])

    def test_kid_on_clean_turn_text_preserved(self):
        out = self._respond(["😊 Hi", " there. All good."])
        self.assertEqual("".join(out), "😊 Hi there. All good.")

    def test_kid_off_passthrough_unfiltered(self):
        out = self._respond(["😊 cocaine. ", "More."], kid_mode=False)
        self.assertEqual("".join(out), "😊 cocaine. More.")

    def test_emoji_fallback_survives_filter(self):
        # No emoji from the model: _response_stream prepends the fallback,
        # the filter must keep it as the leading glyph.
        out = self._respond(["Hello there. "])
        self.assertTrue("".join(out).startswith(FALLBACK))


if __name__ == "__main__":
    unittest.main()
