"""Unit tests for PiClient's 3 Step-5 invariants. No live pi required —
a fake subprocess pumps frames through stdout and captures stdin writes.

Run from the dotty-stackchan repo root:
    python3 -m pytest custom-providers/pi_voice/tests/ -v

Or with unittest:
    python3 -m unittest custom-providers.pi_voice.tests.test_pi_client -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import unittest

# Make the package importable as `pi_voice.*` regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pi_client import PiClient, PiClientError  # noqa: E402


class FakePopen:
    """Minimal subprocess.Popen stand-in. The test pushes JSON frames
    into `stdout_queue` via `emit()`, and stdin writes from PiClient
    land in `stdin_lines` for assertions."""

    def __init__(self):
        self._stdout_buf = io.BytesIO()
        self._stdout_cond = threading.Condition()
        self._stdout_pos = 0
        self._closed = False
        self.stdin_lines: list[dict] = []
        self.stdin = self  # PiClient writes to self.stdin
        self.stderr = io.BytesIO()  # never written in tests

    @property
    def stdout(self):
        return self

    # ------ stdout side: PiClient calls .readline() ----------------

    def readline(self) -> bytes:
        with self._stdout_cond:
            while True:
                self._stdout_buf.seek(self._stdout_pos)
                line = self._stdout_buf.readline()
                if line:
                    self._stdout_pos = self._stdout_buf.tell()
                    return line
                if self._closed:
                    return b""
                self._stdout_cond.wait(timeout=0.5)

    # ------ stdin side: PiClient calls .write() / .flush() ---------

    def write(self, data: bytes) -> int:
        line = data.decode("utf-8").rstrip("\n")
        if line:
            self.stdin_lines.append(json.loads(line))
        return len(data)

    def flush(self):
        pass

    # ------ lifecycle hooks PiClient pokes at -----------------------

    def poll(self):
        return None if not self._closed else 0

    def terminate(self):
        self._closed = True
        with self._stdout_cond:
            self._stdout_cond.notify_all()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return 0

    # ------ test-side helper ---------------------------------------

    def emit(self, frame: dict | str):
        """Push a frame onto pi's stdout. `dict` is JSON-encoded;
        `str` is sent raw (lets a test exercise the json-decode path)."""
        with self._stdout_cond:
            payload = json.dumps(frame) if isinstance(frame, dict) else frame
            self._stdout_buf.seek(0, io.SEEK_END)
            self._stdout_buf.write((payload + "\n").encode("utf-8"))
            self._stdout_cond.notify_all()


def make_client(fake: FakePopen) -> PiClient:
    """PiClient wired to spawn `fake` exactly once."""
    spawned = [0]

    def factory():
        spawned[0] += 1
        return fake

    client = PiClient(factory, turn_timeout_sec=3.0)
    client._spawn_count = spawned  # expose for assertions
    return client


def _auto_respond(fake: FakePopen, *, turn_text: str) -> threading.Thread:
    """Watch fake.stdin_lines for the next prompt command, then echo back
    a matching accept-ack + text_delta + agent_end. Returns the watcher
    thread so the caller can join() it."""
    seen_count = len(fake.stdin_lines)

    def watch():
        while True:
            time.sleep(0.01)
            if len(fake.stdin_lines) > seen_count:
                for cmd in fake.stdin_lines[seen_count:]:
                    if cmd.get("type") == "prompt":
                        fake.emit({
                            "id": cmd["id"], "type": "response",
                            "command": "prompt", "success": True,
                        })
                        fake.emit({
                            "type": "message_update",
                            "assistantMessageEvent": {
                                "type": "text_delta", "delta": turn_text,
                            },
                        })
                        fake.emit({"type": "agent_end"})
                        return

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    return t


class TestSpawnOnce(unittest.TestCase):
    def test_multiple_turns_reuse_one_process(self):
        fake = FakePopen()
        client = make_client(fake)
        try:
            # First turn — id-aware auto-responder watches stdin.
            _auto_respond(fake, turn_text="hi")
            chunks = list(client.iter_turn_text("first"))
            self.assertEqual(chunks, ["hi"])

            # new_session ack — same pattern.
            def ns_responder():
                while True:
                    time.sleep(0.01)
                    for cmd in fake.stdin_lines:
                        if cmd.get("type") == "new_session":
                            fake.emit({
                                "id": cmd["id"],
                                "type": "response", "command": "new_session",
                                "success": True,
                            })
                            return
            threading.Thread(target=ns_responder, daemon=True).start()
            client.new_session()

            # Second turn — same auto-responder pattern, no shared state issues.
            _auto_respond(fake, turn_text="bye")
            chunks = list(client.iter_turn_text("second"))
            self.assertEqual(chunks, ["bye"])

            self.assertEqual(client._spawn_count[0], 1, "must only spawn once")
        finally:
            client.close()

    def test_new_session_ignores_stale_response_until_matching_ack(self):
        fake = FakePopen()
        client = make_client(fake)
        stale_emitted = threading.Event()
        release_matching = threading.Event()
        errors: list[BaseException] = []

        def responder():
            while True:
                time.sleep(0.01)
                for cmd in fake.stdin_lines:
                    if cmd.get("type") == "new_session":
                        fake.emit({
                            "id": "nsess-stale",
                            "type": "response",
                            "command": "new_session",
                            "success": False,
                            "error": "stale failure",
                        })
                        stale_emitted.set()
                        release_matching.wait(timeout=2)
                        fake.emit({
                            "id": cmd["id"],
                            "type": "response",
                            "command": "new_session",
                            "success": True,
                        })
                        return

        def reset_session():
            try:
                client.new_session()
            except BaseException as exc:  # captured for the main test thread
                errors.append(exc)

        threading.Thread(target=responder, daemon=True).start()
        reset = threading.Thread(target=reset_session)
        reset.start()
        try:
            self.assertTrue(stale_emitted.wait(timeout=1))
            time.sleep(0.05)
            self.assertTrue(reset.is_alive(), "stale response must not complete reset")
            release_matching.set()
            reset.join(timeout=2)
            self.assertFalse(reset.is_alive())
            self.assertEqual(errors, [])
        finally:
            release_matching.set()
            reset.join(timeout=2)
            client.close()

    def test_new_session_raises_on_matching_failure(self):
        fake = FakePopen()
        client = make_client(fake)

        def responder():
            while True:
                time.sleep(0.01)
                for cmd in fake.stdin_lines:
                    if cmd.get("type") == "new_session":
                        fake.emit({
                            "id": cmd["id"],
                            "type": "response",
                            "command": "new_session",
                            "success": False,
                            "error": "reset refused",
                        })
                        return

        threading.Thread(target=responder, daemon=True).start()
        try:
            with self.assertRaisesRegex(PiClientError, "reset refused"):
                client.new_session()
        finally:
            client.close()


class TestThinkingFilter(unittest.TestCase):
    def test_thinking_deltas_are_dropped(self):
        fake = FakePopen()
        client = make_client(fake)
        try:
            def feed():
                fake.emit({
                    "id": "turn-1", "type": "response",
                    "command": "prompt", "success": True,
                })
                # Interleave thinking + text; only text should reach the iterator.
                for delta in ["<think>", "reasoning ", "more </think>"]:
                    fake.emit({
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "thinking_delta", "delta": delta,
                        },
                    })
                fake.emit({
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_start"},
                })
                fake.emit({
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
                })
                fake.emit({
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": " world."},
                })
                fake.emit({"type": "agent_end"})

            threading.Thread(target=feed, daemon=True).start()
            chunks = list(client.iter_turn_text("hi"))
            self.assertEqual(chunks, ["Hello", " world."])
        finally:
            client.close()


class TestToolCallTelemetry(unittest.TestCase):
    def test_completed_tool_call_is_logged_at_info_without_arguments(self):
        fake = FakePopen()
        client = make_client(fake)
        try:
            def feed():
                fake.emit({
                    "id": "turn-1", "type": "response",
                    "command": "prompt", "success": True,
                })
                fake.emit({
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "toolcall_end",
                        "toolCall": {
                            "type": "toolCall", "id": "tool-7",
                            "name": "remember", "arguments": {"fact": "private"},
                        },
                    },
                })
                fake.emit({"type": "agent_end"})

            threading.Thread(target=feed, daemon=True).start()
            with self.assertLogs("pi_client", level="INFO") as logs:
                self.assertEqual(list(client.iter_turn_text("remember this")), [])
            joined = "\n".join(logs.output)
            self.assertIn("tool call name=remember id=tool-7", joined)
            self.assertNotIn("private", joined)
        finally:
            client.close()


class TestUiAutoCancel(unittest.TestCase):
    def test_dialog_methods_get_auto_cancelled(self):
        fake = FakePopen()
        client = make_client(fake)
        try:
            def feed():
                fake.emit({
                    "id": "turn-1", "type": "response",
                    "command": "prompt", "success": True,
                })
                # Pi asks a select dialog mid-turn.
                fake.emit({
                    "type": "extension_ui_request",
                    "id": "ui-7", "method": "select",
                    "title": "Pick one", "options": ["a", "b"],
                })
                # confirm too.
                fake.emit({
                    "type": "extension_ui_request",
                    "id": "ui-8", "method": "confirm",
                    "title": "Sure?", "message": "Really?",
                })
                # Fire-and-forget notify should NOT generate a response.
                fake.emit({
                    "type": "extension_ui_request",
                    "id": "ui-9", "method": "notify",
                    "title": "FYI",
                })
                fake.emit({
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "ok"},
                })
                fake.emit({"type": "agent_end"})

            threading.Thread(target=feed, daemon=True).start()
            chunks = list(client.iter_turn_text("hi"))
            self.assertEqual(chunks, ["ok"])

            # Check the turn's stdin: one prompt command + exactly TWO
            # extension_ui_response frames (for select + confirm; notify is dropped).
            kinds = [(c.get("type"), c.get("method")) for c in fake.stdin_lines]
            self.assertIn(("prompt", None), kinds)
            ui_responses = [
                c for c in fake.stdin_lines
                if c.get("type") == "extension_ui_response"
            ]
            self.assertEqual(len(ui_responses), 2)
            self.assertTrue(all(r.get("cancelled") is True for r in ui_responses))
            ids = sorted(str(r.get("id") or "") for r in ui_responses)
            self.assertEqual(ids, ["ui-7", "ui-8"])
        finally:
            client.close()


class TestPromptShape(unittest.TestCase):
    def test_send_turn_writes_correct_jsonl(self):
        fake = FakePopen()
        client = make_client(fake)
        try:
            def feed():
                fake.emit({
                    "id": "turn-1", "type": "response",
                    "command": "prompt", "success": True,
                })
                fake.emit({
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "ok"},
                })
                fake.emit({"type": "agent_end"})

            threading.Thread(target=feed, daemon=True).start()
            list(client.iter_turn_text("Say hi."))
            prompts = [
                c for c in fake.stdin_lines if c.get("type") == "prompt"
            ]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["message"], "Say hi.")
            self.assertEqual(prompts[0]["id"], "turn-1")
        finally:
            client.close()


class TestPromptRejection(unittest.TestCase):
    def test_rejected_prompt_raises(self):
        fake = FakePopen()
        client = make_client(fake)
        try:
            def feed():
                fake.emit({
                    "id": "turn-1", "type": "response",
                    "command": "prompt",
                    "success": False, "error": "bad input",
                })

            threading.Thread(target=feed, daemon=True).start()
            with self.assertRaises(PiClientError):
                list(client.iter_turn_text("bad"))
        finally:
            client.close()


class TestTurnTimeout(unittest.TestCase):
    def test_silent_pi_times_out(self):
        fake = FakePopen()
        client = make_client(fake)
        client._turn_timeout_sec = 1.0  # short for the test
        try:
            # Don't emit anything — let the turn timer fire.
            with self.assertRaises(PiClientError) as ctx:
                list(client.iter_turn_text("hello"))
            self.assertIn("timed out", str(ctx.exception))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
