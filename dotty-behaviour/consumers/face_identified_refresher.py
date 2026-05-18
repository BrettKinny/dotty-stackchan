"""Periodically re-fire set_face_identified while a person stays in
frame.

Firmware self-times-out the right-ring green pip after ~4 s, so
without a refresh the LED would only be green for the first 4 s of
every 2-min VLM-cooldown window. This loop runs at INTERVAL and
skips devices whose identity is stale (TTL-expired) or whose face has
been genuinely lost for > QUIET_SEC.
"""

from __future__ import annotations

import asyncio
import logging
import time

from dispatch import XiaozhiAdminClient
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.face_identified_refresher")


class FaceIdentifiedRefresher:
    def __init__(
        self,
        state: PerceptionState,
        xiaozhi: XiaozhiAdminClient,
        *,
        interval_sec: float,
        ttl_sec: float,
        quiet_after_lost_sec: float,
    ) -> None:
        self._state = state
        self._xiaozhi = xiaozhi
        self._interval_sec = interval_sec
        self._ttl_sec = ttl_sec
        self._quiet_after_lost_sec = quiet_after_lost_sec
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro, *, name: str | None = None) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        log.info(
            "face-identified refresher started "
            "(interval=%.1fs ttl=%.0fs quiet_after_lost=%.1fs)",
            self._interval_sec,
            self._ttl_sec,
            self._quiet_after_lost_sec,
        )
        try:
            while True:
                await asyncio.sleep(self._interval_sec)
                wall_now = time.time()
                for device_id, dev_state in list(self._state.state.items()):
                    if not isinstance(dev_state, dict):
                        continue
                    identity = self._state.get_fresh_face_id(
                        device_id, ttl_sec=self._ttl_sec, now=wall_now
                    )
                    if not identity:
                        continue
                    face_present = bool(dev_state.get("face_present"))
                    last_lost = dev_state.get("last_face_lost_t") or 0.0
                    if not face_present and last_lost:
                        if (
                            wall_now - last_lost
                        ) > self._quiet_after_lost_sec:
                            continue
                    log.info(
                        "face_identified_refresh: device=%s identity=%s",
                        device_id,
                        identity,
                    )
                    self._spawn(
                        self._xiaozhi.set_face_identified(device_id),
                        name="refresher_set_face_identified",
                    )
        except asyncio.CancelledError:
            log.info("face-identified refresher cancelled")
            for t in list(self._tasks):
                if not t.done():
                    t.cancel()
            raise
        except Exception:
            log.exception("face-identified refresher crashed")
