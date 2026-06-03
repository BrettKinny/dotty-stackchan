---
title: Protocols
description: Xiaozhi WebSocket protocol, pi RPC transport, emotion frame format, and the HTTP APIs served by dotty-behaviour and bridge.py.
---

# Protocols — what's on the wire

## TL;DR

- **Xiaozhi WebSocket protocol** — between device and xiaozhi-server. Opus audio + JSON control frames. Supports MCP over JSON-RPC 2.0 in-band. Canonical spec: `github.com/78/xiaozhi-esp32/blob/main/docs/websocket.md`.
- **Emotion channel** — 21 upstream emotion identifiers; the server picks one from the LLM's leading emoji and emits a separate `llm`-type frame. This stack uses a 9-emoji subset.
- **MCP over WS** — the device acts as an MCP server; xiaozhi-server calls `tools/list` and `tools/call` against it. Tool names use dotted namespaces like `self.audio_speaker.set_volume`.
- **pi RPC** — `PiClient` ↔ the dotty-pi agent communicate as JSONL messages over the stdin/stdout of `docker exec -i dotty-pi pi --mode rpc`. This is the voice transport for the default `PiVoiceLLM` provider.
- **HTTP APIs** — split across two services: dotty-behaviour (:8090) serves perception, vision, audio, and calendar endpoints; bridge.py (:8081) serves the admin dashboard `/ui` and admin routes.

## Xiaozhi WebSocket

**Transport.** TLS-optional WebSocket. Our deploy uses plain `ws://` on LAN. URL is given to the device via the OTA response on boot.

**Handshake headers.** The device sets `Authorization`, `Protocol-Version`, `Device-Id`, `Client-Id` on the upgrade request.

### Hello (device → server)

```json
{
  "type": "hello",
  "version": 1,
  "features": {"mcp": true, "aec": true},
  "transport": "websocket",
  "audio_params": {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

Device must receive a hello response within 10 s or it treats the channel as failed.

### Hello response (server → device)

```json
{
  "type": "hello",
  "transport": "websocket",
  "session_id": "xxx",
  "audio_params": {"format": "opus", "sample_rate": 24000}
}
```

The server picks the downlink sample rate (24 kHz above; uplink is 16 kHz from the device).

### Message-type catalog

| Type | Direction | Purpose |
|---|---|---|
| `hello` | device↔server | Handshake (see above) |
| `listen` | device→server | Mic state: `state: "start" \| "stop" \| "detect"`, `mode: "manual" \| "vad"` |
| `stt` | server→device | ASR result: `{"type":"stt","text":"…"}` |
| `tts` | server→device | TTS control: `state: "start" \| "stop" \| "sentence_start"` with optional `text` subtitle |
| `llm` | server→device | Emotion + leading emoji: `{"type":"llm","emotion":"happy","text":"😀"}` — see [emotion protocol](#emotion-protocol) |
| `mcp` | both | MCP JSON-RPC payload wrapped in `{"type":"mcp","payload":{…}}` |
| `system` | server→device | Device control, e.g. `{"command":"reboot"}` |
| `alert` | server→device | Notification, e.g. `{"status":"Warning","message":"Battery low","emotion":"sad"}` |
| `abort` | device→server | e.g. `{"reason":"wake_word_detected"}` to interrupt a response |

### Binary audio framing

Audio travels on the same WebSocket as binary frames. There are three defined framings — the device/server negotiate which one during hello.

**Version 1** — raw Opus payload, no metadata.

**Version 2** (`BinaryProtocol2`):
```c
struct BinaryProtocol2 {
    uint16_t version;
    uint16_t type;           // 0 = Opus, 1 = JSON
    uint32_t reserved;
    uint32_t timestamp;      // milliseconds (used for AEC alignment)
    uint32_t payload_size;
    uint8_t  payload[];
} __attribute__((packed));
```

**Version 3** (`BinaryProtocol3`):
```c
struct BinaryProtocol3 {
    uint8_t  type;
    uint8_t  reserved;
    uint16_t payload_size;
    uint8_t  payload[];
} __attribute__((packed));
```

**Default audio params.** Opus, mono, 16 kHz uplink / 24 kHz downlink, 60 ms frame duration.

### Keepalive and closure

The spec does not mandate a keepalive. Closure is driven by device `CloseAudioChannel()` or server disconnect; the firmware returns to idle.

## Emotion protocol

From [xiaozhi.dev/en/docs/development/emotion/](https://xiaozhi.dev/en/docs/development/emotion/).

### Full upstream emotion catalog (21 identifiers)

| Emoji | Identifier |
|---|---|
| 😶 | `neutral` |
| 🙂 | `happy` |
| 😆 | `laughing` |
| 😂 | `funny` |
| 😔 | `sad` |
| 😠 | `angry` |
| 😭 | `crying` |
| 😍 | `loving` |
| 😳 | `embarrassed` |
| 😲 | `surprised` |
| 😱 | `shocked` |
| 🤔 | `thinking` |
| 😉 | `winking` |
| 😎 | `cool` |
| 😌 | `relaxed` |
| 🤤 | `delicious` |
| 😘 | `kissy` |
| 😏 | `confident` |
| 😴 | `sleepy` |
| 😜 | `silly` |
| 🙄 | `confused` |

### Wire format

Server emits a dedicated `llm`-type frame:

```json
{"session_id":"xxx","type":"llm","emotion":"happy","text":"🙂"}
```

`text` contains the emoji character; `emotion` contains the identifier. The TTS frame that follows has the emoji **stripped** from its text so the speaker doesn't try to read it aloud.

### Default emoji allowlist

The persona prompt and xiaozhi-server's top-level `prompt:` block enforce the following 9-emoji subset:

```
😊 😆 😢 😮 🤔 😠 😐 😍 😴
```

Smaller set = more predictable face animations, fewer corner-cases in the xiaozhi emoji-stripper.

### Two-layer enforcement

1. **Persona prompt** (`personas/dotty_voice.md`) — asks for a leading emoji.
2. **xiaozhi-server top-level `prompt:`** — also asks for a leading emoji.

(A third bridge-side `_ensure_emoji_prefix` fallback existed in the retired ZeroClaw voice path; it is not present in the current `PiVoiceLLM` path.)

## MCP tools over WS

From `github.com/78/xiaozhi-esp32/blob/main/docs/mcp-protocol.md`.

### Advertisement

Device signals MCP support in `hello.features.mcp = true`. Server then queries the device for its tool list.

### `tools/list` request (server → device)

```json
{
  "session_id": "…",
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "method": "tools/list",
    "params": {"cursor": "", "withUserTools": false},
    "id": 2
  }
}
```

### `tools/list` response (device → server)

```json
{
  "session_id": "…",
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
      "tools": [
        {"name": "self.get_device_status", "description": "…", "inputSchema": {…}}
      ],
      "nextCursor": "…"
    }
  }
}
```

### `tools/call` request

```json
{
  "session_id": "…",
  "type": "mcp",
  "payload": {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "self.audio_speaker.set_volume",
      "arguments": {"volume": 50}
    },
    "id": 3
  }
}
```

### Success / error response

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"true"}],"isError":false}}
```

### Tool visibility — public vs user-only

- `McpServer::AddTool` — regular tool, exposed to `tools/list` by default. Available to the AI.
- `McpServer::AddUserOnlyTool` — hidden from the default `tools/list`. Requires `withUserTools: true`. For privileged actions the LLM shouldn't trigger (e.g. reboot).

See [hardware.md](./hardware.md#on-device-mcp-tools) for the default 11-tool MCP surface.

<a id="pi-rpc"></a>
## pi RPC — PiVoiceLLM transport

The `PiVoiceLLM` provider communicates with the dotty-pi agent via **pi RPC mode**: JSONL messages exchanged over the stdin/stdout of a `docker exec` invocation.

```
xiaozhi-server
  └─ PiClient
       └─ docker exec -i dotty-pi pi --mode rpc …
                             │           ▲
                    JSONL request        │
                    (stdin)              │ JSONL response
                                        │ (stdout, streamed)
```

Each turn is a single JSONL object written to stdin; the agent streams JSONL response chunks back on stdout. Only TTS-bound text chunks are forwarded to xiaozhi-server — tool call details stay internal to the agent loop. The agent exits cleanly after each turn; `PiClient` re-invokes `docker exec` for the next turn.

The dotty-pi agent loads the **dotty-pi-ext extension** at startup, which registers the five voice tools (`memory_lookup`, `remember`, `think_hard`, `take_photo`, `play_song`). Tool results never appear in the TTS stream.

<a id="http-apis"></a>
## HTTP APIs

Server-side HTTP is split across two services. All payloads are JSON unless noted.

### dotty-behaviour — perception, vision, audio, calendar (:8090)

`dotty-behaviour` is a FastAPI service (port 8090, same Docker host) that owns the ambient behaviour layer.

| Endpoint | Purpose |
|---|---|
| `POST /api/perception/event` | xiaozhi → dotty-behaviour perception relay (face, sound, state events) |
| `POST /api/vision/explain` | VLM describe-image call |
| `POST /api/audio/explain` | Audio event explanation |
| `POST /api/voice/take_photo` | Voice-triggered camera snapshot + VLM describe |
| `GET /api/calendar/*` | Calendar context queries |

`POST /api/perception/event` is the primary inbound path for firmware `event` frames forwarded by `EventTextMessageHandler` in `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py`:

```json
{
  "name": "<face_detected|face_lost|sound_event|state_changed|dance_started|dance_ended|chat_status|…>",
  "data": {"…": "…"},
  "device_id": "<xiaozhi device-id>",
  "session_id": "<xiaozhi session id>",
  "ts": 1715000000.0
}
```

Response: `{"ok": true}`. dotty-behaviour broadcasts the event to all perception listeners and updates per-device state (`dotty-behaviour/perception/state.py`). See [architecture.md](./architecture.md#perception-event-bus) for the 11 consumer classes (the running set is config-gated).

### bridge.py — dashboard and admin (:8081)

`bridge.py` is a FastAPI service (port 8081, same Docker host) that serves the admin dashboard. Its voice and perception relay roles were retired in issue #36 (2026-05-19); it survives as the dashboard service.

| Endpoint | Purpose |
|---|---|
| `GET /ui` | Admin dashboard web UI |
| `POST /admin/*` | Admin mutations (toggle, kid-mode, smart-mode, play-asset, etc.) |
| `GET /health` | Liveness probe; returns `{"ok": true}` |

`POST /api/voice/escalate` is also defined on bridge.py but is non-functional in the current stack — the ZeroClaw voice dispatch layer it depended on was retired in #36, and the only consumer (the Tier1Slim provider) was removed in the 2026-05-29 alignment pass. See [docs/cutover-behaviour.md](./cutover-behaviour.md) for the historical runbook.

## See also

- [hardware.md](./hardware.md) — what emits the device-side frames.
- [voice-pipeline.md](./voice-pipeline.md) — what xiaozhi-server does between frames.
- [brain.md](./brain.md) — the dotty-pi agent and its tool set.
- [architecture.md](./architecture.md#perception-event-bus) — the perception bus consumers.
- [references.md](./references.md#protocols) — all protocol spec links.

Last verified: 2026-05-22.
