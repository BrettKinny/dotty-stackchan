"""Unit tests for zeroclaw _load_persona_prompt -- persona hot-swap.

Pure unit -- uses importlib to load zeroclaw.py directly from its source
path, mocking the xiaozhi-server packages (config.logger,
core.providers.llm.base) that are only available inside the container.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


def _import_zeroclaw():
    """Import zeroclaw.py with xiaozhi-server internal deps mocked out."""
    mock_logger_mod = MagicMock()
    mock_logger_mod.setup_logging.return_value = MagicMock()
    for pkg in (
        "config",
        "config.logger",
        "core",
        "core.providers",
        "core.providers.llm",
        "core.providers.llm.base",
    ):
        sys.modules.setdefault(pkg, MagicMock())
    sys.modules["config.logger"] = mock_logger_mod

    path = (
        Path(__file__).resolve().parents[1]
        / "custom-providers"
        / "zeroclaw"
        / "zeroclaw.py"
    )
    spec = importlib.util.spec_from_file_location("zeroclaw_provider", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _import_zeroclaw()
_load_persona_prompt = _mod._load_persona_prompt


class LoadPersonaPromptTests(unittest.TestCase):
    """_load_persona_prompt reads PERSONA env var and loads the persona file."""

    def setUp(self) -> None:
        os.environ.pop("PERSONA", None)
        os.environ.pop("PERSONA_DIR", None)

    def tearDown(self) -> None:
        os.environ.pop("PERSONA", None)
        os.environ.pop("PERSONA_DIR", None)

    def test_returns_empty_when_persona_not_set(self):
        self.assertEqual(_load_persona_prompt(), "")

    def test_returns_empty_when_persona_empty_string(self):
        os.environ["PERSONA"] = ""
        self.assertEqual(_load_persona_prompt(), "")

    def test_returns_empty_when_persona_whitespace_only(self):
        os.environ["PERSONA"] = "   "
        self.assertEqual(_load_persona_prompt(), "")

    def test_returns_empty_when_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["PERSONA"] = "nonexistent_persona_xyz"
            os.environ["PERSONA_DIR"] = tmpdir
            result = _load_persona_prompt()
        self.assertEqual(result, "")

    def test_loads_persona_file_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test_persona.md").write_text(
                "You are a test robot.", encoding="utf-8"
            )
            os.environ["PERSONA"] = "test_persona"
            os.environ["PERSONA_DIR"] = tmpdir
            result = _load_persona_prompt()
        self.assertEqual(result, "You are a test robot.")

    def test_strips_leading_trailing_whitespace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "padded.md").write_text(
                "\n\nYou are a robot.\n\n", encoding="utf-8"
            )
            os.environ["PERSONA"] = "padded"
            os.environ["PERSONA_DIR"] = tmpdir
            result = _load_persona_prompt()
        self.assertEqual(result, "You are a robot.")

    def test_persona_dir_override_via_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "custom.md").write_text(
                "Custom persona.", encoding="utf-8"
            )
            os.environ["PERSONA"] = "custom"
            os.environ["PERSONA_DIR"] = tmpdir
            result = _load_persona_prompt()
        self.assertEqual(result, "Custom persona.")

    def test_multiline_persona_preserved(self):
        content = "# Persona\n\nYou are Dotty.\nKeep replies short."
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "dotty.md").write_text(content, encoding="utf-8")
            os.environ["PERSONA"] = "dotty"
            os.environ["PERSONA_DIR"] = tmpdir
            result = _load_persona_prompt()
        self.assertEqual(result, content)

    def test_returns_empty_on_unreadable_dir(self):
        os.environ["PERSONA"] = "any"
        os.environ["PERSONA_DIR"] = "/nonexistent_dir_abc123/personas"
        self.assertEqual(_load_persona_prompt(), "")

    def test_returns_empty_when_no_persona_dir_and_no_base(self):
        # In test environments _PERSONAS_BASE is None (shallow __file__ path).
        # Verify graceful empty return rather than AttributeError.
        os.environ["PERSONA"] = "default"
        # PERSONA_DIR intentionally not set; _PERSONAS_BASE is None in test env.
        result = _load_persona_prompt()
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
