"""Idle photographer — silent take_photo every few minutes while idle.

Loop shape (per bridge.py's _perception_idle_photographer):
  1. Sleep a jittered SLEEP_MIN..SLEEP_MAX seconds.
  2. Pick a target device (most-recent vision_cache device, else first
     known).
  3. Skip if Kid Mode denies the camera, or the device is not in idle
     state, listening, or has a face present (room-view path covers
     those scenarios).
  4. Snapshot the current vision_cache wall_ts so we can detect
     freshness later.
  5. Dispatch take_photo and wait RESULT_WAIT seconds for the firmware
     → /api/vision/explain → cache fill round-trip.
  6. If a new description landed and is "notable" vs the last saved
     one (Jaccard threshold), append to the daily NDJSON.

No servo motion, no LED change, no audio cue — silent capture.
Dispatch failures (take_photo 404) are logged once by the client and
skipped this cycle; the next 3–5 min retries.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Callable

from dispatch import XiaozhiAdminClient
from logs import NdjsonWriter
from perception import PerceptionState, is_notable_perception

log = logging.getLogger("dotty-behaviour.consumers.idle_photographer")


class IdlePhotographer:
    def __init__(
        self,
        state: PerceptionState,
        xiaozhi: XiaozhiAdminClient,
        writer: NdjsonWriter,
        *,
        sleep_min_sec: float,
        sleep_max_sec: float,
        result_wait_sec: float,
        notable_jaccard: float,
        question: str,
        camera_denied: Callable[[], bool],
    ) -> None:
        self._state = state
        self._xiaozhi = xiaozhi
        self._writer = writer
        self._sleep_min_sec = sleep_min_sec
        self._sleep_max_sec = sleep_max_sec
        self._result_wait_sec = result_wait_sec
        self._notable_jaccard = notable_jaccard
        self._question = question
        self._camera_denied = camera_denied

    async def run(self) -> None:
        log.info(
            "idle photographer started "
            "(sleep=%.0f–%.0fs jaccard=%.2f wait=%.0fs)",
            self._sleep_min_sec, self._sleep_max_sec,
            self._notable_jaccard, self._result_wait_sec,
        )
        try:
            while True:
                sleep_s = random.uniform(
                    self._sleep_min_sec, self._sleep_max_sec
                )
                await asyncio.sleep(sleep_s)
                await self._one_cycle()
        except asyncio.CancelledError:
            log.info("idle photographer cancelled")
            raise
        except Exception:
            log.exception("idle photographer crashed")

    async def _one_cycle(self) -> None:
        if self._camera_denied():
            return
        device_id = self._state.pick_idle_device()
        if not device_id:
            return
        pstate = self._state.state.get(device_id) or {}
        current_state = (pstate.get("current_state") or "idle").lower()
        if current_state != "idle":
            return
        if pstate.get("listening") or pstate.get("face_present"):
            return

        pre_ts = (
            self._state.vision_cache.get(device_id, {}).get("wall_ts") or 0.0
        )
        ok = await self._xiaozhi.take_photo(
            device_id, question=self._question
        )
        if not ok:
            return  # XiaozhiAdminClient already logged

        await asyncio.sleep(self._result_wait_sec)
        entry = self._state.vision_cache.get(device_id) or {}
        new_ts = entry.get("wall_ts") or 0.0
        description = (entry.get("description") or "").strip()
        if not description or new_ts <= pre_ts:
            log.info(
                "idle photographer: device=%s no fresh description "
                "(new_ts=%.0f pre_ts=%.0f)",
                device_id, new_ts, pre_ts,
            )
            return

        last_text = pstate.get("last_idle_perception_text")
        if not is_notable_perception(
            description, last_text,
            jaccard_threshold=self._notable_jaccard,
        ):
            log.info(
                "idle photographer: device=%s skipped (not notable len=%d)",
                device_id, len(description),
            )
            return

        # Mutate state under the same dict so other readers see the
        # update without re-fetching.
        live = self._state.state.setdefault(device_id, {})
        live["last_idle_perception_text"] = description
        live["last_idle_perception_t"] = time.time()
        self._writer.append(
            {
                "ts": self._writer.now_isoformat(),
                "device": device_id,
                "type": "perception",
                "mode": "idle",
                "text": description,
            }
        )
        log.info(
            "idle photographer: device=%s saved perception (%d chars)",
            device_id, len(description),
        )
