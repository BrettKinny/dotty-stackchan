---
title: Cross-Layer Interaction Map
description: Signal flow between StackChan firmware, xiaozhi-server, and zeroclaw-bridge.
---

# Cross-Layer Interaction Map

One-page reference for every cross-layer signal in the Dotty stack.

**Layers:**

1. **StackChan firmware** -- ESP32-S3 (m5stack/StackChan). The physical robot.
2. **xiaozhi-esp32-server** -- Docker on a Linux host. Voice I/O pipeline (ASR, TTS, VAD, emotion parsing).
3. **zeroclaw-bridge** -- FastAPI on ZeroClaw host. LLM brain interface (HTTP to ZeroClaw ACP-over-stdio).

---

## Audio & Speech

| Signal | Source | Destination | Protocol | Notes |
|---|---|---|---|---|
| Audio frames | StackChan | xiaozhi | WebSocket, Opus 60 ms frames | 16 kHz mono; sent while `listen` state is `start` |
| TTS audio | xiaozhi | StackChan | WebSocket, Opus frames | 24 kHz; streamed sentence-by-sentence as TTS completes |
| VAD state | xiaozhi (local) | xiaozhi (internal) | SileroVAD in-process | Detects speech-end silence; triggers ASR on the buffered audio |
| ASR text | FunASR (in xiaozhi) | LLM provider (internal) | In-process call | SenseVoiceSmall; `language` config key patched in `fun_local.py` |
| STT frame | xiaozhi | StackChan | WebSocket JSON `{"type":"stt","text":"..."}` | Sent as soon as ASR finishes; firmware shows thinking face |

## LLM & Responses

| Signal | Source | Destination | Protocol | Notes |
|---|---|---|---|---|
| LLM request | xiaozhi (ZeroClawLLM provider) | bridge | HTTP POST `/api/message/stream` | Carries the user text; bridge wraps it in ACP JSON-RPC to ZeroClaw |
| LLM response | bridge | xiaozhi | NDJSON streaming (HTTP chunked) | Each chunk is one partial sentence; bridge enforces emoji prefix |
| Sentence chunks | xiaozhi | TTS then StackChan | Internal then WebSocket Opus | xiaozhi splits response into sentences, synthesizes each, streams audio back |

## Emotion & Expression

| Signal | Source | Destination | Protocol | Notes |
|---|---|---|---|---|
| Emoji in LLM text | bridge (LLM output) | xiaozhi | First char of NDJSON response text | Three-layer enforcement: ZeroClaw prompt, xiaozhi system prompt, `_ensure_emoji_prefix` fallback |
| Emotion frame | xiaozhi | StackChan | WebSocket JSON `{"type":"llm","text":"emoji","emotion":"name"}` | Mapped from leading emoji (e.g. `😊`=smile, `🤔`=thinking); 9-emoji subset used |
| Thinking emotion | bridge | xiaozhi (forwarded to StackChan) | Emitted before LLM call starts | Shows thinking face while waiting for first token |
| Face animation | StackChan firmware (local) | Avatar renderer (local) | Internal | Firmware maps emotion string to animated face expression |

## MCP Tools

| Signal | Source | Destination | Protocol | Notes |
|---|---|---|---|---|
| tools/list | StackChan | xiaozhi | JSON-RPC 2.0 over WebSocket | Sent during WS handshake; 11 tools registered (camera, LED, head, audio, etc.) |
| tools/call | xiaozhi | StackChan | JSON-RPC 2.0 over WebSocket | e.g. `self.camera.take_photo`, `self.robot.set_led_color`, `self.robot.set_head_angles` |
| tool result | StackChan | xiaozhi | JSON-RPC 2.0 over WebSocket | Result forwarded to LLM provider so the model can use the output |

## Session & Control

| Signal | Source | Destination | Protocol | Notes |
|---|---|---|---|---|
| hello | StackChan | xiaozhi | WebSocket JSON `{"type":"hello"}` | Includes `features:{mcp:true}`, audio params; must get reply within 10 s |
| hello response | xiaozhi | StackChan | WebSocket JSON `{"type":"hello"}` | Returns `session_id` and server audio params (24 kHz Opus) |
| listen | StackChan | xiaozhi | WebSocket JSON `{"type":"listen"}` | `state:"start"/"stop"`, `mode:"auto"/"manual"`; controls when audio is processed |
| abort | StackChan | xiaozhi | WebSocket JSON | Sent when user speaks during TTS playback; cancels current response |
| OTA check | StackChan | xiaozhi :8003 | HTTP GET `/xiaozhi/ota/` | Returns WebSocket URL and config on boot; firmware connects to the returned URL |

## Modes & LED

For the *behavioural* layer that consumes these signals -- what mode the robot is in, what LED plays, and how modes hand off to one another -- see [modes.md](./modes.md). That doc is the canonical taxonomy (ambient / conversation / performance / maintenance), the per-mode trigger reference, and the LED contract table. This file remains the wire-level signal reference; `modes.md` is the state-machine view on top of it.
