# dotty-pi-ext

Pi extension that exposes Dotty's voice tools to the pi-based voice
runtime. Installed inside the [`dotty-pi`](../dotty-pi/) container at
`/root/.pi/extensions/dotty-pi-ext/`, surfaced to the agent via pi's
extension manifest.

**Status: 4 of 5 tools ported + integration-tested end-to-end inside
the live dotty-pi container.** `take_photo` is still pending, blocked
on the perception-cache rehoming design (#36's "9 dashboard perception
consumers" pile). Bridge.py is still the source of truth in production
until cutover.

## The five voice tools

These are the tools that voice turns invoke during conversation. Each
replicates the semantics of the matching `_voice_tool_*` handler (or
matching `/api/voice/*` endpoint) in `bridge.py` (port the function
bodies; the contracts below summarise them).

| Tool | What it does | Source-of-truth handler |
|---|---|---|
| `memory_lookup(query)` | FTS5 search against `brain.db`; returns top-3 snippets pipe-joined, ≤200 chars each. | `bridge.py::_voice_tool_memory_lookup` |
| `remember(fact)` | Stores a durable fact (≤300 codepoints, trimmed) into the `memories` table with `category=core`, `importance=0.7`. Mirrors bridge.py's `/api/voice/remember` HTTP endpoint, but called as a tool from inside the agent loop rather than as a side-channel POST. | `bridge.py::voice_remember` (`/api/voice/remember`) |
| `think_hard(question)` | Direct POST to llama-swap `qwen3.6:27b-think` with `enable_thinking=false`, 200-token cap, terse 1-2 sentence answer. | `bridge.py::_voice_tool_think_hard` |
| `take_photo()` | Returns the latest cached vision description if ≤30 s old, otherwise a "can't see" reply. v2 will actively fire the take_photo MCP and await fresh capture. | `bridge.py::_voice_tool_take_photo` |
| `play_song(name)` | Resolves free-form name against xiaozhi's `/xiaozhi/admin/songs` catalogue (60 s cache), then POSTs `/xiaozhi/admin/play-asset`. | `bridge.py::_voice_tool_play_song` |

### Not a tool: LED control

The 12-pixel LED ring is **reserved for mode/state indication** and is
not voice-controllable. Both the LEFT ring (0-5 state arc) and the
RIGHT ring (6-11 status pips) are owned by the firmware's StateManager;
there is no `set_led` tool, and adding one would fight the 5 Hz state-
arc re-assert. A voice-driven LED tool was listed as a #36 Step-5
carryover but is explicitly out of scope per product decision —
LEDs belong to the state machine.

## Migration constraints

Lifted from #36 — keep these visible at the top of every implementation
PR:

- **Keep the pi RPC process alive across turns** (don't respawn per
  voice turn — recover the per-turn startup tax that the spike report
  measured at 1.2–1.8 s).
- **Auto-respond `{cancelled: true}` to any `extension_ui_request`** —
  pi otherwise blocks waiting for a dialog response that no one is
  there to give.
- **Filter `assistantMessageEvent.type == "thinking_delta"`** out of
  the stream that reaches xiaozhi. The spike measured 19 thinking
  deltas vs 3 text deltas per turn; Dotty must not speak the reasoning.

These are PiClient-side responsibilities (see
[`../custom-providers/pi_voice/README.md`](../custom-providers/pi_voice/README.md)),
but extension authors should be aware so tool-result framing matches
what gets filtered through to xiaozhi.

## Layout (planned)

```
dotty-pi-ext/
├── README.md             # this file
├── package.json          # pi extension manifest
├── src/
│   ├── index.ts          # entry: registers tools, wires deps
│   ├── tools/
│   │   ├── memory_lookup.ts
│   │   ├── think_hard.ts
│   │   ├── take_photo.ts
│   │   ├── play_song.ts
│   │   └── set_led.ts
│   └── lib/
│       ├── brain_db.ts   # FTS5 client (sqlite3, opened against /root/.pi/memory/brain.db)
│       └── xiaozhi.ts    # admin-endpoint client (songs catalogue, play-asset, MCP dispatch)
└── tests/                # per-tool contract tests against the bridge.py reference
```

## Open questions for the implementation pass

1. **Tool registration shape.** Pi's extension API expects a specific
   manifest format — verify against `pi --extension-help` before
   committing to `package.json` structure.
2. **brain.db concurrency.** xiaozhi-server's perception path also
   writes to memory. SQLite WAL mode + a single writer convention is
   probably enough; needs explicit test under the new layout.
3. **xiaozhi-admin auth.** The current bridge talks unauthenticated to
   `<XIAOZHI_HOST>:8003/xiaozhi/admin/*` — fine on the RPi loopback,
   but the new container is xiaozhi-adjacent on Unraid. Decide whether
   to keep that path or switch to a UNIX socket.

## See also

- [`../dotty-pi/README.md`](../dotty-pi/README.md) — runtime image.
- [`../custom-providers/pi_voice/README.md`](../custom-providers/pi_voice/README.md) — xiaozhi-side glue + PiClient.
- [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36) — cutover plan + Step-5 build constraints.
