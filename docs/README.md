---
title: Docs Index
description: Index of the StackChan tech reference documentation.
---

# Docs — StackChan tech reference

Curated reference for the StackChan voice robot stack. The top-level `README.md`
covers *how to deploy it*; these docs cover *what it is underneath* — hardware,
protocols, upstream model facts, and capabilities we aren't yet using.

Every file here cites upstream sources so a future agent (or human) can
re-verify claims against the canonical specs rather than trusting our paraphrase.

## Start here if you want…

| If you're trying to… | Read |
|---|---|
| Understand the overall shape | [architecture.md](./architecture.md) |
| Know what the physical robot can do | [hardware.md](./hardware.md) |
| Understand the voice pipeline (ASR/TTS/VAD) | [voice-pipeline.md](./voice-pipeline.md) |
| Understand the brain (ZeroClaw + LLM) | [brain.md](./brain.md) |
| Know what's on the wire between components | [protocols.md](./protocols.md) |
| See every cross-layer signal at a glance | [interaction-map.md](./interaction-map.md) |
| Find features we aren't using yet | [latent-capabilities.md](./latent-capabilities.md) |
| Pick an LLM backend | [llm-backends.md](./llm-backends.md) |
| Jump to an upstream repo or spec | [references.md](./references.md) |

## File map

```
docs/
├── README.md                ← you are here (index)
├── architecture.md          ← high-level data flow, actor responsibilities
├── hardware.md              ← M5Stack StackChan body + firmware lineage + MCP tool catalog
├── voice-pipeline.md        ← xiaozhi-esp32-server, FunASR, VAD, EdgeTTS, Piper
├── brain.md                 ← ZeroClaw architecture, Qwen3-30B-A3B, OpenRouter
├── protocols.md             ← Xiaozhi WebSocket, MCP-over-WS, ACP JSON-RPC, emotion
├── interaction-map.md       ← every cross-layer signal: source, dest, protocol, notes
├── latent-capabilities.md   ← upstream features we could wire up (cross-refs tasks.md)
├── llm-backends.md          ← side-by-side comparison of LLM backend options
└── references.md            ← canonical URLs, licenses, model cards, spec docs
```

## Conventions these docs follow

- **TL;DR at the top of each file** — 3-6 bullets, scannable in the first 40 lines.
- **Tables over prose** for dense facts — specs, tunables, method signatures.
- **Grep-bait headers** — e.g. `## MCP tool handshake`, `## session/prompt` — so you can navigate by header search.
- **Relative links only** — `[voice-pipeline.md](./voice-pipeline.md)`; never absolute paths.
- **Freshness footer** — every non-index file ends with `Last verified: YYYY-MM-DD`.
- **Placeholders for per-deployment values** — `<UNRAID_IP>`, `<RPI_IP>`, etc. (mapping lives with the deployer, not in this repo).
- **Soft claims where unverified** — if a fact came from a secondary source or we couldn't verify, the text says so rather than pretending to cite upstream.

## Relationship to the rest of the repo

- `../README.md` — deployment & ops (commands, layout, troubleshooting).
- `../CLAUDE.md` — agent orientation for this repo specifically.
- `../bridge.py`, `../zeroclaw.py`, `../edge_stream.py`, `../fun_local.py`, `../piper_local.py` — canonical source for the custom provider patches.
- These `docs/` — the *why* and the *what else is possible* behind the above.

## When docs here are stale

Each sub-file has a `Last verified:` date. Freshness decays roughly as follows:

| Topic | Half-life | Why |
|---|---|---|
| Hardware spec | Years | M5Stack CoreS3 revisions are slow |
| Protocol spec | Months | xiaozhi is actively evolving |
| Model facts (Qwen3) | Weeks-months | OpenRouter pricing and model revisions churn |
| Latent capabilities | Months | Upstream adds features regularly |

If you're reading this a year from now, treat the protocol + model claims as *starting points for re-verification*, not ground truth.

Last verified: 2026-04-24.
