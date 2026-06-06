# dotty-pi

Production Docker image for the **pi coding agent** running as Dotty's
voice-tool brain on Unraid. Replaces the RPi-hosted `zeroclaw-bridge`
per [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36).

## What this is

A pinned `node:25.9-alpine3.23` image with `@earendil-works/pi-coding-agent`
installed globally. Idles via `sleep infinity`; voice turns invoke pi on
demand via `docker exec -i` from the Unraid-local `PiClient` (lives in
[`../custom-providers/pi_voice/`](../custom-providers/pi_voice/)).

The runtime contract is:

- **xiaozhi-server** routes voice-LLM calls to the `PiVoiceLLM` provider.
- **PiVoiceLLM / PiClient** translates each turn into a pi RPC request.
- **pi** (this container) runs the prompt against llama-swap on the same
  host (`http://localhost:8080/v1`, model `qwen3.6:27b` by default), with
  the [`dotty-pi-ext`](../dotty-pi-ext/) extension loaded for the seven
  voice tools (`memory_lookup`, `remember`, `recall_person`,
  `remember_person`, `think_hard`, `take_photo`, `play_song`).

## Build + run on Unraid

Use the deploy script — it ships the build context, config, and extension
source, builds the pinned image, recreates the container, and healthchecks:

```bash
DOTTY_PI_HOST=root@<UNRAID_HOST> bash scripts/deploy-dotty-pi.sh
```

The script is the repeatable replacement for the old hand-run
`docker build … && docker compose up -d`. It writes only the build context,
`agent/models.json`, and `extensions/dotty-pi-ext/` — it never touches the
live `memory/brain.db` or `persona/`, and it preserves the extension's
hand-compiled `node_modules` (deps are unchanged; see
[`scripts/deploy-dotty-pi.sh`](../scripts/deploy-dotty-pi.sh) for the full
contract). A functional voice-tool smoke test is a manual post-deploy step
(the script prints the reminder) — keep the agent loop on `qwen3.5:4b`.

On-box layout (build context and live state are **separate** directories):

```
/mnt/user/appdata/
├── dotty-pi-src/                # build context (SRC_DIR)
│   ├── Dockerfile
│   └── docker-compose.yml
└── dotty-pi/                    # bind-mount → /root/.pi (STATE_DIR)
    ├── agent/
    │   ├── models.json          # provider config (deployed)
    │   ├── auth.json            # live — never touched by deploy
    │   └── sessions/            # live — never touched by deploy
    ├── persona/                 # Dotty persona — migrated from RPi (live)
    ├── memory/
    │   └── brain.db             # FTS5 store — migrated from RPi (live)
    ├── sessions/                # pi session state (unused for now)
    └── extensions/
        └── dotty-pi-ext/        # voice-tool extension source (deployed)
            └── node_modules/    # hand-compiled better-sqlite3 (preserved)
```

## Model selection — DO NOT use `qwen3.6:27b` here

llama-swap (`/mnt/user/appdata/llama-swap/config.yaml`) groups models
into matrix sets — `voice` (resident: `qwen3.5:4b` + `qwen3.6:27b-think`)
and `coding` (resident: `qwen3.6:27b` alone). Requesting the coding-set
model evicts both voice models; reloading either is a 30–50 s cold hit.

The cutover model split (validated 2026-05-17 end-to-end):

| Loop | Model | Why |
|---|---|---|
| Outer agent (`pi --model …`) | `qwen3.5:4b` | Fast, in voice set, drives tool-routing reliably enough for Dotty's flat tool surface. |
| `think_hard` escalation | `qwen3.6:27b-think` | The 8K-context 27B, in voice set, resident alongside 4B. Direct llama-swap POST inside the extension; no agent overhead. |

**Never** call `pi --model qwen3.6:27b` from this container — it evicts
`qwen3.6:27b-think` and the next `think_hard` call times out at 30 s
waiting for the cold reload. The integration test on 2026-05-17 caught
this exact failure (returned `"(I'm slow today, try again in a moment)"`
twice before the fix).

Measured wall-clock for the 4B + 27B-think split:

- `memory_lookup` (no LLM escalation): ~5.8 s total (4B turn + tool + reply)
- `think_hard` ("reply with `pong`"): ~45 s total warm (4B turn + tool fires inner 27B-think call + reply)

The `models.json` shipped here registers all three aliases but the agent
loop should always target `qwen3.5:4b`.

## Versioning

| Tag | Pi version | Notes |
|---|---|---|
| `dotty-pi:0.1.0` | `0.74.0` | Production-grade promotion of the 2026-05-15 spike. |
| `dotty-pi:spike` | `0.74.0` | The original day-0 spike (`audits/pi-rpc-spike-report.md`). Keep until production is soaked. |

Bump the image tag deliberately when pi or node moves; do not use floating
tags. Cutover testing depends on a known-good image.

## See also

- [`../dotty-pi-ext/README.md`](../dotty-pi-ext/README.md) — voice-tool extension contract.
- [`../custom-providers/pi_voice/README.md`](../custom-providers/pi_voice/README.md) — xiaozhi-side glue.
- [#36](https://github.com/BrettKinny/dotty-stackchan/issues/36) — the cutover plan + soak rule.
