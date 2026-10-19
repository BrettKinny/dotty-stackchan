"""Shared registry of active StackChan device WebSocket handlers.

Populated by the patched WebSocketServer when devices connect/disconnect;
read by the patched HTTP server's /xiaozhi/admin/inject-text route so
the Dotty admin dashboard can fire `startToChat` against an active device
connection (which is what the bridge needs to make the device actually
speak / emote / fire MCP tools — the bridge has no WS to the device).

Also keeps the last StateManager state each device reported, so a new
WebSocket connection starts from the device's real state instead of the
`idle` default (#170). Without it, a reconnect while the firmware stays in
sleep / security / story_time made "wake up" ordinary conversation.

This file is mounted into the container at /opt/xiaozhi-esp32-server/core/
"""

from typing import Any

# device_id -> ConnectionHandler. Single-process asyncio so plain dict ops
# are race-free for our purposes.
active_connections: dict[str, Any] = {}

# device_id -> last state from a firmware `state_changed` event. Process
# lifetime only: after a server restart the firmware's re-emit on channel
# open (#21) repopulates it.
device_states: dict[str, str] = {}


def record_device_state(device_id: str, state: str) -> None:
    """Remember the latest firmware-reported state for `device_id`."""
    state = (state or "").strip().lower()
    if device_id and state:
        device_states[device_id] = state


def restore_device_state(conn: Any, device_id: str) -> str | None:
    """Seed a new connection with the device's last reported state.

    Called before the connection handles any message, so a live
    `state_changed` (including the firmware's re-emit on channel open)
    still overrides it. Returns the restored state, or None.
    """
    state = device_states.get(device_id or "")
    if state:
        conn.current_state = state
        conn._dotty_desired_state = state
    return state
