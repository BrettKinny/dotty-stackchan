---
title: References
description: Canonical upstream links for every component in the stack.
---

# References

Canonical upstream links for every component mentioned in these docs. When a
claim in another file cites upstream, the link is here. Treat this as the
source-of-truth for re-verification.

<a id="hardware"></a>
## Hardware

| Resource | URL | What's there |
|---|---|---|
| M5Stack StackChan README | https://github.com/m5stack/StackChan | Our firmware codebase. Body spec (servos, LEDs, NFC, IR, touch, battery). |
| M5Stack CoreS3 datasheet | https://docs.m5stack.com/en/core/CoreS3 | SoC, memory, display, camera, IMU, PMU, audio codec part numbers. |
| meganetaaan/stack-chan (original) | https://github.com/meganetaaan/stack-chan | TypeScript/Moddable JS origin project — **we don't run this**, but it's the lineage. |
| 78/xiaozhi-esp32 firmware (generic) | https://github.com/78/xiaozhi-esp32 | Multi-board voice-assistant firmware. Authority for the WS + MCP protocols below. |

<a id="voice"></a>
## Voice pipeline

| Resource | URL | What's there |
|---|---|---|
| xiaozhi-esp32-server | https://github.com/xinnan-tech/xiaozhi-esp32-server | Our Docker server. README lists all provider options. |
| xiaozhi-esp32-server (English README) | https://github.com/xinnan-tech/xiaozhi-esp32-server/blob/main/README_en.md | English feature catalog. |
| xiaozhi.dev docs portal | https://xiaozhi.dev/en/docs/ | Protocol + emotion docs. |
| FunAudioLLM / SenseVoiceSmall | https://huggingface.co/FunAudioLLM/SenseVoiceSmall | ASR model card. Languages, SER, AED, latency figures. |
| FunASR project | https://github.com/modelscope/FunASR | Toolkit that hosts SenseVoice. |
| Silero VAD | https://github.com/snakers4/silero-vad | VAD tunables, version history, limitations. |
| Piper TTS (engine) | https://github.com/rhasspy/piper | Local neural TTS. |
| rhasspy/piper-voices | https://huggingface.co/rhasspy/piper-voices | Voice catalog (includes `en_GB-cori-medium`). Repo license MIT; individual voices carry their own. |
| rany2/edge-tts | https://github.com/rany2/edge-tts | Unofficial EdgeTTS library — the technique we use, not the official path. |

<a id="brain"></a>
## Brain

| Resource | URL | What's there |
|---|---|---|
| ZeroClaw | https://github.com/zeroclaw-labs/zeroclaw | Agent runtime. Architecture, workspace files, providers, MCP support. Rust / dual MIT+Apache-2.0. |
| Qwen3-30B-A3B-Instruct-2507 | https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507 | Model card. Param counts, experts, context length, sampling recommendations. |
| OpenRouter (Qwen3 page) | https://openrouter.ai/qwen/qwen3-30b-a3b-instruct-2507 | Pricing, latency, provider availability. |
| OpenRouter docs | https://openrouter.ai/docs | Tool calling, streaming, failover. |
| Qwen-Agent | https://github.com/QwenLM/Qwen-Agent | Tool-use framework recommended by the Qwen3 card. |

<a id="protocols"></a>
## Protocols

| Resource | URL | What's there |
|---|---|---|
| Xiaozhi WebSocket protocol | https://github.com/78/xiaozhi-esp32/blob/main/docs/websocket.md | Full message catalog, hello shape, binary audio framing. |
| Xiaozhi MCP protocol | https://github.com/78/xiaozhi-esp32/blob/main/docs/mcp-protocol.md | `tools/list`, `tools/call`, `AddTool` vs `AddUserOnlyTool`. |
| Xiaozhi emotion docs | https://xiaozhi.dev/en/docs/development/emotion/ | 21-emotion catalog + wire format. |
| Agent Client Protocol | https://agentclientprotocol.com | ACP spec (Zed-originated). JSON-RPC 2.0 over stdio/HTTP/WS. |
| ACP prompt-turn spec | https://agentclientprotocol.com/protocol/prompt-turn | `session/prompt`, `session/update`, `session/cancel`, `session/request_permission`. |
| MCP (Model Context Protocol) | https://modelcontextprotocol.io | The base spec ACP reuses JSON shapes from. |

<a id="pricing"></a>
## Pricing (volatile)

OpenRouter pricing changes often — values below are most recently observed per [references.md#brain](#brain) but should be re-fetched before quoting.

| Model | $/M input | $/M output | Source |
|---|---|---|---|
| Qwen3-30B-A3B-Instruct-2507 | ~$0.08 | ~$0.28 | OpenRouter (check live) |

## Licensing summary

| Component | License | Notes |
|---|---|---|
| ZeroClaw | MIT / Apache-2.0 (dual) | Binary is yours to run. |
| FunASR / SenseVoiceSmall | Check the HF card | Weights license varies. |
| SileroVAD | MIT | — |
| Piper engine | MIT | — |
| Piper voices | MIT repo, per-voice varies | `en_GB-cori-medium` license — verify before redistributing your robot's outputs. |
| EdgeTTS | N/A — reverse-engineered | Microsoft can cut it off at any time. No support. |
| xiaozhi-esp32 firmware | See repo | Modify and redistribute per upstream. |
| xiaozhi-esp32-server | See repo | Same. |
| m5stack/StackChan | See repo | — |
| meganetaaan/stack-chan | MIT (check repo) | Original project. |
| Qwen3 model weights | See HF card | Commercial-use terms vary by Qwen release. |
| This repo | MIT | See `../LICENSE`. |

Always verify licensing on the upstream page before redistribution — this table paraphrases and may drift.

## See also

- [README.md](./README.md) — docs index.

Last verified: 2026-05-17.
