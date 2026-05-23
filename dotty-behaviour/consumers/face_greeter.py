"""Face greeter — bare "Hi!" on face_detected + named greeting on
face_recognized.

Two greeting paths live in one consumer because both subscribe to the
same bus and need to coordinate (the bare greet is suppressed when the
household registry is non-empty, since the named greet covers the same
intent more specifically).

Bare path (face_detected):
  * Suppressed when current hour is outside [HOUR_START, HOUR_END)
    (sensor-noise frames at 3am should not greet an empty room).
  * Suppressed when the household has at least one roster member
    with `appearance:` set — the only members the room_view path can
    identify by photo. Layer-6 proactive greeter / room-view
    recognition fires a richer named greet within 1-2 s.
  * Per-device cooldown.
  * Empty FACE_GREET_TEXT disables the verbal injection — the
    firmware-side wake popup still fires.
  * Dispatch via inject_text → xiaozhi runs the text through the LLM.

Named path (face_recognized):
  * Looks the identity up in the household registry.
  * Suppressed if a chat happened within QUIET_AFTER_CHAT_SEC.
  * Per-identity cooldown.
  * Dispatch via `say` (bypasses ASR/LLM — Dotty's own speech, not a
    fake user utterance), then fires set_face_identified for the LED.

Writes `last_face_greet_t` on the bare path so the face_lost_aborter
can decide whether to abort.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from dispatch import XiaozhiAdminClient
from household import HouseholdRegistry
from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.consumers.face_greeter")


class FaceGreeter:
    def __init__(
        self,
        state: PerceptionState,
        xiaozhi: XiaozhiAdminClient,
        household: HouseholdRegistry | None,
        *,
        bare_greet_text: str,
        bare_min_interval_sec: float,
        bare_hour_start: int,
        bare_hour_end: int,
        name_template: str,
        name_min_interval_sec: float,
        name_quiet_after_chat_sec: float,
        tz: ZoneInfo,
    ) -> None:
        self._state = state
        self._xiaozhi = xiaozhi
        self._household = household
        self._bare_greet_text = bare_greet_text
        self._bare_min_interval_sec = bare_min_interval_sec
        self._bare_hour_start = bare_hour_start
        self._bare_hour_end = bare_hour_end
        self._name_template = name_template
        self._name_min_interval_sec = name_min_interval_sec
        self._name_quiet_after_chat_sec = name_quiet_after_chat_sec
        self._tz = tz
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro, *, name: str | None = None) -> None:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _roster_has_identifiable_members(self) -> bool:
        if self._household is None:
            return False
        return bool(self._household.roster_ids_with_appearance())

    async def _handle_face_detected(self, device_id: str, now: float) -> None:
        # Time-of-day gate
        current_hour = datetime.now(self._tz).hour
        if not (
            self._bare_hour_start <= current_hour < self._bare_hour_end
        ):
            log.debug(
                "face_detected → suppressed (outside %d–%d window): device=%s",
                self._bare_hour_start, self._bare_hour_end, device_id,
            )
            return

        # Hand off to roster-aware greeter when one exists
        if self._roster_has_identifiable_members():
            log.debug(
                "face_detected → suppressed (roster owns greeting): device=%s",
                device_id,
            )
            return

        dev_state = self._state.state.setdefault(device_id, {})
        last_greet = dev_state.get("last_face_greet_t", 0.0)
        if now - last_greet < self._bare_min_interval_sec:
            return
        dev_state["last_face_greet_t"] = now

        if not self._bare_greet_text:
            log.info(
                "face_detected → mic-only (FACE_GREET_TEXT empty): device=%s",
                device_id,
            )
            return

        # Suppress when a dance is active — face greeting on top of a
        # dance is jarring (bridge.py's _is_dance_active gate).
        if self._state.is_dance_active(device_id):
            log.info(
                "face_detected → suppressed (dance active): device=%s",
                device_id,
            )
            return

        log.info("face_detected → greeting: device=%s", device_id)
        self._spawn(
            self._xiaozhi.inject_text(device_id, self._bare_greet_text),
            name="face_greeter_inject_text",
        )

    async def _handle_face_recognized(
        self, device_id: str, data: dict, now: float
    ) -> None:
        if self._household is None:
            return
        identity = (data.get("identity") or "").strip()
        if not identity:
            return
        person = self._household.get(identity)
        if person is None or not person.display_name:
            log.debug(
                "face_recognized: identity=%s not in roster", identity
            )
            return

        dev_state = self._state.state.setdefault(device_id, {})
        last_chat = dev_state.get("last_chat_t", 0.0)
        if now - last_chat < self._name_quiet_after_chat_sec:
            log.debug(
                "face_recognized → suppressed (chat fresh): "
                "device=%s identity=%s",
                device_id, identity,
            )
            return
        name_greets = dev_state.setdefault("last_name_greet_t", {})
        last_named = name_greets.get(identity, 0.0)
        if now - last_named < self._name_min_interval_sec:
            return
        name_greets[identity] = now

        if self._state.is_dance_active(device_id):
            log.info(
                "face_recognized → suppressed (dance active): device=%s",
                device_id,
            )
            return

        text = self._name_template.format(name=person.display_name)
        log.info(
            "face_recognized → name-greet: device=%s identity=%s text=%r",
            device_id, identity, text,
        )
        self._spawn(
            self._xiaozhi.say(device_id, text),
            name="face_greeter_say",
        )
        # Light the right-ring face pixel green to mirror the named
        # greeting. Firmware auto-times-out after ~4 s; the
        # face_identified_refresher consumer keeps it lit while the
        # person stays in frame.
        self._spawn(
            self._xiaozhi.set_face_identified(device_id),
            name="face_greeter_set_face_identified",
        )

    async def run(self) -> None:
        log.info(
            "face greeter started "
            "(bare_interval=%.0fs text=%r name_template=%r)",
            self._bare_min_interval_sec,
            self._bare_greet_text,
            self._name_template,
        )
        q = self._state.subscribe()
        try:
            while True:
                event = await q.get()
                device_id = event.device_id
                if not device_id or device_id == "unknown":
                    continue
                if event.name == "face_recognized":
                    await self._handle_face_recognized(
                        device_id, event.data or {}, event.ts
                    )
                elif event.name == "face_detected":
                    await self._handle_face_detected(device_id, event.ts)
        except asyncio.CancelledError:
            log.info("face greeter cancelled")
            for t in list(self._tasks):
                if not t.done():
                    t.cancel()
            raise
        except Exception:
            log.exception("face greeter crashed")
        finally:
            self._state.unsubscribe(q)
