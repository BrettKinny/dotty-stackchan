---
title: States, Toggles & LED Contract
description: Authoritative taxonomy of Dotty's high-level modes — a six-state mutex with two orthogonal toggles, owned by the firmware StateManager and surfaced via a 12-pixel LED ring contract.
---

# States, Toggles & LED Contract

This document is the source of truth for Dotty's high-level modes. The model has two axes:

- **STATE** — what Dotty is *doing right now*. Mutually exclusive — exactly one State is active. Six values: `idle`, `talk`, `story_time`, `security`, `sleep`, `dance`.
- **TOGGLES** — orthogonal modifiers that can be on regardless of state. Two values today: `kid_mode`, `smart_mode`. Toggles compose freely with state.

The firmware **StateManager** modifier (`firmware/main/stackchan/modes/state_manager.cpp`) owns both axes. It paints the state arc (left ring 0-5) + toggle pips at 5 Hz, drives the idle-motion profile, and emits `state_changed` perception events on every transition.

Pair this with [hardware.md](./hardware.md) (the physical LED ring + servos) and [interaction-map.md](./interaction-map.md) (the underlying signals).

---

## TL;DR

| Axis | Cardinality | Examples | Owner |
|---|---|---|---|
| State | mutex (1 of 6) | `idle`, `talk`, `story_time` | firmware StateManager |
| Toggle | compose freely | `kid_mode`, `smart_mode` | firmware StateManager |
| Chat sub-state | nested under `talk` / `story_time` | listening (LED) / thinking + speaking (face only) | xiaozhi-server |

The firmware boots into `idle` with both toggles **off**. The bridge resyncs toggles from disk on the first turn after each reconnect. State transitions land via voice phrases, camera edges (face_detected → `talk`), or `/admin/state` from the dashboard.

**Speech sub-states** are conveyed by face animations (eye gestures, talking mouth) and the dedicated **listening pixel** at right-ring index 11. `thinking` and `speaking` have no LED — they live on the face. `listening` lights pixel 11 red so the user knows when their voice is being captured as a turn.

---

## States (mutually exclusive)

| State | LED arc (left ring 0-5) | Idle profile | Behaviour | Backing path |
|---|---|---|---|---|
| `idle` | off `(0,0,0)` | NORMAL | Ambient awareness, gentle idle motion. Default. | n/a (no chat in flight) |
| `talk` | dim green `(0,60,0)` | NORMAL (face_tracking overlay active) | Conversation engaged. Listening pixel (right 6) lights red while the user has the turn; `thinking` and `speaking` are face-animation only. | xiaozhi → bridge → ZeroClaw ACP |
| `story_time` | warm `(100,40,0)` | NORMAL | Long-running interactive story. Bridge bypasses ZeroClaw, calls OpenRouter directly with story persona + rolling context. | bridge → direct OpenRouter (Phase 7) |
| `security` | white `(80,80,80)` **flashing 1 Hz** across all 6 left pixels | SURVEILLANCE | Wide deliberate scan, serious face, periodic photo + audio capture. No proactive greet. | bridge ambient task (Phase 6) |
| `sleep` | very dim blue `(0,0,16)` | SLEEPY | Head face-down + centred, servo torque off, sleeping emoji on screen, ambient awareness paused. Wakes on face / voice / head-pet. | firmware-only quiescence (Phase 5) |
| `dance` | rainbow sweep (left ring) | NORMAL | Transient performance — choreography + audio. Pre-existing dance handler. | `receiveAudioHandle.py::_handle_dance` |

The `idle → talk` trigger is the firmware `face_detected` event (any face, family or stranger). The bridge runs VLM recognition (`bridge.py::_capture_room_view`) in parallel and feeds the resulting identity into the speaker resolver / persona — recognition does **not** gate the state transition.

### Mutex rules

1. Exactly **one** state is current. `setState(S)` to the same state is a no-op.
2. State transitions are explicit — no implicit "fallback" to idle from other states; each non-idle state has its own exit triggers.
3. Camera edges only auto-transition between `idle` ↔ `talk`. Sticky states (`story_time`, `security`, `sleep`, `dance`) ignore face_detected / face_lost.

---

## Toggles (compose freely)

| Toggle | Toggle pip (right ring) | What it does | Persistence |
|---|---|---|---|
| `kid_mode` | warm pink `(168,80,100)` at index **8** | Guardrails only — content sandwich, camera tools denied, kid-safe persona. Does not pick the model. | `/root/zeroclaw-bridge/state/kid-mode` |
| `smart_mode` | orange `(168,80,0)` at index **9** | Voice-daemon model selector. ON → `SMART_MODEL` (claude-sonnet-4-6 by default); OFF → `DEFAULT_MODEL` (mistral-small-3.2-24b-instruct). ZeroClaw retains full memory + tools either way. | `/root/zeroclaw-bridge/state/smart-mode` |

The two toggles are orthogonal — they compose freely. `kid_mode = on` AND `smart_mode = on` runs the smart model behind the kid-safe sandwich. Both toggles are sticky across turns, daemon restarts, and reboots.

`smart_mode` is **dashboard- and admin-endpoint-only** — there is no voice trigger. Kids reach Dotty by voice but not the web dashboard, so dashboard-only is the access-control gate that keeps the more capable (and more expensive) model under household-head control.

---

## LED contract (12-pixel ring)

```
LEFT RING (global 0–5)              RIGHT RING (global 6–11)
┌───────────────────┐               ┌────────────────────────────┐
│ 0  state arc      │               │ 6  face state (TOP)        │
│ 1  state arc      │               │ 7  reserved (locked off)   │
│ 2  state arc      │               │ 8  kid_mode toggle         │
│ 3  state arc      │               │ 9  smart_mode toggle       │
│ 4  state arc      │               │ 10 reserved (locked off)   │
│ 5  state arc      │               │ 11 listening (BOTTOM)      │
└───────────────────┘               └────────────────────────────┘
```

| Index | Half | Owner | Behaviour |
|---|---|---|---|
| 0–5 | left | StateManager (state arc) | All six paint the current mutex-state colour. Dance suppresses and lets the rainbow animation own the ring. |
| 6 | right | StateManager (face state pip) | Yellow `(168,140,0)` when a face is detected; green `(0,140,30)` when the bridge has identified the face via room-view VLM + roster match (mutex on the same pixel). Identified state has a 4 s firmware-side timeout — bridge refreshes by calling `self.robot.set_face_identified` again on each successful match. |
| 7, 10 | right | StateManager (locked off) | Reserved for future indicators (low-battery is a known candidate). Re-asserted to `(0,0,0)` every 200 ms as defense-in-depth. |
| 8 | right | StateManager (`kid_mode` pip) | Warm pink `(168,80,100)` when kid_mode = on; off otherwise. |
| 9 | right | StateManager (`smart_mode` pip) | Orange `(168,80,0)` when smart_mode = on; off otherwise. |
| 11 | right | StateManager (listening pip) | Red `(120,0,0)` while xiaozhi is in `LISTENING` (mic open, ASR active, user's turn); off otherwise. Driven by `stackchan_display.cc::set_listening_pixel` which now routes through `StateManager::setListening(bool)` and emits a `chat_status` perception event for the dashboard mirror. Bottom of the right ring; spatially separated from the toggle pips. |

### LED quirks

- **5 Hz tick.** StateManager re-paints the state arc AND the entire right ring (face / kid / smart / listening / reserved 7 / reserved 10) every ~200 ms. The tick drives the SECURITY 1 Hz flash and the face-identified 4 s timeout, and acts as defense-in-depth re-assert across all status indicators — MCP writes / dance keyframes / future writers cannot persistently clobber any pixel (worst case: 200 ms flicker).
- **PY32 IO expander quantises to RGB565.** Brightness deltas crush — `(40,40,40)` reads almost identical to `(200,200,200)`. Use distinct **hues**, not brightness levels, for any indicator that needs to read across a room.
- **MCP tools are contract-aware.** `self.robot.set_led_color` and `self.robot.set_led_multi` are restricted to the LEFT ring only (indices 0-5). Attempts to write right-ring indices via these tools are rejected with a warn log. Use `self.robot.set_face_identified` (no args) to light the face pixel green for ~4 seconds.
- **Dance choreography only animates the left ring.** `Keyframe::apply` no longer writes the right ring. Custom JSON dances that set `rightRgbColor` will see that field preserved on the `Keyframe` struct but not applied to hardware.
- **RightNeonLight uses local indices 0–5** internally, mapped to global 6–11 via `+6`. StateManager constants: `kFacePipRightLocal=0`, `kReservedPipRightLocal_7=1`, `kKidModePipRightLocal=2`, `kSmartModePipRightLocal=3`, `kReservedPipRightLocal_10=4`, `kListeningPipRightLocal=5`.
- **Dashboard mirror.** The bridge dashboard at `/ui/led-ring-mirror` shows all four indicators in the same colours as the physical ring, updated via 2 s HTMX polling + `dotty-refresh` event nudges fired by SSE perception events (`face_detected`, `face_lost`, `face_recognized`, `chat_status`).

---

## State transitions

```mermaid
stateDiagram-v2
    [*] --> idle

    idle --> talk: face_detected (firmware)
    talk --> idle: face_lost grace expired (firmware)

    idle --> sleep: voice "go to sleep" / "goodnight Dotty"
    sleep --> idle: face_detected / voice "wake up" / head_pet

    idle --> security: voice "keep watch" / "security mode"
    security --> idle: voice "wake up" / face_detected (Phase 6)

    idle --> story_time: voice "tell me a story"
    story_time --> idle: voice "the end" / "stop story" / 90 s silence (Phase 7)

    idle --> dance: voice "dance" / song name
    dance --> idle: choreography ends

    talk --> sleep: voice "goodnight Dotty"
    talk --> story_time: voice "tell me a story"
    talk --> dance: voice "dance"
```

### Voice triggers (Phase 4)

| Phrase (substring, case-insensitive) | Target state |
|---|---|
| `goodnight dotty` / `good night dotty` / `go to sleep` | `sleep` |
| `keep watch` / `security mode` / `watch the room` | `security` |
| `tell me a story` / `story time` | `story_time` |
| `wake up` / `come back` / `are you there` (only when state ∈ `{sleep, security, story_time}`) | `idle` |

Both `kid_mode` and `smart_mode` are voice-untoggleable — they are guardian-controlled axes driven from the `/admin/kid-mode` and `/admin/smart-mode` endpoints (or the dashboard cards that wrap them).

### Admin endpoints (localhost-only)

| Endpoint | Body | Effect |
|---|---|---|
| `POST /admin/state` | `{"state": "<idle\|talk\|story_time\|security\|sleep\|dance>", "device_id": "<optional>"}` | Push `self.robot.set_state` MCP via xiaozhi-server relay. No daemon restart. |
| `POST /admin/smart-mode` | `{"enabled": bool, "device_id": "<optional>"}` | Persist + push pip + rewrite voice-daemon `config.toml` model + schedule daemon restart. ON → `SMART_MODEL`, OFF → `DEFAULT_MODEL`. |
| `POST /admin/kid-mode` | `{"enabled": bool}` | Persist + push pip + restart daemon (guardrail constants are baked at module import). No model swap — that's `smart_mode`'s job. |

### MCP tools (firmware)

| Tool | Arguments | Caller |
|---|---|---|
| `self.robot.set_state` | `{"state": "<...>"}` | xiaozhi-server `/admin/set-state` relay |
| `self.robot.set_toggle` | `{"name": "kid_mode\|smart_mode", "enabled": bool}` | xiaozhi-server `/admin/set-toggle` relay; receiveAudioHandle.py voice phrases |

---

## Backing architecture per state

| State | Voice path | Memory? | Tools? |
|---|---|---|---|
| `idle` | n/a | n/a | n/a |
| `talk` | xiaozhi → bridge → ZeroClaw ACP. Daemon model = `SMART_MODEL` (sonnet-4-6) when smart_mode=on, else `DEFAULT_MODEL` (mistral-small). | yes (FTS) | yes |
| `story_time` | xiaozhi → bridge → direct OpenRouter (story persona overlay + rolling context) | per-session list (Phase 7) | no |
| `security` | bridge ambient task (no voice path active) | logs to journal | photo + audio capture |
| `sleep` | mic stays on for "wake up"; no LLM round-trip | n/a | n/a |
| `dance` | bridge handler dispatches choreography + audio file | n/a | dance MCP |

`smart_mode` swaps the voice daemon's model and restarts the daemon — every voice turn still goes through ZeroClaw (full memory + tools), just on the smart model. `story_time` is the only voice path that bypasses ZeroClaw, with its own session memory (Phase 7).

---

## Implementation status

| Phase | Scope | Status |
|---|---|---|
| 4 | StateManager foundation: state pip + toggle pips + state_changed event + voice phrases + admin endpoints + dashboard | **shipping** |
| 5 | Sleep state behaviour (servo park + torque off + sleepy emoji + wake triggers) | pending |
| 6 | Security state behaviour (periodic photo + audio capture, greeter gate) | pending |
| 7 | Story_time state (interactive setup, OpenRouter session, choose-your-own-adventure) | pending |
| 8 | Ambient awareness loop (idle-state photo + audio scene capture, journal) | pending |

Phase 4 establishes the *rails* — pip, transition events, dispatch helpers, voice routing. Phases 5–8 each hang behaviour off those rails without changing the architecture.

---

## Sources of truth

- **Firmware:** `firmware/main/stackchan/modes/state_manager.{h,cpp}`, `firmware/main/stackchan/modifiers/face_tracking.cpp` (camera-edge hooks), `firmware/main/hal/hal_mcp.cpp` (set_state / set_toggle MCP)
- **Bridge:** `bridge.py` (`_dispatch_set_state`, `_dispatch_set_toggle`, `_admin_state`, `_admin_smart_mode`, `_admin_kid_mode`, `_apply_model_swap`, `_update_perception_state` for `state_changed`), `receiveAudioHandle.py` (voice state phrases + per-conn toggle sync)
- **xiaozhi-server patches:** `custom-providers/xiaozhi-patches/http_server.py` (`/xiaozhi/admin/set-state`, `/xiaozhi/admin/set-toggle`), `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py` (`state_changed` → `conn.current_state`)
- **Dashboard:** `bridge/dashboard.py` + `bridge/templates/state_card.html` + `bridge/templates/smart_mode.html`
