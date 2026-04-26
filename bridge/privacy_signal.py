"""Privacy LED upload-signal helper.

Tells the firmware "data is leaving the LAN" so it can pulse the
corresponding privacy LED (mic green or camera red). Pairs with the
firmware-side `Privacy` (DataType 0x1A) WS handler in
`firmware/main/hal/hal_ws_avatar.cpp` and the `setMicWanBound` /
`setCameraUploading` API on `PrivacyLeds`.

The signal is *advisory* — the firmware also enforces a 2 s failsafe
timeout on each WAN-bound flag, so a missing `upload_end` (bridge crash,
network hiccup) cannot leave the LED stuck pulsing forever. That makes
this helper deliberately fire-and-forget: if the transport fails, we
log and move on; the firmware self-heals.

TRANSPORT NOTES
---------------

The firmware avatar WS connects to `http://localhost:3000/stackChan/ws`
by default — the upstream M5Stack Go-server endpoint. Today the live
deployment does NOT run that server, so this helper is a no-op-with-log
in production. It is wired into `_call_vision_api` already so that if
the avatar server is ever brought up (or replaced with a bridge-hosted
endpoint), the camera-upload pulse starts working with zero further
bridge changes.

The shape of the API (`signal_camera_upload`, `signal_mic_upload`) is
the chokepoint; transport can be swapped out underneath without
touching `bridge.py`.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Optional

log = logging.getLogger("zeroclaw-bridge.privacy_signal")

# Module-level transport hook. Production deploys override this once a
# real WS / admin transport exists; tests override it to capture calls.
# Signature: async (kind: "mic" | "camera", phase: "start" | "end") -> None
PrivacySender = Callable[[str, str], Awaitable[None]]
_sender: Optional[PrivacySender] = None


def set_privacy_sender(sender: Optional[PrivacySender]) -> None:
    """Install (or clear) the module-level sender used by signal_*."""
    global _sender
    _sender = sender


async def signal_mic_upload(phase: str) -> None:
    """Tell the firmware that mic audio is (start) / is no longer (end)
    crossing the LAN boundary. `phase` must be "start" or "end".

    NEVER raises. Firmware enforces a 2 s failsafe so missed `end`
    signals self-heal."""
    await _signal("mic", phase)


async def signal_camera_upload(phase: str) -> None:
    """Tell the firmware that camera frames (or derivatives) are
    (start) / are no longer (end) crossing the LAN boundary. `phase`
    must be "start" or "end".

    NEVER raises. Firmware enforces a 2 s failsafe so missed `end`
    signals self-heal."""
    await _signal("camera", phase)


async def _signal(kind: str, phase: str) -> None:
    if kind not in ("mic", "camera"):
        log.warning("privacy_signal: bad kind=%s", kind)
        return
    if phase not in ("start", "end"):
        log.warning("privacy_signal: bad phase=%s", phase)
        return
    if _sender is None:
        # No transport wired — log at debug so we don't spam the live
        # log while the avatar WS server is undeployed. Tests / a
        # deployed avatar server install a real sender.
        log.debug("privacy_signal: no sender wired (kind=%s phase=%s)", kind, phase)
        return
    try:
        await _sender(kind, phase)
    except Exception:
        log.warning("privacy_signal: sender raised (kind=%s phase=%s)", kind, phase, exc_info=True)


@asynccontextmanager
async def camera_upload_pulse() -> AsyncIterator[None]:
    """Async context manager that brackets a cloud vision call with
    `start` / `end` privacy signals. The `end` is in a `finally`, so it
    fires even if the wrapped call raises.

    Usage::

        async with camera_upload_pulse():
            result = await asyncio.to_thread(_call_vision_api, ...)
    """
    await signal_camera_upload("start")
    try:
        yield
    finally:
        await signal_camera_upload("end")


@asynccontextmanager
async def mic_upload_pulse() -> AsyncIterator[None]:
    """Async context manager pairing mic upload start / end. Symmetric
    twin of `camera_upload_pulse`. Currently UNWIRED — there is no
    clean bridge-side hook for the start of a xiaozhi ASR upload (the
    audio frames are streamed straight from device to xiaozhi-server,
    which the bridge does not observe). Kept here so that if/when an
    ASR-start hook is added (e.g. a xiaozhi-server patch that calls a
    bridge webhook on VAD-active), wiring it through this context
    manager is a one-liner."""
    await signal_mic_upload("start")
    try:
        yield
    finally:
        await signal_mic_upload("end")


__all__ = [
    "camera_upload_pulse",
    "mic_upload_pulse",
    "set_privacy_sender",
    "signal_camera_upload",
    "signal_mic_upload",
]
