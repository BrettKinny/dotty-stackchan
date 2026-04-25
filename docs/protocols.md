---
title: Protocols
description: Xiaozhi WebSocket protocol, ACP JSON-RPC, and the emotion frame format.
---

# Protocols — what's on the wire

## TL;DR

- **Xiaozhi WebSocket protocol** — between device and xiaozhi-server. Opus audio + JSON control frames. Supports MCP over JSON-RPC 2.0 in-band. Canonical spec: `github.com/78/xiaozhi-esp32/blob/main/docs/websocket.md`.
- **Emotion channel** — 21 upstream emotion identifiers; the server picks one from the LLM's leading emoji and emits a separate `llm`-type frame. This stack uses a 9-emoji subset.
- **MCP over WS** — the device acts as an MCP server; xiaozhi-server calls `tools/list` and `tools/call` against it. Tool names use dotted namespaces like `self.audio_speaker.set_volume`.
- **Agent Client Protocol (ACP)** — JSON-RPC 2.0 over stdio between the FastAPI bridge and `zeroclaw acp`. Zed-originated spec, maintained at `agentclientprotocol.com`.

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

`bridge.py` enforces a 9-emoji subset:

```
😊 😆 😢 😮 🤔 😠 😐 😍 😴
```

If the LLM returns a leading emoji outside the allowlist (or no emoji at all), the bridge prepends 😐. Rationale: smaller set = more predictable face animations, fewer corner-cases in the xiaozhi emoji-stripper.

### Three-layer enforcement

1. **ZeroClaw persona prompt** — asks for leading emoji.
2. **xiaozhi-server top-level `prompt:`** — also asks for leading emoji.
3. **Bridge `_ensure_emoji_prefix`** — last line of defence; prepends 😐 if absent.

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

<a id="acp"></a>
## ACP — Agent Client Protocol

Canonical spec: [agentclientprotocol.com](https://agentclientprotocol.com). Zed-Industries-originated, JSON-RPC 2.0, designed for editor↔agent interop; reusable for any agent-over-stdio situation.

**Our transport:** `zeroclaw acp` is spawned with `stdin`/`stdout` inherited. The FastAPI bridge reads/writes JSON-RPC 2.0 framed messages (one JSON object per line or Content-Length-prefixed, per ACP spec).

### Core methods

| Method | Direction | Params | Returns / effect |
|---|---|---|---|
| `initialize` | client → agent | Protocol version, client capabilities | Agent capabilities, supported tool-sets |
| `session/new` | client → agent | `working_directory` | `sessionId` and metadata |
| `session/prompt` | client → agent | `sessionId`, `prompt: ContentBlock[]` (text/images/resources) | `stopReason: "end_turn" \| "max_tokens" \| "max_turn_requests" \| "refusal" \| "cancelled"` |
| `session/update` | agent → client (notification) | `sessionId`, `update.sessionUpdate: "plan" \| "agent_message_chunk" \| "tool_call" \| "tool_call_update"` with content | Agent streams progress |
| `session/request_permission` | agent → client | `sessionId`, tool call details | Client approves/denies tool execution |
| `session/cancel` | client → agent | `sessionId` | Agent halts; pending `session/prompt` resolves with `cancelled` |

### What our bridge uses today

- `initialize` (once at child startup)
- `session/new` (with session caching — reuses across turns, rotates on idle/turn-count/age)
- `session/prompt` (streaming via `session/event` chunks; bridge also supports buffered mode)
- `session/event` — tool call/result logging (`tool_call`, `tool_result` types) and streaming text chunks
- `session/request_permission` — auto-approves tool calls (safety net for tools not in ZeroClaw's `auto_approve` list)

- `session/cancel` → sent on barge-in (device emits `abort`, xiaozhi closes the streaming HTTP connection, bridge cancels the in-flight ACP prompt and drains stale output)

### ACP vs MCP — how they differ

| | MCP | ACP |
|---|---|---|
| Purpose | Expose tools to a model | Drive a whole agent |
| Typical client | An LLM harness | A code editor (or here, our bridge) |
| Message shapes | `tools/list`, `tools/call`, `resources/*`, `prompts/*` | `session/prompt`, `session/update`, `session/cancel`, `session/request_permission` |
| Re-uses MCP | — | Yes — shares ContentBlock and resource JSON shapes |

Both are JSON-RPC 2.0. The device's MCP exchanges ride the Xiaozhi WS; the bridge's ACP exchanges ride local stdio.

## See also

- [hardware.md](./hardware.md) — what emits the device-side frames.
- [voice-pipeline.md](./voice-pipeline.md) — what xiaozhi-server does between frames.
- [brain.md](./brain.md) — what the bridge does with the ACP results.
- [references.md](./references.md#protocols) — all protocol spec links.

Last verified: 2026-04-24.
