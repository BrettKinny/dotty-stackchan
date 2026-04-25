"""Rich MCP tool surface — expose firmware MCP tools to the LLM as native
tool-use, gated behind the ``DOTTY_RICH_MCP=true`` env flag.

When enabled, the bridge advertises 14 firmware MCP tools to the LLM as
OpenAI/Anthropic-style tool definitions, so a single inference can
ground in voice and produce embodied actions in the same turn. Examples:

  - ``user: "I'm over here on your left!"``  -> LLM emits a
    ``tools/call`` for ``robot.set_head_angles(yaw=-45, pitch=0,
    speed=300)`` plus a verbal "Hi!".
  - ``user: "Show me you're sad."`` -> LLM picks the 😢 emoji prefix
    AND emits ``robot.set_led_color(0, 0, 168)`` to match.

The set of tools exposed is **filtered by kid-mode**. Camera and
face-enrollment tools are hidden when ``KID_MODE=true``; this is on top
of the existing ``MCP_TOOL_DENYLIST`` enforcement at the
``request_permission`` layer in ``bridge.py`` — defence in depth, not a
replacement.

Hard allowlist
--------------
A tool is dispatched only if it appears in ``_TOOL_DEFINITIONS`` AND
survives the kid-mode filter. If the LLM hallucinates a tool name
("self.delete_everything") the dispatcher returns
``{"error": "tool not in allowlist", ...}`` — a tool failure must NEVER
break the LLM turn, so every dispatch path is wrapped in try/except.

Bridge wiring deferred
----------------------
TODO: This module ships scaffolded but **not wired into the voice turn
handler**. A concurrent agent owns ``bridge.py`` writes for this commit
round (bridge wiring catchup). Wire-up is deferred to a follow-up
commit:

  1. Read ``DOTTY_RICH_MCP`` env var in ``bridge.py`` startup.
  2. Construct ``RichMCPToolSurface(kid_mode_provider=lambda: KID_MODE)``
     once at module level (or per-request if kid-mode becomes dynamic).
  3. Pass ``surface.tools_for_llm()`` to the LLM tool-use call in the
     voice turn handler.
  4. On a tool_call response from the LLM, call
     ``surface.dispatch(name, args, ws_send_func)`` where
     ``ws_send_func`` is built per-connection by
     :mod:`bridge.rich_mcp_dispatch`.

The shape here is intentionally bridge-agnostic: ``ws_send_func`` is a
plain async callable, so the dispatch module can wrap whichever
per-connection MCP send helper ``receiveAudioHandle.py`` exposes
without coupling this module to the connection-handler internals.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("zeroclaw-bridge.rich_mcp")


# ---------------------------------------------------------------------------
# Tool definitions — JSON Schema in the OpenAI/Anthropic tool-use shape
# ---------------------------------------------------------------------------
#
# Each entry is keyed by the BARE tool name (no "self." prefix). The
# firmware advertises tools as ``self.<name>``; the bridge strips the
# prefix before allowlist lookup, and the dispatcher re-adds it before
# emitting the ``tools/call`` MCP frame. Keep these definitions in sync
# with ``docs/mcp-tools-capture.json``.
#
# Description strings are deliberately verbose and example-laden — the
# LLM has no other context for *when* to fire a tool, so the description
# is doing all the prompting work.

_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    # ---- Read-only / status ------------------------------------------
    "get_device_status": {
        "description": (
            "Read the device's real-time status (audio volume, screen "
            "brightness, battery, network). Call this BEFORE adjusting "
            "any setting so you know the current value. Read-only and "
            "always safe."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    # ---- Audio --------------------------------------------------------
    "audio_speaker.set_volume": {
        "description": (
            "Set the speaker volume. Range 0-100. Call "
            "`get_device_status` first if the current volume is "
            "unknown. Typical conversational level is 40-60."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "volume": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Volume percentage 0-100.",
                },
            },
            "required": ["volume"],
            "additionalProperties": False,
        },
    },
    # ---- Screen -------------------------------------------------------
    "screen.set_brightness": {
        "description": (
            "Set the screen brightness 0-100. Lower in dim rooms; "
            "raise in sunlight. Default is around 75."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "brightness": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Brightness percentage 0-100.",
                },
            },
            "required": ["brightness"],
            "additionalProperties": False,
        },
    },
    "screen.set_theme": {
        "description": (
            "Switch the on-screen theme. 'light' is the daytime/kid "
            "default; 'dark' is for low-light adult use."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "theme": {
                    "type": "string",
                    "enum": ["light", "dark"],
                    "description": "Either 'light' or 'dark'.",
                },
            },
            "required": ["theme"],
            "additionalProperties": False,
        },
    },
    # ---- Camera (kid-mode FILTERED) -----------------------------------
    "camera.take_photo": {
        "description": (
            "Take a photo with the on-board camera and return a "
            "vision-model description. Use only when the user "
            "explicitly asks you to look at something. The privacy "
            "LED hardware-tracks camera activation regardless of "
            "tool intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you want to know about what "
                    "you're seeing (passed to the vision model).",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    # ---- Robot — head -------------------------------------------------
    "robot.get_head_angles": {
        "description": (
            "Return current head yaw/pitch in degrees. Neutral is "
            "{yaw:0, pitch:0}. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "robot.set_head_angles": {
        "description": (
            "Turn the head to face a direction. Yaw -128 to +128 "
            "(negative = your left, positive = your right; stay "
            "within +/-45 for natural conversation, beyond +/-70 "
            "only if asked to look far away). Pitch 0 to 90 (90 = "
            "looking up). Speed 100-1000 (150 is natural, 300+ "
            "feels alert, <150 feels sleepy)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "yaw": {
                    "type": "integer",
                    "minimum": -128,
                    "maximum": 128,
                    "description": "Horizontal angle in degrees.",
                },
                "pitch": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 90,
                    "description": "Vertical angle in degrees.",
                },
                "speed": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 1000,
                    "default": 150,
                    "description": "Servo speed; higher is faster.",
                },
            },
            "required": ["yaw", "pitch"],
            "additionalProperties": False,
        },
    },
    # ---- Robot — LEDs -------------------------------------------------
    "robot.set_led_color": {
        "description": (
            "Set ALL pixels of the internal neon-ring LED. Each "
            "channel 0-168. Use to match the emoji emotion of the "
            "reply: happy=warm yellow (168,140,0), sad=blue "
            "(0,0,168), angry=red (168,0,0), calm=soft green "
            "(0,168,0), off=(0,0,0). NOT for room lights."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "red": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 168,
                },
                "green": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 168,
                },
                "blue": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 168,
                },
            },
            "required": ["red", "green", "blue"],
            "additionalProperties": False,
        },
    },
    "robot.set_led_multi": {
        "description": (
            "Set a SINGLE pixel on the neon-ring without disturbing "
            "the others. Index 0-5 = left ring, 6-11 = right ring. "
            "Useful for status pips (e.g. one blue pixel = listening). "
            "Note: a subsequent `robot.set_led_color` will overwrite "
            "this pixel; re-call after a full-ring update if you "
            "need persistence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 11,
                    "description": "Pixel index 0-11.",
                },
                "r": {"type": "integer", "minimum": 0, "maximum": 168},
                "g": {"type": "integer", "minimum": 0, "maximum": 168},
                "b": {"type": "integer", "minimum": 0, "maximum": 168},
            },
            "required": ["index", "r", "g", "b"],
            "additionalProperties": False,
        },
    },
    # ---- Robot — privacy ---------------------------------------------
    "robot.get_privacy_state": {
        "description": (
            "Read the hardware privacy-LED state (camera/mic active "
            "indicators). Read-only; reflects the firmware's Layer 1 "
            "hardware tracking and is independent of LLM-facing tool "
            "intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    # ---- Robot — reminders -------------------------------------------
    "robot.create_reminder": {
        "description": (
            "Create a one-off or repeating reminder. `duration_seconds` "
            "is how long until it fires (1 to 86400, i.e. up to 24 h). "
            "`message` is what to say when the time is up. Set "
            "`repeat=true` to repeat indefinitely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 86400,
                },
                "message": {
                    "type": "string",
                    "description": "Spoken message when reminder fires.",
                },
                "repeat": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["duration_seconds", "message"],
            "additionalProperties": False,
        },
    },
    "robot.get_reminders": {
        "description": "List active reminders. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "robot.stop_reminder": {
        "description": "Cancel an active reminder by its integer id.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Reminder id from `get_reminders`.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    # ---- Robot — face recognition (kid-mode FILTERED for write ops) --
    "robot.face_unlock": {
        "description": (
            "Unlock adult-mode features via face/PIN. `method` is "
            "'face' or 'pin'; `secret` is the PIN string when "
            "method='pin'. Adult-mode only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["face", "pin"],
                },
                "secret": {
                    "type": "string",
                    "description": "PIN when method='pin'; ignored "
                    "for method='face'.",
                },
            },
            "required": ["method"],
            "additionalProperties": False,
        },
    },
    "robot.face_enroll": {
        "description": (
            "Enroll a new face under `name`. Adult-mode only — "
            "filtered out under kid-mode."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Identity label, e.g. 'brett'.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "robot.face_forget": {
        "description": (
            "Delete an enrolled face by `name`. Adult-mode only — "
            "filtered out under kid-mode."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Identity label to forget.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "robot.face_list": {
        "description": (
            "List enrolled face identities. Read-only and safe in "
            "any mode."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


# Tools to hide from the LLM when ``kid_mode_provider()`` is True.
# Layered defence:
#   - ``camera.take_photo`` is also in ``bridge.MCP_TOOL_DENYLIST``;
#     filtering at the surface keeps the LLM from being prompted to
#     call it in the first place.
#   - ``robot.face_*`` writes change auth state — adult-mode only.
#   - ``screen.set_theme`` (specifically 'dark') is a soft guard:
#     the persona prompt is kid-tuned and the dark theme breaks visual
#     style. The whole tool is filtered rather than per-arg, since the
#     LLM can't be trusted to honour an arg-level rule.
_KID_MODE_FILTERED: frozenset[str] = frozenset({
    "camera.take_photo",
    "robot.face_unlock",
    "robot.face_enroll",
    "robot.face_forget",
    "screen.set_theme",
})


# ---------------------------------------------------------------------------
# RichMCPToolSurface
# ---------------------------------------------------------------------------


class RichMCPToolSurface:
    """Owns the rich-MCP tool catalogue and dispatch.

    Parameters
    ----------
    kid_mode_provider:
        Zero-arg callable returning the *current* kid-mode flag. Called
        on every ``tools_for_llm()`` so a runtime kid-mode toggle is
        reflected without rebuilding the surface. The callable MUST
        NOT raise; if it does, kid-mode is assumed True (safer
        default — fewer tools exposed).

    perception_bus_subscriber:
        Optional hook reserved for future "fire embodied gesture in
        response to perception event" wiring (e.g. turn head toward
        sound source on YAMNet event). Currently unused; accepted so
        the constructor signature stays stable across the deferred
        bridge wiring.
    """

    def __init__(
        self,
        kid_mode_provider: Callable[[], bool],
        perception_bus_subscriber: Optional[Any] = None,
    ) -> None:
        self._kid_mode_provider = kid_mode_provider
        self._perception_bus_subscriber = perception_bus_subscriber

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def available_tool_names(self) -> set[str]:
        """All tool names known to this surface (regardless of mode)."""
        return set(_TOOL_DEFINITIONS.keys())

    def tools_for_llm(self) -> list[dict[str, Any]]:
        """Return tool definitions in the OpenAI/Anthropic tools shape.

        Kid-mode-filtered tools are excluded. Each entry has the keys
        ``name``, ``description``, and ``parameters`` (a JSON Schema
        ``object``) — compatible with both providers.
        """
        kid_mode = self._safe_kid_mode()
        out: list[dict[str, Any]] = []
        for name, defn in _TOOL_DEFINITIONS.items():
            if kid_mode and name in _KID_MODE_FILTERED:
                continue
            out.append({
                "name": name,
                "description": defn["description"],
                "parameters": defn["parameters"],
            })
        return out

    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ws_send_func: Callable[[str, dict[str, Any]], Awaitable[Any]],
    ) -> dict[str, Any]:
        """Invoke a firmware MCP tool via the WS send helper.

        Parameters
        ----------
        tool_name:
            Bare name (no "self." prefix). The dispatcher returns
            ``{"error": "tool not in allowlist"}`` for any name not in
            ``_TOOL_DEFINITIONS`` OR filtered by kid-mode — so a
            jailbroken LLM cannot reach a denied tool by guessing.

        arguments:
            JSON-serialisable arg dict. NOT validated against the
            schema here — the firmware enforces its own bounds and the
            schema's job is to *prompt* the LLM, not to gate the call
            (which would duplicate firmware logic).

        ws_send_func:
            Async callable with signature
            ``(tool_name: str, arguments: dict) -> Any``. Built per-
            connection by :mod:`bridge.rich_mcp_dispatch`. Whatever
            this returns is forwarded to the caller.

        Returns
        -------
        dict
            Either ``{"ok": True, "result": <ws_send_func return>}``
            on success, or ``{"error": "<reason>", ...}`` on any
            failure. NEVER raises — a tool failure must not break the
            LLM turn.
        """
        if tool_name not in _TOOL_DEFINITIONS:
            log.warning("rich_mcp dispatch rejected: %r not in allowlist", tool_name)
            return {
                "error": "tool not in allowlist",
                "tool": tool_name,
            }
        if self._safe_kid_mode() and tool_name in _KID_MODE_FILTERED:
            log.warning(
                "rich_mcp dispatch rejected: %r filtered by kid-mode",
                tool_name,
            )
            return {
                "error": "tool not in allowlist",
                "tool": tool_name,
                "reason": "kid_mode_filter",
            }
        if not isinstance(arguments, dict):
            log.warning(
                "rich_mcp dispatch rejected: %r non-dict arguments %r",
                tool_name, type(arguments).__name__,
            )
            return {
                "error": "arguments must be a JSON object",
                "tool": tool_name,
            }
        try:
            result = await ws_send_func(tool_name, arguments)
        except Exception as exc:  # pragma: no cover — defensive
            log.exception("rich_mcp dispatch raised for tool=%s", tool_name)
            return {
                "error": "dispatch failed",
                "tool": tool_name,
                "exception": repr(exc),
            }
        return {"ok": True, "tool": tool_name, "result": result}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _safe_kid_mode(self) -> bool:
        try:
            return bool(self._kid_mode_provider())
        except Exception:
            log.exception("kid_mode_provider raised; defaulting to True")
            return True
