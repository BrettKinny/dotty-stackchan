"""Dotty override of upstream core/handle/textMessageHandlerRegistry.py.

Adds an `EventTextMessageHandler` that relays ambient perception
events from the firmware to the zeroclaw-bridge over HTTP. Inline
duck-type sidesteps an additional override of textMessageType.py.

All other handlers and registry behaviour are preserved verbatim.
"""

import asyncio
import os
import time
from typing import Any, Dict, Optional

# DOTTY-PATCH: pin fire-and-forget tasks so asyncio's weakref doesn't
# GC them mid-flight. Use _spawn() in place of bare asyncio.create_task
# wherever the returned Task isn't kept by the caller.
_BACKGROUND_TASKS: set = set()


def _spawn(coro, *, name: str | None = None):
    t = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t

from core.handle.textHandler.abortMessageHandler import AbortTextMessageHandler
from core.handle.textHandler.helloMessageHandler import HelloTextMessageHandler
from core.handle.textHandler.iotMessageHandler import IotTextMessageHandler
from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
from core.handle.textHandler.mcpMessageHandler import McpTextMessageHandler
from core.handle.textHandler.pingMessageHandler import PingMessageHandler
from core.handle.textHandler.serverMessageHandler import ServerTextMessageHandler
from core.handle.textMessageHandler import TextMessageHandler

TAG = __name__


class _EventTypeShim:
    """Duck-types TextMessageType so the registry's `.value` lookup
    works without also overriding the upstream enum file."""
    value = "event"


class EventTextMessageHandler(TextMessageHandler):
    """Relay ambient perception events from firmware to the bridge.

    Wire format from firmware:
        {"type": "event", "name": <str>, "data": <dict>, "ts": <ms-since-boot>}

    Fires a fire-and-forget HTTP POST to the bridge so a slow or
    unreachable bridge never blocks the WebSocket handler.
    """

    @property
    def message_type(self):
        return _EventTypeShim()

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        device_id = "unknown"
        try:
            device_id = conn.headers.get("device-id", "unknown")
        except Exception:
            pass

        bridge_url = (
            os.environ.get("BRIDGE_URL")
            or os.environ.get("VISION_BRIDGE_URL", "")
        )
        if not bridge_url:
            conn.logger.bind(tag=TAG).warning(
                "no BRIDGE_URL/VISION_BRIDGE_URL set, dropping perception event"
            )
            return

        # Description-based identity hooks — see
        # `receiveAudioHandle._capture_room_description_async`.
        #
        # On `face_lost`: invalidate the cached description so the next
        # `face_detected` triggers a fresh capture. This is what makes
        # the feature "responsive" when people swap in front of the
        # camera — the LLM stops claiming the previous person is still
        # there as soon as the firmware loses the face.
        #
        # On `face_detected`: if no description is cached and no
        # capture is already in flight, kick off a background capture.
        # By the time the user actually speaks, the description is
        # usually ready; if not, the voice turn just goes out without
        # `[Room view]` (no added voice-turn latency either way).
        event_name = msg_json.get("name", "")
        if event_name == "face_lost":
            try:
                conn._room_description = None
                conn._room_description_ts = 0.0
                # v2 room_view roster identification: clear the matched
                # person_id alongside the description so the next
                # face_detected re-captures from scratch — same
                # responsiveness contract as the description cache.
                conn._room_match_person_id = None
            except Exception:
                pass
        # Phase 4: track the firmware's high-level State on the WS conn so
        # receiveAudioHandle (and any other in-process consumer) can gate
        # behaviour on it without the bridge round-trip. The bridge gets
        # the same event via the relay below and updates _perception_state
        # in parallel — both paths are kept in sync by StateManager being
        # the only producer.
        elif event_name == "state_changed":
            try:
                new_state = ((msg_json.get("data") or {}).get("state") or "").strip().lower()
                if new_state:
                    conn.current_state = new_state
            except Exception:
                pass
        # Listen on `face_detected`. Prior firmware also emitted
        # `face_recognized(identity="unknown")` from the dormant dlib
        # FaceRecognizer scaffold; that scaffold + emission were removed
        # in fw-v1.3.2 (firmware `ea3f04b`), so the bridge no longer
        # needs the dual-event handling.
        elif event_name == "face_detected":
            try:
                if (not getattr(conn, "_room_description", None)
                        and not getattr(
                            conn, "_room_description_in_flight", False)):
                    # Imported lazily — receiveAudioHandle imports
                    # core.* modules that aren't available at module
                    # import time in some test contexts. The bind-mount
                    # target inside the xiaozhi container is the
                    # `core.handle.receiveAudioHandle` package path, NOT
                    # a top-level module — the bare `from receiveAudioHandle`
                    # form here originally raised `No module named` and
                    # silently no-op'd the room_view capture for weeks.
                    from core.handle.receiveAudioHandle import (
                        _capture_room_description_async,
                    )
                    _spawn(
                        _capture_room_description_async(conn),
                        name="room_view_capture",
                    )
            except Exception as exc:
                conn.logger.bind(tag=TAG).warning(
                    f"room_view: failed to start capture: {exc}"
                )

        # Firmware ts is ms-since-boot, not wall-clock — useless for
        # consumer "last N seconds" comparisons across the relay hop.
        # Use server wall-clock and preserve the firmware value in
        # data["firmware_ts_ms"] for debug / event ordering within a
        # single device boot.
        data = dict(msg_json.get("data") or {})
        firmware_ts = msg_json.get("ts")
        if isinstance(firmware_ts, (int, float)):
            data["firmware_ts_ms"] = firmware_ts

        payload = {
            "device_id": device_id,
            "ts": time.time(),
            "name": event_name,
            "data": data,
        }
        url = f"{bridge_url.rstrip('/')}/api/perception/event"

        def _post() -> None:
            try:
                import requests
                r = requests.post(url, json=payload, timeout=2)
                if r.status_code >= 400:
                    conn.logger.bind(tag=TAG).warning(
                        f"perception POST {r.status_code}: {r.text[:120]}"
                    )
            except Exception as exc:
                conn.logger.bind(tag=TAG).warning(
                    f"perception POST failed: {exc}"
                )

        _spawn(asyncio.to_thread(_post), name="perception_event_post")


class TextMessageHandlerRegistry:
    """消息处理器注册表 — Dotty override adds the EVENT relay handler."""

    def __init__(self):
        self._handlers: Dict[str, TextMessageHandler] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        handlers = [
            HelloTextMessageHandler(),
            AbortTextMessageHandler(),
            ListenTextMessageHandler(),
            IotTextMessageHandler(),
            McpTextMessageHandler(),
            ServerTextMessageHandler(),
            PingMessageHandler(),
            EventTextMessageHandler(),  # Dotty addition for ambient perception
        ]
        for handler in handlers:
            self.register_handler(handler)

    def register_handler(self, handler: TextMessageHandler) -> None:
        self._handlers[handler.message_type.value] = handler

    def get_handler(self, message_type: str) -> Optional[TextMessageHandler]:
        return self._handlers.get(message_type)

    def get_supported_types(self) -> list:
        return list(self._handlers.keys())
