"""Play a pre-rendered cat-purr asset on `head_pet_started` events.

Per-device cooldown stops a continuous head-pet from re-triggering on
every event burst. Extends ``last_chat_t`` by PURR_DURATION_SEC so
the sound localiser doesn't turn the head toward the speaker mid-purr
(without that suppression the localiser would treat the purr's own
audio as a sound event from the side).

Firmware-side `head_pet_started` emission was a 2026-04-27 follow-up;
this consumer was ready against the bus before the firmware caught up.
Behaviour identical to bridge/purr_player.py — same env knobs, same
cooldown, same last_chat_t poke.
"""

from __future__ import annotations

import asyncio
import logging
import time

from dispatch import XiaozhiAdminClient
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.purr_player")


class PurrPlayer:
    def __init__(
        self,
        state: PerceptionState,
        xiaozhi: XiaozhiAdminClient,
        *,
        asset_path: str,
        cooldown_sec: float,
        duration_sec: float,
    ) -> None:
        self._state = state
        self._xiaozhi = xiaozhi
        self._asset_path = asset_path
        self._cooldown_sec = cooldown_sec
        self._duration_sec = duration_sec
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro, *, name: str | None = None) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        log.info(
            "purr player started (cooldown=%.0fs asset=%s)",
            self._cooldown_sec,
            self._asset_path,
        )
        q = self._state.subscribe()
        try:
            while True:
                event = await q.get()
                if event.name != "head_pet_started":
                    continue
                device_id = event.device_id
                if not device_id or device_id == "unknown":
                    continue
                now = float(event.ts or time.time())
                dev_state = self._state.state.setdefault(device_id, {})
                last_purr = dev_state.get("last_purr_t", 0.0)
                if now - last_purr < self._cooldown_sec:
                    continue
                dev_state["last_purr_t"] = now
                # Suppress the sound localiser during playback by
                # pushing last_chat_t into the future.
                dev_state["last_chat_t"] = now + self._duration_sec
                log.info("head_pet_started → purr: device=%s", device_id)
                self._spawn(
                    self._xiaozhi.play_asset(device_id, self._asset_path),
                    name="purr_play_asset",
                )
        except asyncio.CancelledError:
            log.info("purr player cancelled")
            for t in list(self._tasks):
                if not t.done():
                    t.cancel()
            raise
        except Exception:
            log.exception("purr player crashed")
        finally:
            self._state.unsubscribe(q)
