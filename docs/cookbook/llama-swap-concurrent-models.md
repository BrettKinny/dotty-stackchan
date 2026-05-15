---
title: Run Two Local Models Concurrently with llama-swap
description: Use llama-swap's matrix DSL to keep a small voice model and a big "thinking" model resident at the same time on one GPU, while a third long-context profile preempts both for coding sessions.
---

# Run Two Local Models Concurrently with llama-swap

Dotty's voice path benefits from a small fast model for chitchat (sub-second
replies) **plus** a bigger model for "think hard" escalations. Reloading the
big model from disk on every escalation kills the user experience — but a
naive llama-swap setup with two models on one GPU does exactly that: it
unloads one to load the other.

[llama-swap](https://github.com/mostlygeek/llama-swap) v211+ has a `matrix`
DSL that solves this. You declare which combinations of models are valid
"sets", and llama-swap picks the lowest-cost set that contains the model
you requested. If the requested model is already in the running set, no
load happens at all.

This recipe walks through a 3-alias setup that keeps two voice models
permanently warm while still letting a separate long-context profile take
the whole GPU when you want it.

## The pattern

Three model aliases on one llama-swap instance:

| Alias | Role | Context | When it's loaded |
|---|---|---|---|
| `chat-small` | voice chitchat | small (4–8K) | always, alongside `think-small` |
| `think-small` | voice `think_hard` escalation | small (8–16K) | always, alongside `chat-small` |
| `think-big` | long-context coding / deep work | big (64K+) | only when explicitly requested; evicts the others |

Routing is automatic via the OpenAI-compatible `model:` field in each request:

- Voice service / fast-path chat → `chat-small`
- Voice `think_hard` tool → `think-small`
- Coding agent / dev CLI → `think-big`

llama-swap's matrix solver does the eviction work. No manual restart, no
config swap.

## VRAM math (worked example)

This walk-through uses **2× RTX 3060 (12 GB each)**. Adapt numbers for your
own hardware — the constraint pattern is the same.

| Configuration | Model | KV cache | Total | Per GPU (layer-split) |
|---|---|---|---|---|
| `chat-small` alone | 4B-Q4_K_M, ~2.5 GB | f16, 4K ctx, ~0.5 GB | ~3 GB | Pinned to one GPU |
| `think-small` alone | 27B-UD-Q4_K_XL, ~17.6 GB | q4_0, 8K ctx, ~0.5 GB | ~18 GB | ~9 GB each |
| **Both small models** | combined | combined | ~21 GB | **GPU 0: ~12 GB, GPU 1: ~9 GB** |
| `think-big` alone | 27B-UD-Q4_K_XL, ~17.6 GB | q4_0, 96K ctx, ~6 GB | ~24 GB | ~12 GB each (full GPUs) |

Coexisting the two small models on 2× 12 GB is **tight but possible**. The
trick is to:

1. **Pin the small chat model to one GPU** via `CUDA_VISIBLE_DEVICES=0`.
   It's small enough not to need both cards.
2. **Tensor-split the bigger think model unevenly** (e.g. `-ts 1,1.5`)
   so it leans on GPU 1, where there's more free memory after the small
   model takes its share.
3. **Use small KV cache types** (`q4_0`) for the big model. For the small
   model, keep `f16` KV — the dequant cost of `q4_0` outweighs the
   bandwidth savings on a fast small model.
4. **Right-size context windows.** The big context profile (`think-big`)
   gets long ctx; the voice profiles get short ctx, because voice turns
   and `think_hard` questions don't span tens of thousands of tokens.

On a single 24 GB GPU (RTX 3090, 4090, etc.) the math gets easier — you
can layer-split or pin to GPU 0, and headroom is more forgiving.

## Configuration

`config.yaml` (illustrative — adjust paths, model files, and `env` for your
host):

```yaml
healthCheckTimeout: 90
logLevel: info
startPort: 10001

models:
  # Voice chitchat: small, fast, pinned to one GPU
  "chat-small":
    cmd: |
      /app/llama-server
      -m /models/<your-small-model>.gguf
      -ngl 99
      -c 4096
      -fa on
      --cache-type-k f16
      --cache-type-v f16
      -np 1
      --reasoning off
      --host 127.0.0.1
      --port ${PORT}
      --alias chat-small
    env:
      - "CUDA_VISIBLE_DEVICES=0"     # restrict to first GPU only
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  # Voice escalation: big model at small context, layer-split with weight
  # toward the GPU that isn't already hosting chat-small
  "think-small":
    cmd: |
      /app/llama-server
      -m /models/<your-big-model>.gguf
      -ngl 99
      -sm layer
      -ts 1,1.5
      -c 8192
      -fa on
      --cache-type-k q4_0
      --cache-type-v q4_0
      -np 1
      --host 127.0.0.1
      --port ${PORT}
      --alias think-small
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  # Coding agent: same big model, full context, runs alone
  "think-big":
    cmd: |
      /app/llama-server
      -m /models/<your-big-model>.gguf
      -ngl 99
      -sm layer
      -c 98304
      -fa on
      --cache-type-k q4_0
      --cache-type-v q4_0
      -np 1
      --host 127.0.0.1
      --port ${PORT}
      --alias think-big
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

# Concurrency matrix: declare which model combinations are allowed.
matrix:
  vars:
    cs: chat-small
    ts: think-small
    tb: think-big

  sets:
    # Two voice models coexist
    voice: "cs & ts"
    # Big-context profile runs alone — evicts the voice pair when called
    coding: "tb"
```

The matrix solver behavior is well-described in the upstream
[config.example.yaml](https://github.com/mostlygeek/llama-swap/blob/main/config.example.yaml).
Short version: when a request arrives for model X, llama-swap finds the
cheapest set containing X (evicting the fewest models) and starts X if it
isn't already running.

## How the routing plays out in practice

- **Dotty is idle, then user speaks.** Voice service sends a `chat-small`
  request. Set `voice` (`cs & ts`) contains it — but only `cs` is needed
  right now. `chat-small` loads (~5–10 s cold), reply streams back.
  `think-small` is *not* preloaded.
- **First `think_hard` invocation.** Voice tool sends a `think-small`
  request. Same `voice` set already contains `chat-small`, so no eviction;
  `think-small` starts (~30–50 s cold-load for a 27B model). From then on
  both are resident. Subsequent `think_hard` calls warm-stream.
- **User starts a coding session.** Dev CLI sends a `think-big` request.
  Solver picks set `coding` (`tb` alone), evicts both small models, loads
  the long-context profile. Cold-load again.
- **User finishes coding, talks to Dotty.** Voice service sends
  `chat-small`. Solver picks `voice`, evicts `think-big`, loads `chat-small`.
  Next `think_hard` re-warms `think-small`.

Both transitions back into voice cost a cold-load on the first turn after
coding (~5–10 s for the small chat model, plus ~30–50 s when the user first
triggers `think_hard`). After that, the voice pair is warm and stays warm
indefinitely (TTL is per-model and is reset on use).

## Tradeoffs

**You give up:** big-context inference *concurrent* with voice. The
voice path and coding agent are mutually exclusive within a single
llama-swap instance because the big-context profile needs the whole GPU.

**You gain:** voice `think_hard` is sub-3-second warm instead of paying a
30–50 s cold-load on every escalation, and voice chitchat is unaffected
when no coding is happening.

**Watch out for:** the per-GPU VRAM ceiling, especially with split mode.
Layer-split halves the model across cards, but the small model pinned to
one card takes that card's headroom first. Use `nvidia-smi` to check
post-load.

**Knobs to adjust if it doesn't fit:**

- Shrink `chat-small` context (4K → 2K) — voice turns rarely exceed this.
- Shrink `think-small` context (8K → 4K) — `think_hard` questions are
  usually a few-paragraph prompt with a few-paragraph answer.
- Use a smaller quant on the big model (Q4_K_M → Q3_K_M) — quality cost,
  ~3 GB savings.
- Switch the small model to a smaller one (4B → 1.5B) — quality cost, ~1.5 GB
  savings, sub-second responses.

## Verifying the setup

After applying the config and restarting llama-swap:

```bash
# 1. Confirm all three aliases advertised
curl -s http://<LLAMA_SWAP_HOST>:8080/v1/models | jq '.data[].id'

# 2. Warm up chat-small
curl -s http://<LLAMA_SWAP_HOST>:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"chat-small","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'

# 3. Warm up think-small (max_tokens generous — reasoning models eat tokens)
curl -s http://<LLAMA_SWAP_HOST>:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"think-small","messages":[{"role":"user","content":"hi"}],"max_tokens":4096}'

# 4. Check VRAM — both should be resident, no GPU at full
ssh root@<LLAMA_SWAP_HOST> 'nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv'

# 5. Sanity-check eviction: requesting think-big should kick the small ones
curl -s http://<LLAMA_SWAP_HOST>:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"think-big","messages":[{"role":"user","content":"hi"}],"max_tokens":4096}'
ssh root@<LLAMA_SWAP_HOST> 'nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv'
# Expect: think-big now resident, chat-small + think-small evicted.
```

If `think-small` returns `content: ""` with `finish_reason: length` and a
populated `reasoning_content`, your model is emitting `<think>` reasoning
tokens before the answer — bump `max_tokens` (4K is a reasonable floor for
reasoning-on models). For Qwen3-family models you can also pass
`reasoning_effort: minimal` to bound it.

## See also

- [llm-backends.md](../llm-backends.md) — picking among cloud / Ollama /
  ZeroClaw backends at the xiaozhi-server slot.
- [run-fully-local.md](./run-fully-local.md) — single-model local setup
  via the Ollama compose override.
- [llama-swap upstream README](https://github.com/mostlygeek/llama-swap)
  for the full matrix DSL spec and other features (preload, hooks, peers).

Last verified: 2026-05-15.
