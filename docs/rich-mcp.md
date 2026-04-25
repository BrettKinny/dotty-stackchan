---
title: Rich MCP Tool Surface
description: Expose firmware MCP tools to the LLM as native tool-use for embodied behaviour during conversation.
---

# Rich MCP Tool Surface (`DOTTY_RICH_MCP`)

Dotty's firmware advertises a set of MCP tools — head angle, LED ring,
screen brightness, reminders, camera, face recognition — over the
WebSocket handshake. With `DOTTY_RICH_MCP=true`, the bridge passes
those tool definitions through to the LLM as **native tool-use**, so a
single inference can produce both the spoken reply *and* embodied
actions in the same turn.

| Setting | Default | Effect |
| --- | --- | --- |
| `DOTTY_RICH_MCP=false` | yes | Voice-only behaviour. Existing
text-driven LED/head heuristics in `receiveAudioHandle.py` continue to
work unchanged. |
| `DOTTY_RICH_MCP=true`  | opt-in | LLM can call any non-filtered
firmware tool inline with the voice reply. |

## Why it matters

Without rich MCP, the bridge can only react to *text* the LLM produces
(emoji prefix → face animation, sentiment → LED). Rich MCP lets the
LLM be **deliberate** about embodied behaviour:

- "I'm over here on your left!" → head turn toward the speaker.
- "You sound tired." → LED dims to soft blue.
- "Could you set a 5-minute timer?" → `robot.create_reminder` fires.

This is the difference between *voice* and *presence*.

## How it's wired

```
LLM provider  →  bridge.rich_mcp.RichMCPToolSurface
                   │
                   ├─ tools_for_llm() → list of OpenAI/Anthropic tool defs
                   └─ dispatch(name, args, ws_send_func)
                                       │
                                       └─ bridge.rich_mcp_dispatch.build_ws_send_func(conn)
                                                  │
                                                  └─ MCP tools/call frame over WS
```

`RichMCPToolSurface` lives in `bridge/rich_mcp.py`. It owns the static
catalogue, the kid-mode filter, and the hard allowlist — even a
jailbroken LLM cannot call a tool not in the catalogue. Dispatch is
fully try/except guarded so a tool failure can never break the LLM
turn.

!!! info "Bridge wiring deferred"
    The scaffolded surface ships **without** the wire-up in `bridge.py`'s
    voice turn handler — that work is owned by a separate concurrent
    commit. Until that lands, `DOTTY_RICH_MCP` has no observable effect.
    The follow-up commit will instantiate the surface, pass
    `surface.tools_for_llm()` to the LLM call, and wire `dispatch` into
    the tool-call response path.

## Tool catalogue

| Tool | Params | Kid-mode | Example LLM use |
| --- | --- | --- | --- |
| `get_device_status` | _(none)_ | yes | Check current volume before adjusting. |
| `audio_speaker.set_volume` | `volume` 0-100 | yes | "Turn it up" → 70. |
| `screen.set_brightness` | `brightness` 0-100 | yes | Dim screen at bedtime. |
| `screen.set_theme` | `theme: light\|dark` | **no** | Adult-mode dark theme. |
| `camera.take_photo` | `question` | **no** | "What am I holding?" — vision query. Privacy LED hardware-tracks the camera regardless. |
| `robot.get_head_angles` | _(none)_ | yes | Verify head pose before a sequence. |
| `robot.set_head_angles` | `yaw, pitch, speed` | yes | "Look up at me" → `yaw=0, pitch=30, speed=200`. |
| `robot.set_led_color` | `red, green, blue` (0-168) | yes | Match emoji emotion: 😢 → blue. |
| `robot.set_led_multi` | `index, r, g, b` | yes | Status pip — one pixel = listening. |
| `robot.get_privacy_state` | _(none)_ | yes | Read hardware privacy LED state. |
| `robot.create_reminder` | `duration_seconds, message, repeat` | yes | "Remind me in 5 min." |
| `robot.get_reminders` | _(none)_ | yes | "What reminders do I have?" |
| `robot.stop_reminder` | `id` | yes | "Cancel the timer." |
| `robot.face_unlock` | `method, secret` | **no** | PIN entry → adult mode. |
| `robot.face_enroll` | `name` | **no** | Add new household member. |
| `robot.face_forget` | `name` | **no** | Remove enrolled face. |
| `robot.face_list` | _(none)_ | yes | Read-only list of known faces. |

When `DOTTY_KID_MODE=true` (the default), the bold-marked rows are
**filtered out of the tool definitions returned to the LLM**. The LLM
can't even see them, let alone call them. As a defence-in-depth layer,
`RichMCPToolSurface.dispatch` re-checks the filter at call time, so a
stale tool catalogue cached by an upstream provider can't bypass the
filter either.

### Why `screen.set_theme` is kid-mode-filtered

The `dark` theme is fine in itself, but the persona prompt is kid-tuned
for the `light` (daytime) visual style. We filter the whole tool rather
than try to enforce an arg-level rule the LLM might bypass — see the
`_KID_MODE_FILTERED` set in `bridge/rich_mcp.py`.

## Example transcript

```
user:  Hey Dotty, I'm over here on your left!
LLM:   [tool] robot.set_head_angles(yaw=-45, pitch=0, speed=300)
       [tool] robot.set_led_color(red=168, green=140, blue=0)
       [text] 😊 Hi! There you are.
bridge: emits MCP tools/call frames over WS, then plays TTS for the text.
```

```
user:  Set a timer for ten minutes.
LLM:   [tool] robot.create_reminder(duration_seconds=600, message="Time's up!")
       [text] 🤔 Okay, ten-minute timer started.
```

The LLM can choose to fire zero, one, or several tool calls per turn.
The bridge's existing emoji-prefix face-animation contract still
holds; rich MCP is *additive*.

## Privacy considerations

- `camera.take_photo` is **kid-mode-denied** at three layers:
    1. Hidden from the LLM tool catalogue (this module).
    2. Denied at the `request_permission` boundary in `bridge.py`
       (`MCP_TOOL_DENYLIST`).
    3. Hardware privacy LEDs (Layer 1) light when the camera engages,
       independent of LLM intent.
- `robot.face_enroll` and `robot.face_forget` change persistent
  authentication state; they're filtered in kid-mode so a child can't
  inadvertently enroll a stranger or wipe a parent.
- `robot.face_unlock` is the gateway to adult mode and is filtered in
  kid-mode for the same reason.

## Cross-references

- [Kid Mode](./kid-mode.md) — the persona-level child-safe guardrails
  that this module's filter complements.
- [Observability](./observability.md) — richer MCP usage shows up in
  the `dotty_request_duration_seconds` per-endpoint metric and the
  per-tool log lines emitted by `bridge.rich_mcp` and
  `bridge.rich_mcp_dispatch`.
- [Proactive Greetings](./proactive-greetings.md) — Layer 6 greetings
  can use rich MCP for non-verbal greeting gestures (head nod, LED
  pulse) once the bridge wire-up lands.
- `docs/mcp-tools-capture.json` — captured firmware tool advertisement
  used as the source of truth for the catalogue.
