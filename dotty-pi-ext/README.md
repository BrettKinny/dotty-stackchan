# dotty-pi-ext

Pi extension that exposes Dotty's voice tools to the pi-based voice
runtime. Installed inside the [`dotty-pi`](../dotty-pi/) container at
`/root/.pi/extensions/dotty-pi-ext/`, surfaced to the agent via pi's
extension manifest.

**Status: 7 of 7 tools live.** The original five (`memory_lookup`,
`remember`, `think_hard`, `take_photo`, `play_song`) plus the two
person-memory tools added in #53 (`recall_person`, `remember_person`).
`take_photo` reads from the `dotty-behaviour` daemon
(`GET /api/voice/take_photo`). The #36 cutover executed on 2026-05-19;
PiVoiceLLM is the live default voice path and this extension is the
production source of truth for these tools.

## The seven voice tools

These are the tools that voice turns invoke during conversation. The
original five replicate the semantics of the matching `_voice_tool_*`
handler (or `/api/voice/*` endpoint) in `bridge.py`; `recall_person` /
`remember_person` are pi-native, added in #53.

| Tool | What it does | Source-of-truth handler |
|---|---|---|
| `memory_lookup(query)` | FTS5 search against `brain.db`; returns top-3 snippets pipe-joined, ≤200 chars each. | `bridge.py::_voice_tool_memory_lookup` |
| `remember(fact)` | Stores a durable fact (≤300 codepoints, trimmed) into the `memories` table with `category=core`, `importance=0.7`. Mirrors bridge.py's `/api/voice/remember` HTTP endpoint, but called as a tool from inside the agent loop rather than as a side-channel POST. | `bridge.py::voice_remember` (`/api/voice/remember`) |
| `recall_person(name)` | Reads up to 6 approved per-person facts from the `person:<id>` namespace in `brain.db` (case-insensitive name match), each ≤200 chars, pipe-joined. The pi runtime has no system-prompt-injection seam, so per-person memory is surfaced as a tool rather than bridge.py's injected `[Person memory]` block. | pi-native (#53) |
| `remember_person(name, fact)` | Stores a ≤300-char fact about a named household member. Asks dotty-behaviour's `/api/voice/person_review_status` classifier first; facts about minors are held in `person_pending:<id>` for human review, adults go straight to `person:<id>`. | pi-native (#53) |
| `think_hard(question)` | Direct POST to llama-swap `qwen3.6:27b-think` with `enable_thinking=false`, 200-token cap, terse 1-2 sentence answer. | `bridge.py::_voice_tool_think_hard` |
| `take_photo()` | GET to dotty-behaviour `/api/voice/take_photo` — returns the latest cached vision description if ≤30 s old, otherwise a "can't see" reply. v2 will actively fire the take_photo MCP and await fresh capture. | `dotty-behaviour::routes/voice.py` (lift of `bridge.py::_voice_tool_take_photo`) |
| `play_song(name)` | Resolves free-form name against xiaozhi's `/xiaozhi/admin/songs` catalogue (60 s cache), then POSTs `/xiaozhi/admin/play-asset`. | `bridge.py::_voice_tool_play_song` |

### Not a tool: per-turn auto-log

After each completed user prompt, an `agent_end` handler in
`src/lib/turn_logger.ts` writes a `category=conversation`,
`importance=0.3` row to `brain.db` summarising the turn. This is the
live conversation write path on the PiVoiceLLM voice path (it took
over from bridge.py's retired `/api/voice/memory_log` endpoint at the
#36 cutover). Lives as an event subscription rather than a tool
because the agent doesn't decide to log — every successful prompt
gets recorded automatically.

Content shape (matches the bridge endpoint byte-for-byte):
`"user: {user[:500]} | assistant: {assistant[:1000]}"`, both halves
`.strip()`ed first, truncation is codepoint-aware (matches Python
`str[:N]` semantics, not JS `String.slice()`).

Skips writing when both halves are empty (e.g. a pure-tool turn
with no text reply). Errors are swallowed and logged to stderr —
the agent loop never fails because of memory write trouble.

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

## Layout

```
dotty-pi-ext/
├── README.md             # this file
├── package.json          # pi extension manifest
├── src/
│   ├── index.ts          # entry: registers the 7 tools, wires the agent_end logger
│   ├── tools/
│   │   ├── memory_lookup.ts
│   │   ├── remember.ts
│   │   ├── recall_person.ts
│   │   ├── remember_person.ts
│   │   ├── think_hard.ts
│   │   ├── take_photo.ts
│   │   └── play_song.ts
│   └── lib/
│       ├── brain_db.ts        # FTS5 client (sqlite3, opened against /root/.pi/memory/brain.db)
│       ├── dotty_behaviour.ts # dotty-behaviour client (person-review classifier, take_photo)
│       ├── llama_swap.ts      # llama-swap client (think_hard escalation)
│       ├── turn_logger.ts     # agent_end per-turn conversation auto-log
│       └── xiaozhi_admin.ts   # admin-endpoint client (songs catalogue, play-asset, MCP dispatch)
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
