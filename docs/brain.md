# Brain — ZeroClaw + Qwen3 + OpenRouter

## TL;DR

- The brain is **[ZeroClaw](https://github.com/zeroclaw-labs/zeroclaw)** — a Rust AI-agent runtime (MIT/Apache-2.0 dual-licensed) that runs on the RPi, installed via `cargo install`.
- A **FastAPI bridge** (`bridge.py`) accepts HTTP POSTs from xiaozhi-server's LLM provider and translates them into **ACP (Agent Client Protocol) JSON-RPC 2.0 over stdio** against a long-running `zeroclaw acp` child process.
- The LLM is **[Qwen3-30B-A3B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507)** — a 30.5 B-param MoE with 3.3 B active per token — accessed via **OpenRouter**.
- Persona lives in Markdown files under `~/.zeroclaw/workspace/` (`SOUL.md`, `IDENTITY.md`, `MEMORY.md`, `AGENTS.md`, and optionally `USER.md`). These are hot-read — no rebuild to change Dotty's personality.
- Known weak spot: Qwen3 tends to leak Chinese on long-context English-only prompts. Bridge compensates with a per-turn English+emoji sandwich (prefix + suffix) wrapping any turn with `channel="stackchan"`.

## ZeroClaw architecture

From [github.com/zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw) (see [references.md](./references.md#brain)):

| Component | Role |
|---|---|
| **Gateway** | Control plane: HTTP / WS / SSE, sessions, config, cron, webhooks, web dashboard (localhost:42617 by default) |
| **Runtime** | Agent execution. Two modes: **Native** (direct process, default, fastest) or **Docker** (sandboxed) |
| **Channels** | Pluggable inputs. Supports WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Matrix, IRC, Email, Bluesky, Nostr, Mattermost, Nextcloud Talk, DingTalk, Lark, QQ, Reddit, LinkedIn, Twitter, MQTT, WeChat Work, and others. Dotty's bridge uses `channel="stackchan"` as an arbitrary string identifier — ZeroClaw treats it as just another channel. |
| **Providers** | LLM backends. OpenAI, Anthropic (API or OAuth), Gemini, and 17+ OpenAI-compatible endpoints (including OpenRouter, Ollama, GLM). Failover + multi-account auth profiles supported. |
| **Memory** | Pluggable backends. SQLite is the default; PostgreSQL and Markdown are options. Hybrid keyword+vector search per upstream wiki. |
| **Tools / MCP** | 70+ built-in tools plus bidirectional MCP — ZeroClaw is both an MCP client (consumes external servers) and an MCP server (can expose its internals to other agents). |

Resource claims from upstream: ~8.8 MB static binary, <5 MB runtime RAM on release builds. The Pi can comfortably run it.

### Workspace files — Dotty's persona surface

Under `/root/.zeroclaw/workspace/`:

| File | Upstream description | What we use it for |
|---|---|---|
| `SOUL.md` | "Core identity and operating principles" | Dotty's voice, values, role in the household. `channel="stackchan"` section was removed (2026-04-24) after the bridge-level enforcement subsumed it. |
| `IDENTITY.md` | Agent personality and role definition | Name, backstory, family context (owner is Brett / Square Wave Systems). |
| `USER.md` | User context and preferences | Per-user context; optional. Not heavily populated in our deploy. |
| `MEMORY.md` | "Long-term facts and lessons learned" | Git-visible core memories. Complemented by an SQLite `brain.db`. |
| `AGENTS.md` | "Session conventions and initialization rules" | Cross-agent behavioral guardrails. Self-modifying per upstream — the agent writes to it. |

**Important**: the bridge wraps voice turns in a hard English+emoji sandwich **outside** of ZeroClaw's persona files. The enforcement lives in `bridge.py`, not in `SOUL.md`, because a persona-level constraint wasn't strong enough to keep Qwen3 from leaking Chinese mid-session. See [Qwen3 caveat](#qwen3-caveat-chinese-leak-and-long-context-drift).

### The confusing "ACP" terminology in ZeroClaw

ZeroClaw uses the acronym **ACP** in two different contexts. Don't conflate them:

1. **Autonomy Control (ACP Mode)** — ZeroClaw's README describes autonomy *levels* (`ReadOnly`, `Supervised`, `Full`) under a heading that calls them "ACP Mode". This is about how much the agent is allowed to do without asking.
2. **Agent Client Protocol** — the `zeroclaw acp` CLI subcommand launches ZeroClaw as an ACP server speaking JSON-RPC 2.0 over stdio. This is the Zed-originated [Agent Client Protocol](https://agentclientprotocol.com) — unrelated to autonomy levels, despite sharing an acronym.

Our bridge uses the **second** one. Dotty's autonomy mode is whatever ZeroClaw's `config.toml` sets (likely `Supervised`), and is separate from the stdio protocol.

See [protocols.md](./protocols.md#acp) for the ACP wire format.

## The bridge — `bridge.py`

Lives at `<RPI_BRIDGE_PATH>/bridge.py`, runs under systemd (`zeroclaw-bridge.service`).

**Responsibilities:**

| HTTP endpoint | Purpose |
|---|---|
| `POST /api/message` | Accept a message + channel, return a response string. Called by the xiaozhi ZeroClawLLM provider. |
| `GET /health` | Liveness probe. |

**Responsibilities per turn:**

1. Spawn (or reuse) a `zeroclaw acp` child, holding stdin/stdout open.
2. Send JSON-RPC `session/new` to get a fresh session_id. *(Note: currently one `session/new` per turn — see session reuse in [latent-capabilities.md](./latent-capabilities.md#brain-unused).)*
3. Wrap the user content in the English+emoji sandwich when `channel == "stackchan"`.
4. Send JSON-RPC `session/prompt` with the wrapped content.
5. Wait for the terminal result (not streamed — see [latent-capabilities.md](./latent-capabilities.md#brain-unused)).
6. Run `_ensure_emoji_prefix()` — if the first non-whitespace char isn't in the 9-emoji allowlist (😊😆😢😮🤔😠😐😍😴), prepend 😐.
7. Return JSON `{"response": "<cleaned text>"}`.

## The LLM — Qwen3-30B-A3B-Instruct-2507

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

## OpenRouter

Routing: OpenRouter fronts the Qwen3 model, handles multiple upstream providers, and exposes an OpenAI-compatible API. ZeroClaw's config references it via an `openrouter` provider section with encrypted API key.

Observability OpenRouter itself offers (not currently surfaced in Dotty):
- Per-request latency + cost dashboards.
- Multi-model A/B routing.
- Per-provider failover for the same model.

If Dotty's per-turn latency ever needs deeper analysis than ZeroClaw's `state/costs.jsonl` and `state/runtime-trace.jsonl`, OpenRouter's dashboard is where to look next.

## See also

- [protocols.md](./protocols.md#acp) — exact ACP RPC surface the bridge speaks.
- [voice-pipeline.md](./voice-pipeline.md) — what drives the bridge (xiaozhi's LLM provider).
- [latent-capabilities.md](./latent-capabilities.md#brain-unused) — streaming, session reuse, tool-use, MCP-server mode.
- [references.md](./references.md#brain) — ZeroClaw, ACP, Qwen3, OpenRouter links.

Last verified: 2026-04-24.
