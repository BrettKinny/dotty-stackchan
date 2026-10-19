"""Voice output safety: the kid-mode filter and the leading-face contract.

AI-assisted (Claude Opus 5.5). Covers #157/#138 (fail-closed blocked-content
filter on streamed chunks), #178 (imitable-danger imagery and the kid-mode
prompt) and #168 (exactly one supported leading emoji, near-miss mapping).
Everything here runs offline against the shared textUtils module.
"""
from __future__ import annotations

import importlib.util as _ilu
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = _ilu.spec_from_file_location(
    "dotty_textUtils_safety", _REPO_ROOT / "custom-providers" / "textUtils.py",
)
assert _spec is not None and _spec.loader is not None
tu = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(tu)

REPLACEMENT = tu.CONTENT_FILTER_REPLACEMENT


def _filtered(chunks, kid_mode=True, on_hit=None):
    return list(tu.filter_tts_stream(iter(chunks), kid_mode, on_hit=on_hit))


class TestDangerTier(unittest.TestCase):
    """#178: replies a 4-8 year old could copy and get hurt by."""

    UNSAFE = [
        # The reply recorded on the robot in the #178 bench session.
        "😊 I love zooming up to the ceiling fan and dancing around the blades!",
        "😆 I like to climb up the bookshelf and jump off the top!",
        "😊 Let's poke a fork into the power socket.",
        "😊 We could play with matches!",
        "😊 Touch the hot stove to see what happens!",
        "😊 I swallowed a battery once, yum.",
        "😊 Jump off the roof like a superhero!",
        "😊 Put a plastic bag over your head.",
        "😊 Then I hang from the ceiling fan.",
        "😊 Let's light a fire in the bedroom!",
    ]
    SAFE = [
        "😊 A fan spins its blades to push air around the room.",
        "😊 I danced through the blades of grass.",
        "😊 The ceiling fan keeps us cool.",
        "😊 Rollerblades are fun!",
        "😊 Fire trucks are red and loud!",
        "😊 Rocket ships fly to the moon.",
        "😊 I like to climb trees at the park.",
        "😊 Look out the window at the birds!",
        "😊 Birthday candles are fun to blow out.",
        "😊 Batteries give me energy!",
        "😊 Why did the tiny robot cross the road? To get to the other byte!",
        # Safety advice names the hazard but must still be spoken.
        "😊 Never touch the hot stove, it can burn you.",
        "😊 Ask a grown-up before you use scissors.",
        "😊 Don't put anything in a power socket.",
        # The self-harm exception must never be swallowed by the filter.
        "😢 I'm sorry you feel sad. Please talk to a grown-up you trust.",
    ]

    def test_unsafe_imagery_is_blocked(self):
        for text in self.UNSAFE:
            with self.subTest(text=text):
                hit = tu.content_filter_match(text)
                self.assertIsNotNone(hit)
                assert hit is not None
                self.assertEqual(hit[0], "danger")

    def test_benign_and_safety_advice_pass(self):
        for text in self.SAFE:
            with self.subTest(text=text):
                self.assertIsNone(tu.content_filter_match(text))

    def test_safety_cue_only_exempts_its_own_sentence(self):
        text = "😊 Never touch the oven. But I love dancing around the fan blades!"
        hit = tu.content_filter_match(text)
        assert hit is not None
        self.assertEqual(hit[0], "danger")

    def test_safety_cue_never_exempts_other_tiers(self):
        self.assertIsNotNone(tu.content_filter_match("😊 Never say shit."))

    def test_danger_phrase_split_across_chunks_replaces_turn(self):
        out = _filtered(["😊 I love zooming up to the cei", "ling fan and dan",
                         "cing around the bla", "des!"])
        self.assertEqual(out, [REPLACEMENT])

    def test_graphic_violence_words_are_blocked(self):
        for word in ("murdered", "beheaded", "stabbing", "massacre"):
            with self.subTest(word=word):
                hit = tu.content_filter_match(f"😊 The knight {word} them.")
                assert hit is not None
                self.assertEqual(hit[0], "log")


class TestFilterFailsClosed(unittest.TestCase):

    def test_matcher_error_replaces_turn(self):
        original = tu.content_filter_match

        def broken(_text):
            raise RuntimeError("regex engine exploded")

        tu.content_filter_match = broken
        try:
            out = _filtered(["😊 Hello there."])
        finally:
            tu.content_filter_match = original
        self.assertEqual(out, [REPLACEMENT])

    def test_on_hit_error_still_replaces_turn(self):
        def boom(tier, match):
            raise RuntimeError("dashboard down")

        self.assertEqual(_filtered(["😊 cocaine."], on_hit=boom), [REPLACEMENT])

    def test_source_error_emits_nothing_buffered(self):
        emitted = []

        def source():
            yield "😊 This text was never checked "
            raise RuntimeError("stream died")

        with self.assertRaises(RuntimeError):
            for chunk in tu.filter_tts_stream(source(), True):
                emitted.append(chunk)
        self.assertEqual(emitted, [])

    def test_kid_mode_off_is_still_passthrough(self):
        self.assertEqual(_filtered(["😊 cocaine"], kid_mode=False), ["😊 cocaine"])


class TestProviderText(unittest.TestCase):
    """Provider-authored phrases may stream ahead of the full-turn check."""

    def test_provider_text_is_released_before_the_rest_of_the_turn(self):
        seen = []

        def source():
            yield tu.ProviderText("🤔 Let me think hard about that. ")
            seen.append("source resumed")
            yield "The answer is forty-two."

        stream = tu.filter_tts_stream(source(), True)
        self.assertEqual(next(stream), "🤔 Let me think hard about that. ")
        self.assertEqual(seen, [], "provider text must not wait for the model")
        self.assertEqual(list(stream), ["The answer is forty-two."])

    def test_model_text_after_provider_text_is_still_filtered(self):
        out = _filtered([tu.ProviderText("🤔 Let me think. "), "cocaine is fun."])
        self.assertEqual(out[0], "🤔 Let me think. ")
        # The turn already carries its face; the replacement drops its own.
        self.assertEqual(out[1:], [REPLACEMENT[len(tu.FALLBACK_EMOJI):].lstrip()])

    def test_provider_text_waits_behind_unchecked_model_text(self):
        out = _filtered(["😊 model text ", tu.ProviderText("then a constant.")])
        self.assertEqual("".join(out), "😊 model text then a constant.")

    def test_blocked_provider_text_is_replaced(self):
        self.assertEqual(_filtered([tu.ProviderText("😊 cocaine")]), [REPLACEMENT])


if __name__ == "__main__":
    unittest.main()
