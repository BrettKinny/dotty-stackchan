"""Real ASR-to-state routing regressions; AI-assisted by OpenAI Codex (GPT-6).

Container imports are isolated by the existing fixture module. Only external
intent/STT/MCP boundaries and the executor are faked; routing remains real.
"""

import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_asr_name_corrections import _module


def route(text, state="idle"):
    conn = types.SimpleNamespace(
        logger=MagicMock(), need_bind=False, max_output_size=0,
        client_is_speaking=False, current_state=state, session_id="fixture",
        websocket=types.SimpleNamespace(send=AsyncMock()),
        executor=MagicMock(), chat=MagicMock(),
        _dotty_toggles_synced=True,
    )
    commands = AsyncMock()
    with patch.object(_module, "handle_user_intent", new=AsyncMock(return_value=False)), \
         patch.object(_module, "send_stt_message", new=AsyncMock()), \
         patch.object(_module, "_mcp_call_tool", new=commands):
        asyncio.run(_module.startToChat(conn, json.dumps({"content": text})))
    states = [call.args[2]["state"] for call in commands.await_args_list
              if call.args[1] == "self.robot.set_state"]
    return states, conn.executor.submit.call_args.args[1]


def test_explicit_do_not_sleep_is_conversation_not_state_command():
    states, prompt = route("Do not go to sleep.")
    assert states == []
    assert prompt == "Do not go to sleep."


@pytest.mark.parametrize("negation", ["Do not", "Don't", "Don’t", "Never"])
@pytest.mark.parametrize("command", ["go to sleep", "tell me a story", "keep watch"])
def test_explicit_negated_state_commands_do_not_dispatch(negation, command):
    states, prompt = route(f"Please {negation} {command}.")
    assert states == []
    assert negation in prompt


@pytest.mark.parametrize("quote", [('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’')])
def test_quoted_story_command_is_a_mention_not_a_state_change(quote):
    text = f"What does {quote[0]}tell me a story{quote[1]} mean?"
    states, prompt = route(text)
    assert states == []
    assert prompt == text


@pytest.mark.parametrize("text, expected", [
    ("Goodnight Dotty.", "sleep"),
    ("Good night, Dotty.", "sleep"),
    ("Please go to sleep.", "sleep"),
    ("Please keep watch.", "security"),
    ("Security mode.", "security"),
    ("Watch the room.", "security"),
    ("Please tell me a story.", "story_time"),
    ("Story time.", "story_time"),
    ("Don't keep watch. Tell me a story.", "story_time"),
    ("Don't keep watch, but tell me a story.", "story_time"),
    ('The phrase "never" is interesting. Please go to sleep.', "sleep"),
])
def test_affirmative_state_commands_still_dispatch(text, expected):
    states, _ = route(text)
    assert states == [expected]


@pytest.mark.parametrize("state", ["sleep", "security", "story_time"])
@pytest.mark.parametrize("text", ["Wake up.", "Come back.", "Are you there?",
                                  "Don't go to sleep, wake up."])
def test_affirmative_wake_escape_still_dispatches_idle(state, text):
    states, _ = route(text, state)
    assert states == ["idle"]


@pytest.mark.parametrize("text", [
    'What does "wake up" mean?', "Don't wake up.", "Never come back.",
])
def test_quoted_or_negated_wake_is_not_an_escape_command(text):
    states, _ = route(text, "story_time")
    assert states == []


@pytest.mark.parametrize("text", [
    "Please don't ever go to sleep.",
    "Never, please tell me a story.",
    "Don't you go to sleep.",
    "Do not, tell me a story.",
    'Say "go to sleep" out loud.',
    'Explain “security mode” to me.',
    "What does 'don't go to sleep' mean?",
    "What's the meaning of 'tell me a story'?",
    "Tell me. A story is something I dislike.",
    "Explain the story timetable.",
])
def test_punctuation_and_mentions_do_not_manufacture_state_commands(text):
    states, _ = route(text)
    assert states == []


def test_unrelated_contraction_does_not_hide_an_affirmative_command():
    states, _ = route("I'm ready, please tell me a story.")
    assert states == ["story_time"]
