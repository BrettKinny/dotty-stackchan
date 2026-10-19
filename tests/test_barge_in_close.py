"""#105: a storm of interrupted replies ends the listening session.

AI-assisted (Claude Opus 5.5). Bench 2026-10-06: with the mic left open, room
and TV audio produced 18 recognitions and 19 aborts in three minutes at
levels overlapping a quiet child's real speech. The maintainer chose to close
the session rather than keep listening; touch or the wake word reopens it.
"""
import asyncio
import importlib.util as _ilu
import json
import pathlib
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = pathlib.Path(__file__).parent.parent


def _ns(**attrs):
    return types.SimpleNamespace(**attrs)


@contextmanager
def _stubbed(**modules):
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in modules}
    try:
        sys.modules.update(modules)
        yield
    finally:
        for name, module in saved.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


_STUBS = {
    "core": _ns(), "core.utils": _ns(), "core.handle": _ns(),
    "core.utils.util": _ns(audio_to_data=lambda *a, **k: None),
    "core.handle.abortHandle": _ns(handleAbortMessage=AsyncMock()),
    "core.handle.intentHandler": _ns(handle_user_intent=AsyncMock(return_value=False)),
    "core.utils.output_counter": _ns(check_device_output_limit=lambda *a: False),
    "core.handle.sendAudioHandle": _ns(send_stt_message=AsyncMock(), SentenceType=object),
    "core.utils.device_command": _ns(call_tool=AsyncMock()),
}
with _stubbed(**_STUBS):
    _spec = _ilu.spec_from_file_location("receive_audio_barge_in_under_test",
                                         _ROOT / "receiveAudioHandle.py")
    rah = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(rah)  # type: ignore[union-attr]


def _conn(config=None):
    return _ns(logger=MagicMock(), config=config or {}, client_is_speaking=True,
               client_listen_mode="auto", need_bind=False, max_output_size=0,
               headers={"device-id": "dev-1"}, session_id="s")


class _EndSession:
    """Stand-in for device_command.end_session, installed for the lazy import."""

    def __init__(self):
        self.calls = []

    async def __call__(self, conn, **kwargs):
        self.calls.append(kwargs)
        return True


class TestBargeInStorm(unittest.TestCase):
    def _run(self, coro_fn):
        end = _EndSession()

        async def go():
            result = await coro_fn()
            await asyncio.sleep(0)  # let the spawned end_session run
            return result

        with _stubbed(**{"core.utils.device_command": _ns(end_session=end)}):
            result = asyncio.run(go())
        return result, end.calls

    def test_fourth_interruption_within_a_minute_ends_the_session(self):
        conn = _conn()

        async def go():
            return [rah._note_barge_in(conn, now=t) for t in (0, 10, 20, 30)]

        results, calls = self._run(go)
        self.assertEqual(results, [False, False, False, True])
        self.assertEqual(calls, [{"reason": "barge-in storm"}])

    def test_spread_out_interruptions_never_close(self):
        conn = _conn()

        async def go():
            return [rah._note_barge_in(conn, now=t) for t in range(0, 600, 25)]

        results, calls = self._run(go)
        self.assertNotIn(True, results)
        self.assertEqual(calls, [])

    def test_threshold_is_configurable_and_zero_disables(self):
        tight = _conn({"dotty_barge_in_close": {"aborts": 2, "window_s": 5}})
        off = _conn({"dotty_barge_in_close": {"aborts": 0}})

        async def go():
            return ([rah._note_barge_in(tight, now=t) for t in (0, 6, 7)],
                    [rah._note_barge_in(off, now=t) for t in range(10)])

        (tight_results, off_results), calls = self._run(go)
        self.assertEqual(tight_results, [False, False, True])
        self.assertNotIn(True, off_results)
        self.assertEqual(len(calls), 1)

    def test_vad_barge_in_path_stops_feeding_asr_on_the_storm(self):
        conn = _conn()
        conn.vad = _ns(is_vad=lambda c, a: True)
        conn.asr = _ns(receive_audio=AsyncMock())

        async def go():
            with patch.object(rah, "handleAbortMessage", new=AsyncMock()), \
                 patch.object(rah, "no_voice_close_connect", new=AsyncMock()):
                for _ in range(4):
                    conn.client_is_speaking = True
                    await rah.handleAudioMessage(conn, b"frame")

        _result, calls = self._run(go)
        self.assertEqual(conn.asr.receive_audio.await_count, 3)
        self.assertEqual(len(calls), 1)

    def test_asr_turn_that_completes_the_storm_is_not_answered(self):
        conn = _conn()
        intent = AsyncMock(return_value=True)

        async def go():
            with patch.object(rah, "handleAbortMessage", new=AsyncMock()), \
                 patch.object(rah, "_sync_toggles_once", new=AsyncMock()), \
                 patch.object(rah, "handle_user_intent", new=intent):
                for text in ("one thing", "another", "and more", "fourth one"):
                    conn.client_is_speaking = True
                    await rah.startToChat(conn, json.dumps({"content": text}))

        _result, calls = self._run(go)
        self.assertEqual([c.args[1] for c in intent.await_args_list],
                         ["one thing", "another", "and more"])
        self.assertEqual(len(calls), 1)

    def test_turns_while_dotty_is_silent_do_not_count(self):
        conn = _conn()
        conn.client_is_speaking = False
        intent = AsyncMock(return_value=True)

        async def go():
            with patch.object(rah, "_sync_toggles_once", new=AsyncMock()), \
                 patch.object(rah, "handle_user_intent", new=intent):
                for i in range(6):
                    await rah.startToChat(conn, json.dumps({"content": f"turn {i}"}))

        _result, calls = self._run(go)
        self.assertEqual(intent.await_count, 6)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
