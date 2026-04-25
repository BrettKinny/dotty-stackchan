---
title: Latent Capabilities
description: Features the hardware and software already support but are not yet wired up.
---

# Latent capabilities — features available but not wired up

## TL;DR

- Every row below is something the **hardware, voice pipeline, or brain already supports** but that the current deployment **doesn't use**. It's raw material for the backlog.
- Organised by where the capability lives: hardware, voice pipeline, brain.
- Each row ends with a cross-reference: a related `ROADMAP.md` item, or flagged as a **new-task candidate** if no backlog entry exists yet.
- Treat this as a menu, not a plan — some are cheap wins, others are complex.

<a id="hardware-unused"></a>
## Hardware — unused

Underlying peripherals on the M5Stack CoreS3 / StackChan kit that the firmware doesn't currently expose through MCP.

| Capability | What it unlocks | Priority | Cross-ref |
|---|---|---|---|
| **9-axis IMU shake/gesture** (BMI270 + BMM150) | Tap-to-activate, shake-to-dismiss, head-tilt-aware responses | Medium | **New-task candidate** |
| **Proximity sensor** (LTR-553ALS) | Wake-on-approach; auto-dim face at idle | Low | **New-task candidate** |
| **Ambient-light** (same sensor) | Match face brightness to room lighting | Low | **New-task candidate** |
| **NFC module** | Tap an NFC-tagged toy/card to trigger a scripted interaction | Medium | **New-task candidate** |
| **IR tx/rx** | Universal remote mode (learn + replay legacy appliance codes) | Low | **New-task candidate** |
| **microSD slot** | Offline sound packs, local fallback voices, recorded memories | Medium | Partially overlaps `ROADMAP.md` → "Create backup script" |
| **3-zone touch panel** | Multi-zone gesture controls (head-pat as a discrete event) | Low | **New-task candidate** |
| **Camera beyond `take_photo`** | On-device VLM preprocessing; streaming to a local vision server | Medium | Cross-refs `ROADMAP.md` → "Lock down for child-safe operation" (camera exposure) |
| **Hardware-enforced privacy LEDs** | LED state wired to the peripheral-enable signal, not a software hint | **High / safety** | `ROADMAP.md` → "Hardwire privacy-indicator LEDs in firmware" |
| **Servo velocity/acceleration caps** | Calmer, safer, less-startling head motion | **High / safety** | `ROADMAP.md` → "Tame violent servo motion" |

<a id="voice-pipeline-unused"></a>
## Voice pipeline — unused

Features xiaozhi-esp32-server supports upstream that aren't turned on or surfaced.

| Capability | What it unlocks | Priority | Cross-ref |
|---|---|---|---|
| **SenseVoice Speech Emotion Recognition (SER)** | Use the *user's* vocal emotion as LLM context (not just the LLM's own emoji output) | High | **New-task candidate** |
| **SenseVoice Audio Event Detection (AED)** | Detect bgm, applause, laughter, crying, coughing, sneezing — useful context for a kids' robot | Medium | **New-task candidate** |
| **SenseVoice language-ID output** | Detect when the user actually spoke a non-English language; respond in kind or request clarification | Low | Cross-refs the English-pin patch `fun_local.py` |
| **Sherpa-ONNX ASR** | Alternative to FunASR; fully offline, supports different languages | Low | **New-task candidate** |
| **Custom wake word** | Replace/add to the stock wake word via ESP-SR MultiNet | Low | **New-task candidate** |
| **Voiceprint speaker ID** | Distinguish family members; apply per-user persona/context | Medium | Cross-refs child-safety task (different guardrails for kids vs adults) |
| **VLLM (vision LM)** | "What's in this photo?" — camera + VLM pipeline | Medium | Gated on privacy-LED task |
| **PowerMem** | Dual-layer short-term + summarized memory (currently ZeroClaw owns memory) | Low | Would overlap with ZeroClaw's memory — probably don't |
| **Intent router** (`function_call` mode) | Route simple commands (turn off lights, set timer) without round-tripping to the LLM | Medium | **New-task candidate** |
| **RagFlow knowledge base** | Retrieval-augmented responses against a household doc store | Low | **New-task candidate** |
| **Multi-device routing** | Run the StackChan as one of several voice surfaces on the same ZeroClaw brain | Low | Needs the full-module deployment (DB-backed) |
| **Piper streaming synthesis** | Lower first-audio latency than the current batch synthesis | Medium | `ROADMAP.md` → "Reduce first-audio latency" |
| **ffmpeg post-processing on TTS** | Robot-voice character via ring modulator / bitcrush / vocoder | Medium | `ROADMAP.md` → "TTS provider swap — robot-sounding voice" |

<a id="brain-unused"></a>
## Brain — unused

ZeroClaw + Qwen3 + OpenRouter features that could be wired into the bridge.

| Capability | What it unlocks | Priority | Cross-ref |
|---|---|---|---|
| **ACP `session/update` streaming** | First-token TTS instead of waiting for the full response (perceived-latency win) | **High** | `ROADMAP.md` → "Reduce first-audio latency" |
| **Long-lived ZeroClaw sessions** | Skip `session/new` per turn — carry context across turns within a conversation | Medium | `ROADMAP.md` → "Reduce first-audio latency" (ACP session overhead) |
| **`session/request_permission`** | Bridge confirms tool calls before they execute — useful for child-safety. Bridge now auto-approves (2026-04-25); tool allowlist for child-safety is a follow-up. | Medium | `ROADMAP.md` → "Lock down for child-safe operation" |
| ~~**Qwen3 function-calling / tool-use**~~ | **Wired up (2026-04-25).** ZeroClaw auto-approves tools in `auto_approve` list and sends tool execution as `session/event` notifications. Bridge logs tool calls at INFO level. Works for `weather`, `web_search_tool`, `calculator`, etc. | ~~Medium~~ Done | — |
| **ZeroClaw MCP-server mode** | Expose ZeroClaw's tools/memory to other MCP clients | Low | **New-task candidate** |
| **Qwen3 `role: "system"` injection** | Move the English+emoji constraints into a proper system message instead of a prompt prefix/suffix; better MoE adherence | Medium | Rework of bridge's wrapping logic |
| **Qwen3 extended context (256K native)** | Keep long conversation history / memory verbatim instead of summarising | Low | Costs more tokens per turn — probably not worth it yet |
| **OpenRouter latency/cost dashboard** | Observability beyond the local `state/costs.jsonl` | Low | Already available — just point a browser at it |
| **OpenRouter failover / multi-model** | A/B a smaller faster model for voice turns specifically | Medium | `ROADMAP.md` → "Reduce first-audio latency" (smaller model for voice) |
| **ZeroClaw cost/trace surfacing** | Expose `state/costs.jsonl` + `runtime-trace.jsonl` via the bridge `/health` or a new `/stats` endpoint | Low | **New-task candidate** |
| **ZeroClaw cron scheduler** | The robot could say "good morning" on a schedule, not just on demand | Low | **New-task candidate** |

<a id="observability"></a>
## Cross-cutting — observability

None of these are feature requests — they're gaps in what we can *see* about the running system.

| Gap | What it'd unlock |
|---|---|
| Capture a real `tools/list` response | Ground-truth for the MCP tool table in [hardware.md](./hardware.md) |
| Per-turn latency breakdown | Which of ASR / LLM / TTS / network is the dominant cost |
| Per-turn cost breakdown | Whether Qwen3 via OpenRouter is cheaper than a smaller local model |
| Per-session trace diff | Whether English-sandwich is still needed after a hypothetical model upgrade |

These are all feeders for the **`ROADMAP.md`** "Map the ZeroClaw ↔ xiaozhi-server ↔ StackChan firmware interaction" backlog item.

## Prioritisation rule of thumb

| Signal | Do it sooner |
|---|---|
| Child-safety or privacy | Always |
| Reduces perceived latency | Usually |
| Uses hardware we already paid for | Often |
| Requires an external service | Often skip |
| Needs the full-module DB deployment | Bundle for a future migration |

## See also

- [../ROADMAP.md](../ROADMAP.md) — live backlog; this file is a *source* for it, not a replacement.
- [hardware.md](./hardware.md) — what the hardware features actually are.
- [voice-pipeline.md](./voice-pipeline.md) — what the server supports upstream.
- [brain.md](./brain.md) — what ZeroClaw/Qwen/OpenRouter expose.
- [references.md](./references.md) — upstream source for every capability claim.

Last verified: 2026-04-24.
