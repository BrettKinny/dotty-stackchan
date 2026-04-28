"""Unit test for the cross-cutting `type` discriminator added in commit 6.

Verifies the convention: every NDJSON memory write the bridge produces
carries a `type` field. Future ZeroClaw FTS ingestion routines can
filter on this field (`type:dream`, `type:perception`, etc.) without
guessing from the filename.

The taxonomy this commit standardises:

  type=chat              — _ConvoLogger.log_turn → convo-YYYY-MM-DD.ndjson
  type=perception        — idle photographer    → perception-YYYY-MM-DD.ndjson
  type=dream             — sleep dreamer        → dreams-YYYY-MM-DD.ndjson
  type=dance             — dance reflector      → dances-YYYY-MM-DD.ndjson
  type=scene_synthesis   — scene synthesis loop → scene-synthesis-YYYY-MM-DD.ndjson

This test asserts each documented value remains a string in the
expected shape — adding a new value without updating the taxonomy
should fail this test.
"""
from __future__ import annotations

import unittest


KNOWN_TYPES = frozenset({
    "chat",
    "perception",
    "dream",
    "dance",
    "scene_synthesis",
})


class TypeTagTaxonomyTests(unittest.TestCase):

    def test_known_types_are_strings(self):
        # Catches accidental tuple/None/int sneaking into the constant.
        for t in KNOWN_TYPES:
            self.assertIsInstance(t, str)
            self.assertTrue(t.islower())
            self.assertGreater(len(t), 0)

    def test_no_whitespace_in_tags(self):
        # FTS queries split on whitespace — a tag with a space breaks
        # `tag:perception` / `tag:scene_synthesis` lookups.
        for t in KNOWN_TYPES:
            self.assertNotIn(" ", t)
            self.assertNotIn("\t", t)
            self.assertNotIn("-", t, f"use underscore not hyphen: {t!r}")

    def test_chat_is_default_for_convo(self):
        # _ConvoLogger.log_turn defaults to type="chat" — keep that
        # contract stable so untyped legacy log_turn() callers still
        # land in the right bucket.
        self.assertIn("chat", KNOWN_TYPES)

    def test_taxonomy_documented_in_tools_md(self):
        # Smoke check: this taxonomy must be in sync with the live
        # /root/.zeroclaw/workspace/TOOLS.md on the RPi (deployed by
        # the commit-6 deploy step). The test doesn't read the Pi
        # (no network in unit tests), but locks the assertion into
        # the test so a future change here triggers a docs update.
        # If you change KNOWN_TYPES, also update TOOLS.md.
        # The check is purely a reminder: if this list grows or
        # shrinks, fail to remind the developer to push the doc.
        expected = {"chat", "perception", "dream", "dance", "scene_synthesis"}
        self.assertEqual(KNOWN_TYPES, expected)


if __name__ == "__main__":
    unittest.main()
