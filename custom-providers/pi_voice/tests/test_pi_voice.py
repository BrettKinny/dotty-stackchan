"""Unit tests for PiVoiceLLM — the xiaozhi LLMProvider subclass.

Focus: prompt construction (last-user extraction + sandwich injection),
first-turn / nth-turn lifecycle, error fallback path. Live pi not
required — uses a fake PiClient.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDER_DIR = os.path.dirname(HERE)
CUSTOM_PROVIDERS_DIR = os.path.dirname(PROVIDER_DIR)
sys.path.insert(0, PROVIDER_DIR)
sys.path.insert(0, CUSTOM_PROVIDERS_DIR)

import textUtils  # noqa: E402
# Import via the pi_voice package, not the top-level pi_client module —
# pi_voice catches pi_voice.pi_client.PiClientError, and `from pi_client
# import PiClientError` would give us a *different* class object even
# though the source is identical, so isinstance/except wouldn't match.
from pi_voice import (  # noqa: E402
    LLMProvider,
    PiClientError,
    _wrap_with_sandwich,
)
from pi_voice.pi_voice import _last_user_text  # noqa: E402


class FakeClient:
    """Stand-in for PiClient. Captures prompts; lets tests script the
    text-delta sequence + error injection."""

    def __init__(self):
        self.prompts: list[str] = []
        self.new_session_calls = 0
        self.scripted_chunks: list[list[str]] = []
        self.scripted_errors: list[BaseException | None] = []
        self.closed = False

    def script_turn(self, chunks: list[str], error: BaseException | None = None) -> None:
        self.scripted_chunks.append(chunks)
        self.scripted_errors.append(error)

    def new_session(self) -> None:
        self.new_session_calls += 1

    def iter_turn_text(self, prompt: str) -> Iterator[str]:
        self.prompts.append(prompt)
        chunks = self.scripted_chunks.pop(0) if self.scripted_chunks else []
        err = self.scripted_errors.pop(0) if self.scripted_errors else None
        if err is not None:
            raise err
        for c in chunks:
            yield c

    def recent_stderr(self) -> list[str]:
        return []

    def close(self) -> None:
        self.closed = True


class TestSandwichInjection(unittest.TestCase):
    def test_suffix_appended_kid_mode_on(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn(["😊 ", "Hi"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("sess-1", [{"role": "user", "content": "Hello"}]))
        self.assertEqual(len(client.prompts), 1)
        self.assertTrue(client.prompts[0].startswith("Hello\n\nVOICE TOOL ROUTING:"))
        self.assertTrue(client.prompts[0].endswith(textUtils.build_turn_suffix(True)))
        # Sanity: the kid-mode-specific bullets must be in the suffix.
        self.assertIn("YOUNG CHILD", client.prompts[0])
        self.assertIn("SELF-HARM EXCEPTION", client.prompts[0])

    def test_suffix_appended_kid_mode_off(self):
        os.environ["DOTTY_KID_MODE"] = "false"
        client = FakeClient()
        client.script_turn(["😐 OK"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("sess-1", [{"role": "user", "content": "Hi"}]))
        self.assertTrue(client.prompts[0].startswith("Hi\n\nVOICE TOOL ROUTING:"))
        self.assertTrue(client.prompts[0].endswith(textUtils.build_turn_suffix(False)))
        # Adult mode: still has emoji-prefix / English-only / no-Markdown
        # bullets, but NOT the kid-specific ones.
        self.assertIn("EXACTLY ONE emoji", client.prompts[0])
        self.assertNotIn("YOUNG CHILD", client.prompts[0])

    def test_wrap_helper_pure(self):
        # Tool routing must precede the final spoken-output constraints.
        wrapped = _wrap_with_sandwich("hi", True)
        self.assertTrue(wrapped.startswith("hi"))
        self.assertLess(wrapped.index("VOICE TOOL ROUTING"), wrapped.index("HARD CONSTRAINTS"))
        self.assertIn("not to tool calls", wrapped)

    def test_json_wrapped_user_content_is_unwrapped(self):
        dialogue = [{"role": "user", "content": '{"content": "remember purple"}'}]
        self.assertEqual(_last_user_text(dialogue), "remember purple")

    def test_mapping_wrapped_user_content_is_unwrapped(self):
        dialogue = [{"role": "user", "content": {"content": "think carefully"}}]
        self.assertEqual(_last_user_text(dialogue), "think carefully")

    def test_unknown_or_invalid_json_text_is_preserved(self):
        for content in ('{"question": "why"}', "{not json}"):
            self.assertEqual(
                _last_user_text([{"role": "user", "content": content}]),
                content,
            )

    def test_shared_state_file_refreshes_kid_mode_each_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "kid-mode"
            state_file.write_text("true")
            old_path = os.environ.get("DOTTY_KID_MODE_STATE")
            os.environ["DOTTY_KID_MODE_STATE"] = str(state_file)
            self.addCleanup(
                lambda: (
                    os.environ.__setitem__("DOTTY_KID_MODE_STATE", old_path)
                    if old_path is not None
                    else os.environ.pop("DOTTY_KID_MODE_STATE", None)
                )
            )

            client = FakeClient()
            client.script_turn(["😊 first"])
            client.script_turn(["😐 second"])
            provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
            list(provider.response("s", [{"role": "user", "content": "one"}]))

            state_file.write_text("false")
            list(provider.response("s", [{"role": "user", "content": "two"}]))

            self.assertIn("YOUNG CHILD", client.prompts[0])
            self.assertNotIn("YOUNG CHILD", client.prompts[1])

    def test_malformed_shared_state_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "kid-mode"
            state_file.write_text("not-a-boolean")
            old_path = os.environ.get("DOTTY_KID_MODE_STATE")
            old_mode = os.environ.get("DOTTY_KID_MODE")
            os.environ["DOTTY_KID_MODE_STATE"] = str(state_file)
            os.environ["DOTTY_KID_MODE"] = "false"

            def restore_env() -> None:
                for name, value in (
                    ("DOTTY_KID_MODE_STATE", old_path),
                    ("DOTTY_KID_MODE", old_mode),
                ):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            self.addCleanup(restore_env)
            client = FakeClient()
            client.script_turn(["😐 adult mode"])
            provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
            list(provider.response("s", [{"role": "user", "content": "hello"}]))

            self.assertNotIn("YOUNG CHILD", client.prompts[0])


class TestEmptyTurn(unittest.TestCase):
    def test_no_user_message_short_circuits(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        out = list(provider.response("sess-1", [{"role": "system", "content": "..."}]))
        self.assertEqual(out, [f"{textUtils.FALLBACK_EMOJI} (empty turn)"])
        self.assertEqual(client.prompts, [], "PiClient must not be called for empty dialogue")


class TestNewSessionLifecycle(unittest.TestCase):
    def test_first_turn_skips_new_session(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn(["ok"])
        client.script_turn(["ok"])
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        list(provider.response("s", [{"role": "user", "content": "a"}]))
        self.assertEqual(client.new_session_calls, 0, "no new_session on first turn")
        list(provider.response("s", [{"role": "user", "content": "b"}]))
        self.assertEqual(client.new_session_calls, 1, "new_session on second turn")

    def test_concurrent_responses_are_serialized_through_agent_end(self):
        class OverlapDetectingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                self.first_started = threading.Event()
                self.release_first = threading.Event()

            def iter_turn_text(self, prompt: str) -> Iterator[str]:
                self.prompts.append(prompt)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    if len(self.prompts) == 1:
                        self.first_started.set()
                        self.release_first.wait(timeout=2)
                    yield "😊 ok"
                finally:
                    self.active -= 1

        os.environ["DOTTY_KID_MODE"] = "false"
        client = OverlapDetectingClient()
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        outputs: list[list[str]] = []

        def run(text: str) -> None:
            outputs.append(list(provider.response(
                "s", [{"role": "user", "content": text}],
            )))

        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        first.start()
        self.assertTrue(client.first_started.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        self.assertEqual(len(client.prompts), 1, "second turn must wait")
        client.release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(client.max_active, 1)
        self.assertEqual(len(outputs), 2)
        self.assertEqual(client.new_session_calls, 1)


class TestErrorFallback(unittest.TestCase):
    def test_client_error_yields_fallback(self):
        os.environ["DOTTY_KID_MODE"] = "true"
        client = FakeClient()
        client.script_turn([], error=PiClientError("pi crashed"))
        provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
        out = list(provider.response("s", [{"role": "user", "content": "anything"}]))
        self.assertEqual(out, [f"{textUtils.FALLBACK_EMOJI} (brain offline — try again in a moment)"])


class TestLeadingEmojiContract(unittest.TestCase):
    def _response(self, chunks: list[str], *, kid_mode: bool = False) -> list[str]:
        env = {
            "DOTTY_KID_MODE": "true" if kid_mode else "false",
            "DOTTY_KID_MODE_STATE": "/nonexistent/dotty-test-kid-mode",
        }
        with patch.dict(os.environ, env):
            client = FakeClient()
            client.script_turn(chunks)
            provider = LLMProvider({}, client=client)  # type: ignore[arg-type]
            return list(provider.response("s", [{"role": "user", "content": "hello"}]))

    def test_missing_emoji_gets_fallback_before_first_text(self):
        out = self._response(["Hello", " there"])
        self.assertEqual(out[0], f"{textUtils.FALLBACK_EMOJI} ")
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello there")

    def test_leading_whitespace_never_precedes_emoji(self):
        out = self._response(["  ", "Hello"])
        self.assertTrue(out[0].startswith(textUtils.FALLBACK_EMOJI))
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello")

    def test_allowed_emoji_is_not_double_prefixed(self):
        out = self._response(["😊 Hello"])
        self.assertEqual(out, ["😊 Hello"])

    def test_disallowed_leading_emoji_is_replaced_not_retained(self):
        out = self._response(["❤️ Hello"])
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello")

    def test_disallowed_single_codepoint_emoji_is_replaced(self):
        out = self._response(["😂 Hello"])
        self.assertEqual("".join(out), f"{textUtils.FALLBACK_EMOJI} Hello")

    def test_empty_model_stream_gets_emoji_fallback(self):
        out = self._response([])
        self.assertEqual(out, [f"{textUtils.FALLBACK_EMOJI} (no response)"])

    def test_kid_filter_still_replaces_the_complete_turn(self):
        out = self._response(["Hello ", "cocaine"], kid_mode=True)
        self.assertEqual(out, [textUtils.CONTENT_FILTER_REPLACEMENT])


if __name__ == "__main__":
    unittest.main()
