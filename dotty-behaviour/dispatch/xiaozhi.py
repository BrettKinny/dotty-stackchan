"""xiaozhi-server admin HTTP client.

Lifted from bridge.py's `_dispatch_*` family. Each method maps to
exactly one `/xiaozhi/admin/*` route. All methods are fire-and-forget
in spirit — they return a bool reporting whether the call succeeded
but never raise on network errors, so a flaky xiaozhi-server can't
crash a consumer loop.

The dance-active gate that some bridge.py dispatchers used to do
inline (`_is_dance_active`) is intentionally NOT here. Gating
belongs to the consumer that owns the policy (face_greeter,
proactive_greeter), not the network layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

log = logging.getLogger("dotty-behaviour.dispatch.xiaozhi")


class XiaozhiAdminClient:
    """HTTP client for xiaozhi-server's /xiaozhi/admin/* routes."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 3.0,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        """True iff a host is set — empty host disables all dispatch
        the same way bridge.py treats `not _XIAOZHI_HOST`."""
        return bool(self._host)

    def _url(self, path: str) -> str:
        return f"http://{self._host}:{self._port}{path}"

    def _post_sync(
        self, url: str, payload: dict[str, Any], *, label: str
    ) -> bool:
        """Synchronous HTTP POST run inside asyncio.to_thread.

        Returns True iff the response is 2xx. Logs at warning on any
        failure (network, 4xx, 5xx) but never raises.
        """
        try:
            r = requests.post(url, json=payload, timeout=self._timeout_s)
            if r.status_code >= 400:
                log.warning(
                    "%s %s: %s", label, r.status_code, r.text[:200]
                )
                return False
            return True
        except Exception as exc:
            log.warning("%s failed: %s", label, exc)
            return False

    async def _post(
        self, path: str, payload: dict[str, Any], *, label: str
    ) -> bool:
        if not self.configured:
            log.warning(
                "%s: XIAOZHI_HOST not set; cannot reach xiaozhi-server",
                label,
            )
            return False
        url = self._url(path)
        return await asyncio.to_thread(
            self._post_sync, url, payload, label=label
        )

    # ------------------------------------------------------------------
    # The admin endpoints bridge.py dispatches to. (The former
    # set-tier1slim-model endpoint was removed with Tier1Slim in the
    # 2026-05-29 alignment pass — smart_mode is toggle-only on the live
    # PiVoiceLLM path; model-swap is v2 scope.)
    # ------------------------------------------------------------------

    async def abort(self, device_id: str) -> bool:
        """Stop in-flight TTS for a device."""
        return await self._post(
            "/xiaozhi/admin/abort",
            {"device_id": device_id},
            label="abort",
        )

    async def inject_text(self, device_id: str, text: str) -> bool:
        """Push text through the LLM pipeline as if the user said it.

        Used by the face greeter — Dotty's reply gets generated +
        spoken normally.
        """
        return await self._post(
            "/xiaozhi/admin/inject-text",
            {"text": text, "device_id": device_id},
            label="inject-text",
        )

    async def say(self, device_id: str, text: str) -> bool:
        """Stream TTS straight to the device, bypassing ASR/LLM.

        Used by the proactive greeter — Dotty produces a pre-composed
        greeting verbatim rather than treating server-side text as a
        fake user utterance.
        """
        return await self._post(
            "/xiaozhi/admin/say",
            {"text": text, "device_id": device_id},
            label="say",
        )

    async def set_head_angles(
        self, device_id: str, yaw: int, pitch: int, speed: int
    ) -> bool:
        """Direct servo command — used by sound/wake-word turners."""
        return await self._post(
            "/xiaozhi/admin/set-head-angles",
            {
                "device_id": device_id,
                "yaw": yaw,
                "pitch": pitch,
                "speed": speed,
            },
            label="set-head-angles",
        )

    async def set_state(self, device_id: str, state: str) -> bool:
        """Transition the firmware StateManager into the given State.

        ``state`` must be one of: idle / talk / story_time / security
        / sleep / dance. Firmware rejects unknown values.
        """
        return await self._post(
            "/xiaozhi/admin/set-state",
            {"device_id": device_id, "state": state},
            label="set-state",
        )

    async def set_face_identified(self, device_id: str) -> bool:
        """Light the right-ring face pixel green (~4 s firmware timeout).

        Cosmetic only — failures don't block the recognising consumer.
        """
        return await self._post(
            "/xiaozhi/admin/set-face-identified",
            {"device_id": device_id},
            label="set-face-identified",
        )

    async def set_toggle(
        self, device_id: str, name: str, enabled: bool
    ) -> bool:
        """Set a firmware toggle. ``name`` ∈ {kid_mode, smart_mode}."""
        return await self._post(
            "/xiaozhi/admin/set-toggle",
            {"device_id": device_id, "name": name, "enabled": enabled},
            label="set-toggle",
        )

    async def play_asset(self, device_id: str, asset: str) -> bool:
        """Play a pre-rendered audio asset (purr, security tones, etc.).

        ``asset`` is a path xiaozhi-server resolves in its own
        filesystem; 404 if it can't find the file.
        """
        return await self._post(
            "/xiaozhi/admin/play-asset",
            {"device_id": device_id, "asset": asset},
            label="play-asset",
        )

    async def take_photo(self, device_id: str, question: str) -> bool:
        """Relay a `self.camera.take_photo` MCP frame to the device.

        The image arrives back at dotty-behaviour via the firmware's
        usual POST to /api/vision/explain. Callers long-poll
        /api/vision/latest (or read perception_state.vision_cache
        directly when in-process) after this returns.

        Returns False if the xiaozhi-server side doesn't yet expose
        /xiaozhi/admin/take-photo (404). Never raises.
        """
        return await self._post(
            "/xiaozhi/admin/take-photo",
            {"device_id": device_id, "question": question},
            label="take-photo",
        )

    async def capture_audio(
        self, device_id: str, duration_ms: int = 4000
    ) -> bool:
        """Relay a `self.audio.capture_clip` MCP frame.

        Returns False if the relay route is missing. Same loose-contract
        pattern as take_photo — firmware capture may not yet exist.
        """
        return await self._post(
            "/xiaozhi/admin/capture-audio",
            {"device_id": device_id, "duration_ms": duration_ms},
            label="capture-audio",
        )
