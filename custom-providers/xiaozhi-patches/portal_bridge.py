"""Shared registry of active StackChan device WebSocket handlers.

Populated by the patched WebSocketServer when devices connect/disconnect;
read by the patched HTTP server's /xiaozhi/admin/inject-text route so
the Dotty admin dashboard can fire `startToChat` against an active device
connection (which is what the bridge needs to make the device actually
speak / emote / fire MCP tools — the bridge has no WS to the device).

This file is mounted into the container at /opt/xiaozhi-esp32-server/core/
"""

from typing import Any

# device_id -> ConnectionHandler. Single-process asyncio so plain dict ops
# are race-free for our purposes.
active_connections: dict[str, Any] = {}

# device_id -> last firmware-owned mutex state observed on the wire. Unlike
# active_connections this intentionally survives a WebSocket reconnect: the
# robot can drop and reopen its audio channel without changing state, and a
# newly-created ConnectionHandler must not silently default to idle while the
# hardware is asleep/security-armed. This is a process-local bridge only;
# dotty-behaviour persists its own copy for daemon restarts.
last_known_states: dict[str, str] = {}


def state_for_device(device_id: str) -> str:
    """Return the last firmware state, failing safe for a new device."""
    return last_known_states.get(device_id, "idle")
