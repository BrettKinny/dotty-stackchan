"""Shared test fakes — recorder XiaozhiAdminClient + helpers.

Consumers need an injectable XiaozhiAdminClient; rather than mocking
`requests` per-test, we substitute the whole client with a recorder
that captures each method call so assertions are obvious.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeXiaozhi:
    """Drop-in recorder for XiaozhiAdminClient.

    Each method records its kwargs into a per-method list and returns
    True (or a per-method overridden return value). Tests assert on the
    lists.
    """

    abort_calls: list[dict[str, Any]] = field(default_factory=list)
    inject_text_calls: list[dict[str, Any]] = field(default_factory=list)
    say_calls: list[dict[str, Any]] = field(default_factory=list)
    set_head_angles_calls: list[dict[str, Any]] = field(default_factory=list)
    set_state_calls: list[dict[str, Any]] = field(default_factory=list)
    set_face_identified_calls: list[dict[str, Any]] = field(default_factory=list)
    set_toggle_calls: list[dict[str, Any]] = field(default_factory=list)
    play_asset_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return True

    async def abort(self, device_id: str) -> bool:
        self.abort_calls.append({"device_id": device_id})
        return True

    async def inject_text(self, device_id: str, text: str) -> bool:
        self.inject_text_calls.append({"device_id": device_id, "text": text})
        return True

    async def say(self, device_id: str, text: str) -> bool:
        self.say_calls.append({"device_id": device_id, "text": text})
        return True

    async def set_head_angles(
        self, device_id: str, yaw: int, pitch: int, speed: int
    ) -> bool:
        self.set_head_angles_calls.append(
            {
                "device_id": device_id,
                "yaw": yaw,
                "pitch": pitch,
                "speed": speed,
            }
        )
        return True

    async def set_state(self, device_id: str, state: str) -> bool:
        self.set_state_calls.append({"device_id": device_id, "state": state})
        return True

    async def set_face_identified(self, device_id: str) -> bool:
        self.set_face_identified_calls.append({"device_id": device_id})
        return True

    async def set_toggle(
        self, device_id: str, name: str, enabled: bool
    ) -> bool:
        self.set_toggle_calls.append(
            {"device_id": device_id, "name": name, "enabled": enabled}
        )
        return True

    async def play_asset(self, device_id: str, asset: str) -> bool:
        self.play_asset_calls.append(
            {"device_id": device_id, "asset": asset}
        )
        return True


async def let_consumer_settle() -> None:
    """Yield control enough times for a consumer to drain queued events
    and any spawned fire-and-forget tasks to run. One asyncio.sleep(0)
    is not enough on cpython because each create_task schedules a fresh
    callback that needs another loop iteration to execute."""
    for _ in range(8):
        await asyncio.sleep(0)
