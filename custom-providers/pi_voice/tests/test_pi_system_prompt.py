"""The live voice path must give pi a Dotty identity (#177).

AI-assisted (Claude). Without `--system-prompt`, pi falls back to its built-in
coding-assistant prompt and the robot introduces itself as one.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pi_client  # noqa: E402


def _spawned_args(env: dict[str, str]) -> list[str]:
    captured: dict[str, list[str]] = {}

    def fake_factory(container, pi_args):
        captured["args"] = list(pi_args)
        raise RuntimeError("stop before spawning")

    with patch.dict(os.environ, env, clear=False), \
         patch.object(pi_client, "local_exec_subprocess_factory", fake_factory):
        client = pi_client.make_default_pi_client()
        try:
            client._spawn()
        except RuntimeError:
            pass
    return captured["args"]


class SystemPromptTests(unittest.TestCase):
    def test_persona_file_is_passed_as_system_prompt(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("You are Dotty, a small desktop robot.\n")
        self.addCleanup(os.unlink, handle.name)
        args = _spawned_args({"DOTTY_PI_SYSTEM_PROMPT_FILE": handle.name})
        self.assertIn("--system-prompt", args)
        self.assertEqual(args[args.index("--system-prompt") + 1],
                         "You are Dotty, a small desktop robot.")

    def test_missing_or_empty_file_keeps_previous_behaviour(self):
        args = _spawned_args({"DOTTY_PI_SYSTEM_PROMPT_FILE": "/nonexistent/persona.md"})
        self.assertNotIn("--system-prompt", args)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("  \n")
        self.addCleanup(os.unlink, handle.name)
        self.assertNotIn("--system-prompt",
                         _spawned_args({"DOTTY_PI_SYSTEM_PROMPT_FILE": handle.name}))

    def test_shipped_voice_persona_names_dotty_and_not_a_coding_assistant(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        text = open(os.path.join(repo, "personas", "pi_voice.md"), encoding="utf-8").read()
        self.assertIn("You are Dotty", text)
        self.assertNotIn("[REMEMBER", text)
        self.assertLess(len(text), 2500)


if __name__ == "__main__":
    unittest.main()
