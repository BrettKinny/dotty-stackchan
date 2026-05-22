# pi_voice

xiaozhi-server custom LLM provider that routes voice turns through the
[`dotty-pi`](../../dotty-pi/) container instead of bridge.py. The
RPi-replacement path per [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36).

**Status: production.** Wired into xiaozhi-server via `selected_module.LLM: PiVoiceLLM`. Tier1Slim (`tier1_slim`) is the documented rollback path. 6/6 unit tests pass.

What works:
- `pi_client.py` — long-lived `pi --mode rpc` client; spawns once,
  reuses across turns via `new_session`. Filters `thinking_delta`,
  auto-cancels dialog `extension_ui_request`s, drops fire-and-forget
  UI requests. Throws `PiClientError` on rejected prompts / timeouts.
- `pi_voice.py` — `LLMProvider` subclass that translates xiaozhi's
  `(session_id, dialogue)` → pi prompt and yields text deltas back as
  a sync generator (the shape xiaozhi's voice loop expects).
- `tests/test_pi_client.py` — pure-Python unit tests with a fake
  subprocess for the 3 Step-5 invariants + prompt-rejection +
  timeout. Run with `python3 -m unittest custom-providers.pi_voice.tests.test_pi_client`.

What's not yet done (the live-integration items):
- Volume-mounting this dir into the xiaozhi container.
- Flipping `selected_module.LLM: PiVoiceLLM` and soaking.
- Sandwich enforcement (Tier1Slim wraps every turn with
  `build_turn_suffix(KID_MODE)` from `core.utils.textUtils`; needs
  porting either here or into the pi extension's system prompt).
- Memory write-back from xiaozhi-server (Tier1Slim posts conversation
  logs + remember-markers; PiVoiceLLM currently ignores those).

## Architecture

```
xiaozhi-server (Docker)                     dotty-pi (Docker, same host)
┌────────────────────────────┐              ┌──────────────────────────────┐
│  selected_module.LLM:      │              │  pi (idling via sleep ∞)     │
│    PiVoiceLLM              │              │                              │
│       │                    │              │  on `docker exec -i` from    │
│       ↓ async call         │              │  PiClient:                   │
│  custom-providers/         │  docker exec │    pi --provider ollama      │
│    pi_voice/               │  ───────────→│      --model qwen3.6:27b     │
│      pi_voice.py           │              │      --extensions dotty-pi-ext│
│      pi_client.py          │  ←─────────  │      --thinking minimal      │
│                            │  stdout RPC  │      <prompt>                │
└────────────────────────────┘              └──────────────────────────────┘
                                                          ↓
                                                ┌──────────────────────┐
                                                │ dotty-pi-ext         │
                                                │   5 voice tools      │
                                                │   (memory_lookup,    │
                                                │    think_hard, …)    │
                                                └──────────────────────┘
                                                          ↓
                                            llama-swap (qwen3.6:27b-think)
                                            xiaozhi-admin (songs, MCP)
                                            brain.db (FTS5)
```

## Components

### `pi_voice.py` — xiaozhi LLMProviderBase subclass

Translates xiaozhi's chat-completion interface to a pi RPC turn. Async
`chat_stream` shape so xiaozhi can pipe text deltas straight to TTS
without buffering the full reply.

### `pi_client.py` — Unraid-local RPC client

Owns the long-lived pi process. Per #36's Step-5 constraints:

- **Single persistent pi process** spawned once per xiaozhi-server boot
  (don't respawn per turn — that recovers the 1.2–1.8 s spike-measured
  startup tax).
- **Auto-cancel `extension_ui_request`** with `{cancelled: true}` to
  prevent pi from blocking on UI prompts no one will answer.
- **Filter `assistantMessageEvent.type == "thinking_delta"`** out of the
  event stream the provider yields back to xiaozhi (per spike: 19
  thinking deltas vs 3 text deltas per turn; only text reaches TTS).

### `__init__.py` — package marker

So xiaozhi-server's `core.providers.llm.pi_voice` import path resolves.

## Wiring into xiaozhi-server

Three things are required, all in the repo's `docker-compose.yml`:

```yaml
volumes:
  # 1. the provider package itself
  - ./custom-providers/pi_voice:/opt/xiaozhi-esp32-server/core/providers/llm/pi_voice
  # 2. + 3. docker CLI binary + host docker socket — PiClient shells out to
  # `docker exec -i dotty-pi pi --mode rpc ...` from INSIDE this container,
  # which needs both the binary in $PATH and access to the daemon.
  - /var/run/docker.sock:/var/run/docker.sock
  - /usr/bin/docker:/usr/bin/docker:ro
```

⚠️ **Security caveat:** bind-mounting `/var/run/docker.sock` gives this
container effective root on the docker host — it can `docker run --privileged
anything` against the daemon. Acceptable for a single-purpose self-hosted
appliance like Dotty; do NOT enable on a shared / multi-tenant host. If that
trade-off isn't acceptable in your environment, refactor `pi_client.py` to
talk to pi over a TCP/Unix socket exposed by a sidecar (out of scope for v1).

Then in `data/.config.yaml`:

```yaml
selected_module:
  LLM: PiVoiceLLM

LLM:
  PiVoiceLLM:
    type: pi_voice
    container_name: dotty-pi
```

The model + extension wiring lives container-side (in `dotty-pi/models.json`
and the bind-mounted `dotty-pi-ext/`); xiaozhi-server doesn't need to know
about them. The container default is `qwen3.5:4b` outer + `qwen3.6:27b-think`
escalation per `dotty-pi/README.md` — using `qwen3.6:27b` here would evict
the voice matrix set, see that README's "Model selection" section.

The `bridge.py` admin dashboard service continues to run independently;
it is no longer in the voice path. Its former `/api/voice/*` and
`/api/message` routes were retired in #36.

### Recovery: known-good rollback

If PiVoiceLLM misbehaves, flip back to Tier1Slim in `data/.config.yaml`
(`selected_module.LLM: Tier1Slim`) and `docker compose restart
xiaozhi-esp32-server`. The Tier1Slim provider, its volume mount, and its
config block are still intact in the repo as a fallback. The docker-socket
mount above is harmless when running other LLM providers.

## Open questions resolved during this slice

- **Stream shape.** xiaozhi expects `response()` to be a *sync generator
  yielding strings* (verified against `tier1_slim.LLMProvider.response`).
  `LLMProvider.response()` here matches that exactly.
- **Tool-call surfacing.** Pi owns the agent loop; tool calls happen
  *inside* pi (via `dotty-pi-ext`) and only their text-shape result ever
  leaves the container. xiaozhi never sees `tool_calls` from this
  provider — unlike Tier1Slim, which parses them itself.
- **Wire-protocol details.** `extension_ui_response` cancel shape from
  pi's `docs/rpc.md`; `assistantMessageEvent` filtering rule from the
  spike telemetry.

## Open questions still on the table

- **Sandwich enforcement.** Tier1Slim wraps every turn with
  `build_turn_suffix(KID_MODE)` from `core.utils.textUtils`. PiVoiceLLM
  needs the same — either inject server-side here, or move the
  sandwich into the pi extension's system prompt. Latter is cleaner
  but means kid-mode toggles need to push a system-prompt swap into
  the container.
- **Memory write-back.** Tier1Slim posted every turn to
  `bridge.py:/api/voice/memory_log` + `/api/voice/remember` — those
  endpoints are retired. Memory write-back now belongs in the pi
  extension: a small write (sqlite_brain_db.write) triggered by a
  `[REMEMBER: …]` marker in the final assistant text, plus a per-turn
  log row.
- **Persona file location.** The pi extension reads from
  `/mnt/user/appdata/dotty-pi/persona/` (bind-mounted into the container).
  Wiring is stable; runtime persona-swap mechanism TBD.

## See also

- [`../../dotty-pi/README.md`](../../dotty-pi/README.md) — the runtime image.
- [`../../dotty-pi-ext/README.md`](../../dotty-pi-ext/README.md) — voice-tool extension.
- [`../tier1_slim/tier1_slim.py`](../tier1_slim/tier1_slim.py) — reference for the
  chat_stream contract + sandwich wiring + tool dispatch pattern.
- [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36) — cutover plan + soak rule.
