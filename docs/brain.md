---
title: Brain
description: ZeroClaw agent runtime, the model matrix (Tier1Slim inner loop + escalation targets, legacy ZeroClawLLM), and the FastAPI bridge.
---

# Brain — ZeroClaw + the model matrix + the bridge

## TL;DR

- The "brain" is two cooperating pieces: **[ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)** (a Rust AI-agent runtime, MIT/Apache-2.0 dual-licensed) on the ZeroClaw host, plus a **FastAPI bridge** (`bridge.py`) that fronts it over HTTP.
- The bridge accepts POSTs from the voice path and translates them into **ACP (Agent Client Protocol) JSON-RPC 2.0 over stdio** against a long-running `zeroclaw acp` child.
- **Which LLM runs which turn depends on the active voice provider.** Two paths coexist:
  - **Tier1Slim path (default in `.config.yaml`)** — small/fast model (`qwen3.5:4b` on a local llama-swap by default) handles every conversational turn; only tool calls escalate to the bridge. Smart-mode flips the inner-loop model to a cloud model in-process.
  - **ZeroClawLLM path (legacy)** — every turn runs through ZeroClaw with [Qwen3-30B-A3B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507) (a 30.5 B-param MoE, 3.3 B active per token) via OpenRouter.
- The bridge picks the smart-mode dispatch path based on the `DOTTY_VOICE_PROVIDER` env var: `"tier1slim"` → hot-swap via `/xiaozhi/admin/set-tier1slim-model`; `"zeroclaw"` → rewrite ZeroClaw's `config.toml` and restart the daemon.
- Persona lives in two different places depending on path: Tier1Slim reads `personas/dotty_voice.md`; ZeroClawLLM reads `~/.zeroclaw/workspace/{SOUL,IDENTITY,MEMORY,AGENTS}.md`. See [change-persona.md](./cookbook/change-persona.md).
- Known weak spot of Qwen3: it leaks Chinese on long-context English-only prompts. Both paths compensate — the bridge wraps `channel="stackchan"` turns in an English+emoji sandwich; Tier1Slim appends an English-only suffix per turn.

## Model matrix

| Path | Model | Where | When called |
|---|---|---|---|
| Tier1Slim inner loop (smart_mode OFF) | `qwen3.5:4b` | local llama-swap (`http://192.168.1.67:8080/v1` by default) | Every plain conversational turn. ~500 ms warm. |
| Tier1Slim inner loop (smart_mode ON) | `anthropic/claude-sonnet-4-6` (`SMART_MODEL`) | OpenRouter | Every conversational turn while smart_mode is on. |
| Tier1Slim escalation: `think_hard` | `qwen3.6:27b-think` | local llama-swap | Multi-step reasoning, 3+ digit arithmetic, anything the small model would have to guess at. |
| Tier1Slim escalation: `memory_lookup` | (no LLM call — FTS) | ZeroClaw memory | `"do you remember…"` queries. |
| Tier1Slim escalation: `take_photo` | `google/gemini-2.0-flash-001` (`VLM_MODEL`) | OpenRouter (or local Ollama if `VLM_API_URL` is repointed) | Camera describe. |
| Tier1Slim escalation: `play_song` | (no LLM call) | Firmware via xiaozhi `/xiaozhi/admin/play-asset` | Song request. |
| ZeroClawLLM (legacy single tier) | `Qwen3-30B-A3B-Instruct-2507` | OpenRouter | Every turn when `selected_module.LLM = ZeroClawLLM`. |
| Vision narrative LLM (security/scene synthesis) | `VISION_MODEL` (`google/gemini-2.0-flash-001` by default) | OpenRouter | Bridge-internal — describes the camera frame for journaling and security mode. |
| Audio captioning (security mode) | `AUDIO_CAPTION_MODEL` (`google/gemini-2.5-flash` by default) | OpenRouter | Bridge-internal — `what does Dotty hear` describer. |

The full Tier1Slim wire format, escalation payload, and `set_runtime()` hot-swap are documented in [tier1slim.md](./tier1slim.md).

## ZeroClaw architecture

From [github.com/zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw) (see [references.md](./references.md#brain)):

| Component | Role |
|---|---|
| **Gateway** | Control plane: HTTP / WS / SSE, sessions, config, cron, webhooks, web dashboard (localhost:42617 by default) |
| **Runtime** | Agent execution. Two modes: **Native** (direct process, default, fastest) or **Docker** (sandboxed) |
| **Channels** | Pluggable inputs. Supports WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Matrix, IRC, Email, Bluesky, Nostr, Mattermost, Nextcloud Talk, DingTalk, Lark, QQ, Reddit, LinkedIn, Twitter, MQTT, WeChat Work, and others. This bridge uses `channel="stackchan"` as an arbitrary string identifier — ZeroClaw treats it as just another channel. |
| **Providers** | LLM backends. OpenAI, Anthropic (API or OAuth), Gemini, and 17+ OpenAI-compatible endpoints (including OpenRouter, Ollama, GLM). Failover + multi-account auth profiles supported. |
| **Memory** | Pluggable backends. SQLite is the default; PostgreSQL and Markdown are options. Hybrid keyword+vector search per upstream wiki. |
| **Tools / MCP** | 70+ built-in tools plus bidirectional MCP — ZeroClaw is both an MCP client (consumes external servers) and an MCP server (can expose its internals to other agents). |

Resource claims from upstream: ~8.8 MB static binary, <5 MB runtime RAM on release builds. The Pi can comfortably run it.

### Workspace files — the persona surface

Under `/root/.zeroclaw/workspace/`:

| File | Upstream description | What we use it for |
|---|---|---|
| `SOUL.md` | "Core identity and operating principles" | The configured persona's voice, values, role in the household. The dedicated `channel="stackchan"` section was removed after bridge-level English-only enforcement subsumed it. |
| `IDENTITY.md` | Agent personality and role definition | Name, backstory, household/family context specific to your deployment. |
| `USER.md` | User context and preferences | Per-user context; optional. Not heavily populated in our deploy. |
| `MEMORY.md` | "Long-term facts and lessons learned" | Git-visible core memories. Complemented by an SQLite `brain.db`. |
| `AGENTS.md` | "Session conventions and initialization rules" | Cross-agent behavioral guardrails. Self-modifying per upstream — the agent writes to it. |

**Important**: the bridge wraps voice turns in a hard English+emoji sandwich **outside** of ZeroClaw's persona files. The enforcement lives in `bridge.py`, not in `SOUL.md`, because a persona-level constraint wasn't strong enough to keep Qwen3 from leaking Chinese mid-session. See [Qwen3 caveat](#qwen3-caveat-chinese-leak-and-long-context-drift).

### The confusing "ACP" terminology in ZeroClaw

ZeroClaw uses the acronym **ACP** in two different contexts. Don't conflate them:

1. **Autonomy Control (ACP Mode)** — ZeroClaw's README describes autonomy *levels* (`ReadOnly`, `Supervised`, `Full`) under a heading that calls them "ACP Mode". This is about how much the agent is allowed to do without asking.
2. **Agent Client Protocol** — the `zeroclaw acp` CLI subcommand launches ZeroClaw as an ACP server speaking JSON-RPC 2.0 over stdio. This is the Zed-originated [Agent Client Protocol](https://agentclientprotocol.com) — unrelated to autonomy levels, despite sharing an acronym.

Our bridge uses the **second** one. The robot's autonomy mode is whatever ZeroClaw's `config.toml` sets (likely `Supervised`), and is separate from the stdio protocol.

See [protocols.md](./protocols.md#acp) for the ACP wire format.

## The bridge — `bridge.py`

Lives at `<BRIDGE_PATH>/bridge.py`, runs under systemd (`zeroclaw-bridge.service`).

**HTTP surface:**

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /api/message` | `ZeroClawLLM` (legacy single-tier) | Accept a message + channel, return a response string. |
| `POST /api/voice/escalate` | `Tier1Slim` | Dispatch a tool call (`memory_lookup` / `think_hard` / `take_photo` / `play_song`) and return the result. |
| `POST /api/voice/remember` | `Tier1Slim` | Fire-and-forget: persist a `[REMEMBER: ...]` fact extracted from the model's reply. |
| `POST /api/voice/memory_log` | `Tier1Slim` | Fire-and-forget: log the completed turn so ZeroClaw indexes it for future recall. |
| `POST /api/perception/event` | xiaozhi-server perception relay | Receive `face_detected` / `face_lost` / `sound_event` / `state_changed` events from the firmware via xiaozhi. |
| `GET /health` | health checks | Liveness probe. |
| `POST /admin/*` (localhost-only) | operator scripts | Runtime mutations (kid-mode, persona, model, safety). |

See [protocols.md](./protocols.md) for exact wire formats.

**Per-turn responsibilities on the `ZeroClawLLM` path:**

1. Spawn (or reuse) a `zeroclaw acp` child, holding stdin/stdout open.
2. Send JSON-RPC `session/new` to get a fresh session_id. *(Note: currently one `session/new` per turn — see session reuse in [latent-capabilities.md](./latent-capabilities.md#brain-unused).)*
3. Wrap the user content in the English+emoji sandwich when `channel == "stackchan"`.
4. Send JSON-RPC `session/prompt` with the wrapped content.
5. Wait for the terminal result (not streamed — see [latent-capabilities.md](./latent-capabilities.md#brain-unused)).
6. Run `_ensure_emoji_prefix()` — if the first non-whitespace char isn't in the 9-emoji allowlist (😊😆😢😮🤔😠😐😍😴), prepend 😐.
7. Return JSON `{"response": "<cleaned text>"}`.

**Per-turn responsibilities on the `Tier1Slim` path:**

The bridge doesn't see plain conversational turns at all — they happen entirely inside Tier1Slim against llama-swap. The bridge is invoked only when the small model emits a `tool_call`, at which point it:

1. Looks up the tool name and dispatches to ZeroClaw (`memory_lookup`), llama-swap's `qwen3.6:27b-think` (`think_hard`), the VLM (`take_photo`), or xiaozhi's `/xiaozhi/admin/play-asset` (`play_song`).
2. Returns the result string truncated to 1000 chars.
3. Separately, accepts `/api/voice/remember` and `/api/voice/memory_log` posts so future `memory_lookup` calls find the new content.

## The LLMs

### Qwen3-30B-A3B-Instruct-2507 (legacy `ZeroClawLLM` path)

From the [HuggingFace card](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507):

| Fact | Value |
|---|---|
| Total parameters | 30.5 B |
| Activated per token | 3.3 B |
| Non-embedding params | 29.9 B |
| Layers | 48 |
| Attention heads | 32 Q / 4 KV (GQA) |
| Experts | 128 total, 8 active per token |
| Native context | 262 144 tokens (256 K) |
| Extended context | up to 1 M tokens with Dual Chunk Attention + MInference sparse attention |
| Mode | Non-thinking only (no `<think>` blocks) |
| Recommended sampling | T=0.7, top_p=0.8, top_k=20, min_p=0, presence_penalty 0–2 |
| Tool calling | Supported (OpenAI-compatible; Qwen-Agent framework recommended for full features) |
| Suggested output length | 16 384 tokens |

The "2507" suffix indicates the 2025 revision (the HF card calls it the May 2025 update; the YYMM reading would be July 2025 — these conflict in upstream copy, treat the revision semantically rather than calendar-pinning it).

### Qwen3 caveat — Chinese leak and long-context drift

Qwen3 is multilingual by training and occasionally **leaks Chinese mid-response** when:
- Context is long (persona + memory + history = lots of tokens before the user turn).
- System-prompt adherence is weakened by MoE expert routing.

Observed symptom in our deploy: the model would start a response in English and drop a Chinese character or phrase partway through. `en-*` EdgeTTS voices return silent audio on non-English input, so the whole response sounded like a dead mic — it was a language bug, not a TTS bug.

**Our mitigation, layered:**

1. ZeroClaw's own system prompt has English hard rules.
2. xiaozhi-server's top-level `prompt:` is also English-only.
3. The bridge adds **both a prefix and a suffix** around every voice turn — the suffix sits at the end-of-prompt position (max attention) and reiterates the constraint. 8/8 adversarial prompts (broken English, embedded Japanese, short utterances) passed cleanly after this change.

### qwen3.5:4b (Tier1Slim inner loop)

Local on llama-swap (Unraid, dual RTX 3060). Fast: ~500 ms warm round-trip including TTS dispatch. Trained for tool calling, which is what lets the four-tool catalogue work reliably at 4 B. See [tier1slim.md](./tier1slim.md) for the wire format.

### qwen3.6:27b-think (Tier1Slim `think_hard` target)

Local on the same llama-swap, separate alias. ~18 tok/s generation, ~30 s cold-load. Co-resident with `qwen3.5:4b` under the `voice` matrix set in `llama-swap/config.yaml` so an escalation doesn't evict the inner loop. See [cookbook/llama-swap-concurrent-models.md](./cookbook/llama-swap-concurrent-models.md).

### Cloud models (smart_mode + visual + audio)

- **Smart-mode inner loop:** `anthropic/claude-sonnet-4-6` (`SMART_MODEL` env var). Used by Tier1Slim when smart_mode is on; flipped in-process via `set_runtime()`.
- **VLM (`take_photo`, security camera frames):** `google/gemini-2.0-flash-001` (`VLM_MODEL`).
- **Audio captioning (security mode):** `google/gemini-2.5-flash` (`AUDIO_CAPTION_MODEL`).

## OpenRouter

Routing: OpenRouter fronts cloud models (`SMART_MODEL`, `VLM_MODEL`, `AUDIO_CAPTION_MODEL`, and the legacy ZeroClaw path's Qwen3-30B). It handles multiple upstream providers and exposes an OpenAI-compatible API. ZeroClaw's config references it via an `openrouter` provider section with an encrypted API key; the bridge reads `OPENROUTER_API_KEY` from the systemd unit's environment for the VLM and audio-caption calls.

Observability OpenRouter itself offers (not currently surfaced in this stack):
- Per-request latency + cost dashboards.
- Multi-model A/B routing.
- Per-provider failover for the same model.

If per-turn latency ever needs deeper analysis than ZeroClaw's `state/costs.jsonl` and `state/runtime-trace.jsonl`, OpenRouter's dashboard is where to look next.

## See also

- [tier1slim.md](./tier1slim.md) — the default voice path.
- [protocols.md](./protocols.md#acp) — exact ACP RPC surface the bridge speaks.
- [voice-pipeline.md](./voice-pipeline.md) — what drives the bridge.
- [llm-backends.md](./llm-backends.md) — choosing between Tier1Slim, ZeroClawLLM, OpenAICompat.
- [latent-capabilities.md](./latent-capabilities.md#brain-unused) — streaming, session reuse, tool-use, MCP-server mode.
- [references.md](./references.md#brain) — ZeroClaw, ACP, Qwen3, OpenRouter links.

Last verified: 2026-05-17.
