"""Server-push helper for proactive utterances.

Layer 6 (proactive greetings) and any future server-initiated TTS path
go through this module so the server-push mechanism is documented
in one place.

NOTES on server-push mechanics
------------------------------

a) **xiaozhi WS protocol DOES support server-pushed TTS framing.**
   The existing dance audio path proves it: the server can push
   `tts/sentence_start`-style frames followed by Opus audio chunks
   directly down the device websocket without the device having
   first opened the mic. The same channel is used by the
   `/xiaozhi/admin/inject-text` and `/xiaozhi/admin/inject-tts`
   admin routes today.

b) **Current implementation reuses inject-text.** The simplest
   server-push surface (and the one Layer 1.5's face-greeter
   already uses) is HTTP POST to the xiaozhi-server admin endpoint.
   xiaozhi-server then runs the text through its configured TTS
   provider and pushes the resulting audio frames to the device.
   That goes through the normal TTS pipeline (cost, latency,
   selected voice) but requires zero firmware changes — which is
   why we start there for the proactive greeter.

c) **Future option: bypass-xiaozhi direct-Opus push** for lower
   latency and to bypass the TTS provider entirely (e.g., to play
   a pre-rendered Opus blob cached for "Good morning, Hudson!").
   That requires the bridge to either (a) hold its own websocket
   to the device, or (b) ask xiaozhi-server to forward an
   already-encoded Opus payload via a new admin route. Tracked as
   future work — not blocking Layer 6 scaffolding.

The function `push_greeting_audio(...)` exposed here is the
single chokepoint Layer 6 calls; flipping its implementation to a
direct-Opus path later does not require touching the greeter.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

log = logging.getLogger("zeroclaw-bridge.server_push")


# Resolved lazily so tests can patch env without importing bridge.py.
def _xiaozhi_admin_url() -> Optional[str]:
    host = os.environ.get("UNRAID_HOST", "")
    if not host:
        return None
    port = int(os.environ.get("UNRAID_OTA_PORT", "8003"))
    return f"http://{host}:{port}/xiaozhi/admin/inject-text"


async def push_greeting_audio(
    device_id: str,
    text: str,
    *,
    timeout: float = 3.0,
    inject_text_fn: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> bool:
    """Push a proactive utterance to a device.

    Routes through the existing xiaozhi-server inject-text admin route,
    same path the Layer 1.5 face-greeter uses. Returns True on
    successful POST, False on any error. NEVER raises.

    `inject_text_fn` (optional): an alternative async callable that
    accepts (device_id, text). Used for tests and to allow bridge.py
    to inject its own helper without us re-importing it (avoids a
    circular import).
    """
    if not text:
        log.warning("push_greeting_audio: empty text, skipping")
        return False
    if not device_id or device_id == "unknown":
        log.warning("push_greeting_audio: missing device_id, skipping")
        return False

    if inject_text_fn is not None:
        try:
            await inject_text_fn(device_id, text)
            return True
        except Exception:
            log.exception("push_greeting_audio: inject_text_fn raised")
            return False

    url = _xiaozhi_admin_url()
    if not url:
        log.warning(
            "push_greeting_audio: UNRAID_HOST not set; cannot reach "
            "xiaozhi-server (device=%s)",
            device_id,
        )
        return False

    payload = {"text": text, "device_id": device_id}

    def _post() -> bool:
        try:
            import requests as _req

            r = _req.post(url, json=payload, timeout=timeout)
            if r.status_code >= 400:
                log.warning(
                    "push_greeting_audio inject-text %s: %s",
                    r.status_code, r.text[:200],
                )
                return False
            return True
        except Exception as exc:
            log.warning("push_greeting_audio inject-text failed: %s", exc)
            return False

    try:
        return await asyncio.to_thread(_post)
    except Exception:
        log.exception("push_greeting_audio: to_thread raised")
        return False


__all__ = ["push_greeting_audio"]
