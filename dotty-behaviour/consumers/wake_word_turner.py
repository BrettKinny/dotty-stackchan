"""On `wake_word_detected`, turn the head toward the speaker.

Distinct from SoundTurner:
  * sound_turner   = "curious about an ambient noise" — cooldown'd, gentler
  * wake_word_turn = "look at the user who summoned me" — no cooldown, faster

Skips when a face is already being tracked (face tracker owns gaze
in that case — likely the speaker IS the tracked face). Skips
direction=centre because there's no spatial info to act on.

Writes ``state[device_id]["last_sound_turn_t"]`` so the ambient
sound turner doesn't immediately re-fire on the user's continued
voice after the wake word.
"""

from __future__ import annotations

import asyncio
import logging

from dispatch import XiaozhiAdminClient
from perception import PerceptionEvent, PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.wake_word_turner")


class WakeWordTurner:
    def __init__(
        self,
        state: PerceptionState,
        xiaozhi: XiaozhiAdminClient,
        *,
        yaw_deg: int,
        speed: int,
    ) -> None:
        self._state = state
        self._xiaozhi = xiaozhi
        self._yaw_deg = yaw_deg
        self._speed = speed
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro, *, name: str | None = None) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        log.info(
            "wake-word turner started (yaw=±%d speed=%d)",
            self._yaw_deg,
            self._speed,
        )
        q = self._state.subscribe()
        try:
            while True:
                event = await q.get()
                if event.name != "wake_word_detected":
                    continue
                device_id = event.device_id
                if not device_id or device_id == "unknown":
                    continue
                data = event.data or {}
                direction = data.get("direction", "")
                if direction not in ("left", "right"):
                    continue

                dev_state = self._state.state.setdefault(device_id, {})
                if dev_state.get("face_present"):
                    continue

                yaw = (
                    -self._yaw_deg if direction == "left" else self._yaw_deg
                )
                now = event.ts
                dev_state["last_sound_turn_t"] = now

                log.info(
                    "wake_word_detected → head-turn: "
                    "device=%s phrase=%r dir=%s yaw=%d",
                    device_id,
                    data.get("phrase", ""),
                    direction,
                    yaw,
                )
                self._spawn(
                    self._xiaozhi.set_head_angles(
                        device_id, yaw, 0, self._speed
                    ),
                    name="wake_word_turner_set_head_angles",
                )
                head_turn_data = {
                    "yaw": yaw,
                    "pitch": 0,
                    "speed": self._speed,
                    "reason": "wake_word",
                    "direction": direction,
                    "phrase": data.get("phrase", ""),
                }
                self._state.update_state(
                    device_id, "head_turn", head_turn_data, now
                )
                self._state.broadcast(
                    PerceptionEvent(
                        device_id=device_id,
                        name="head_turn",
                        data=head_turn_data,
                        ts=now,
                    )
                )
        except asyncio.CancelledError:
            log.info("wake-word turner cancelled")
            for t in list(self._tasks):
                if not t.done():
                    t.cancel()
            raise
        except Exception:
            log.exception("wake-word turner crashed")
        finally:
            self._state.unsubscribe(q)
