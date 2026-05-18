"""On face_lost after a recent greet, abort in-flight TTS so Dotty
stops talking to an empty room.

Two-stage filter that mirrors bridge.py's `_perception_face_lost_aborter`:

  1. Only acts within WINDOW seconds of the last greet — long-finished
     conversations are left alone (no surprise interruptions if you
     glance away during a sentence Dotty's saying half an hour after
     greeting you).
  2. Schedules the abort GRACE seconds in the future and cancels it
     if face_detected fires for the same device first. Without this,
     the HuMan-detector's normal flicker (face_lost/face_detected
     pairs ~1 s apart) eats every turn.

State coupling: relies on consumers/upstream callers writing
``state[device_id]["last_face_greet_t"]`` so this loop knows which
greets are recent. face_greeter (deferred to a later slice) is the
canonical writer; without it the aborter is silent — never acts.
"""

from __future__ import annotations

import asyncio
import logging

from dispatch import XiaozhiAdminClient
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.face_lost_aborter")


class FaceLostAborter:
    def __init__(
        self,
        state: PerceptionState,
        xiaozhi: XiaozhiAdminClient,
        *,
        window_sec: float,
        grace_sec: float,
    ) -> None:
        self._state = state
        self._xiaozhi = xiaozhi
        self._window_sec = window_sec
        self._grace_sec = grace_sec
        self._pending: dict[str, asyncio.Task] = {}

    async def _delayed_abort(self, device_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            log.info(
                "face_lost → abort: device=%s (face stayed lost %.1fs)",
                device_id,
                delay,
            )
            await self._xiaozhi.abort(device_id)
        except asyncio.CancelledError:
            log.info(
                "face_lost abort cancelled (face returned): device=%s",
                device_id,
            )
            raise

    async def run(self) -> None:
        log.info(
            "face-lost aborter started (window=%.0fs grace=%.1fs)",
            self._window_sec,
            self._grace_sec,
        )
        q = self._state.subscribe()
        try:
            while True:
                event = await q.get()
                device_id = event.device_id
                if not device_id or device_id == "unknown":
                    continue

                if event.name == "face_detected":
                    t = self._pending.pop(device_id, None)
                    if t and not t.done():
                        t.cancel()
                    continue

                if event.name != "face_lost":
                    continue

                now = event.ts
                dev_state = self._state.state.setdefault(device_id, {})
                last_greet = dev_state.get("last_face_greet_t", 0.0)
                if now - last_greet > self._window_sec:
                    continue

                prior = self._pending.pop(device_id, None)
                if prior and not prior.done():
                    prior.cancel()
                log.info(
                    "face_lost → schedule abort in %.1fs: device=%s (greet %.1fs ago)",
                    self._grace_sec,
                    device_id,
                    now - last_greet,
                )
                self._pending[device_id] = asyncio.create_task(
                    self._delayed_abort(device_id, self._grace_sec),
                )
        except asyncio.CancelledError:
            log.info("face-lost aborter cancelled")
            for t in self._pending.values():
                if not t.done():
                    t.cancel()
            raise
        except Exception:
            log.exception("face-lost aborter crashed")
        finally:
            self._state.unsubscribe(q)
