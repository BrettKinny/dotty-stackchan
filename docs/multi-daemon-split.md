---
title: Multi-Daemon Split (Voice + Discord)
description: Run two ZeroClaw daemons on one host so voice and Discord can use different models, autonomy levels, and safety wrappers.
---

# Multi-Daemon Split — Voice + Discord on one host

## TL;DR

- ZeroClaw (as of 0.7.3) has **no per-channel model or autonomy override** — one daemon, one model, one autonomy level. If you want a child-safe local-fast model on the robot's voice channel and a stronger, broader-autonomy model on Discord, you need **two daemons**.
- Run two systemd units against two config dirs (`~/.zeroclaw/` and `~/.zeroclaw-discord/`). The voice daemon goes through the bridge (kid-mode + emoji-prefix enforcement); the Discord daemon talks to ZeroClaw's Discord channel directly.
- **Persona is shared via symlinks** (`SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`, `BOOTSTRAP.md`, `HEARTBEAT.md`, `skills/`). **Memory is per-daemon** (`memory.db`, `sessions/`, `MEMORY.md`).
- The **encryption key** (`.secret_key`) is **copied**, not regenerated, so the same encrypted `api_key` decrypts in both configs.
- **Skip this entirely** if you only run the voice channel, or if you're happy running both channels under the same model/autonomy.

## Why two daemons

ZeroClaw's config is global per-process. `default_model`, autonomy mode (`ReadOnly` / `Supervised` / `Full`), and the system-prompt scaffolding all apply to every channel the daemon serves. There is no `[channels.discord].model = "..."` override in 0.7.3.

That collides with two reasonable goals:

| Goal | Voice | Discord |
|---|---|---|
| Latency floor | Hard (TTS lip-sync, child attention span) | Soft (text, async-friendly) |
| Audience | Kids in the room | Operator (you) |
| Safety wrapper | Kid-mode + content filter | Trusted operator, no wrapper needed |
| Autonomy | Restrictive (no shell, no broad file write) | Broad (operator wants the agent to act) |
| Model | Fast, cheap, "good enough" — e.g. Mistral Small 3.2 | Strong reasoning — e.g. Claude Sonnet 4.6 |

Trying to satisfy both ends from one daemon means picking the *minimum* of every dimension: slowest model, tightest autonomy, kid-safe filtering on Discord traffic the operator never wanted filtered. Two daemons is the cleanest way to keep both channels honest.

This may collapse back to one daemon once ZeroClaw lands per-channel overrides — see [Future: collapsing back to one daemon](#future-collapsing-back-to-one-daemon).

## When to use this — and when not to

| Situation | Recommendation |
|---|---|
| You only ever speak to the robot via voice | **Single daemon.** Skip this whole doc. |
| You use Discord but are happy with the voice model and autonomy on Discord too | **Single daemon.** |
| You want different models, different autonomy, or different safety wrappers per channel | **Two daemons.** |
| You're running on tiny hardware (e.g. a Pi Zero) | **Single daemon** — two ZeroClaw processes will fight for RAM/CPU. |

The rest of this doc assumes you're committing to two daemons.

## The split at a glance

| | Voice daemon | Discord daemon |
|---|---|---|
| Config dir | `~/.zeroclaw/` | `~/.zeroclaw-discord/` |
| systemd unit | `zeroclaw-bridge.service` | `zeroclaw-discord.service` |
| Process | `python bridge.py` (which spawns `zeroclaw acp`) | `zeroclaw daemon --config-dir ~/.zeroclaw-discord` |
| Channel handler | `channel="stackchan"` via FastAPI HTTP → ACP stdio | ZeroClaw's built-in Discord channel (WebSocket gateway) |
| Talks through `bridge.py`? | **Yes** (kid-mode, emoji-prefix, English+emoji sandwich) | **No** — ZeroClaw connects to Discord directly |
| Typical model | Fast, kid-safe (e.g. Mistral Small 3.2) | Stronger reasoning (e.g. Claude Sonnet 4.6) |
| Typical autonomy | `Supervised`, narrow tool allowlist | `Supervised` or `Full`, broad tool access |
| `[channels.discord].enabled` | **`false`** (defensive — see below) | `true` |

### Why `[channels.discord].enabled = false` on the voice daemon

If both daemons run with Discord enabled, both will connect to Discord's gateway and fight for messages. Setting `enabled = false` on the voice daemon keeps it focused on `channel="stackchan"` and prevents accidental Discord activations if the voice config is copied from a template.

## Persona sharing via symlinks

Persona is intended to be **one identity, two surfaces**. Both daemons should believe they are the same character. Memory, in contrast, is **per-conversation context** — the robot's voice memory shouldn't leak into Discord's context window and vice versa.

Layout (under `~/.zeroclaw-discord/workspace/`, with arrows pointing to `~/.zeroclaw/workspace/`):

```
~/.zeroclaw-discord/workspace/
├── SOUL.md         → ~/.zeroclaw/workspace/SOUL.md       (symlink, shared)
├── IDENTITY.md     → ~/.zeroclaw/workspace/IDENTITY.md   (symlink, shared)
├── USER.md         → ~/.zeroclaw/workspace/USER.md       (symlink, shared)
├── AGENTS.md       → ~/.zeroclaw/workspace/AGENTS.md     (symlink, shared)
├── TOOLS.md        → ~/.zeroclaw/workspace/TOOLS.md      (symlink, shared)
├── BOOTSTRAP.md    → ~/.zeroclaw/workspace/BOOTSTRAP.md  (symlink, shared)
├── HEARTBEAT.md    → ~/.zeroclaw/workspace/HEARTBEAT.md  (symlink, shared)
├── skills/         → ~/.zeroclaw/workspace/skills/       (symlink, shared)
├── MEMORY.md       (real file, per-daemon)
├── memory.db       (real file, per-daemon)
├── memory/         (real dir, per-daemon)
└── sessions/       (real dir, per-daemon)
```

| File | Shared? | Why |
|---|---|---|
| `SOUL.md`, `IDENTITY.md`, `USER.md` | Shared | Core character — voice and Discord are the same agent. |
| `AGENTS.md`, `TOOLS.md`, `BOOTSTRAP.md`, `HEARTBEAT.md` | Shared | Behavioral conventions and startup invariants are identity-level, not channel-level. |
| `skills/` | Shared | Skills are agent capabilities; both daemons should have the same toolkit definitions. |
| `MEMORY.md` | **Per-daemon** | Long-term memories accumulate from real conversations; you don't want voice-channel memories surfacing in Discord context (or vice versa). |
| `memory.db`, `memory/`, `sessions/` | **Per-daemon** | SQLite backing store + session transcripts — same reasoning as `MEMORY.md`. |

**Implication:** if you edit `SOUL.md` (or run `POST /admin/persona` against the bridge), both daemons see the change immediately on their next message. No restart required for symlinked files. **Per-daemon files (`MEMORY.md` etc.) need to be edited in each config dir separately** if you want them in lockstep — but in practice you usually don't.

## The encryption key

ZeroClaw encrypts `api_key` and other secrets in `config.toml` using a per-config-dir key at `.secret_key`. If you generate a new key for the Discord config, the `api_key` value copied from the voice config won't decrypt.

**Correct procedure:**

```bash
cp ~/.zeroclaw/.secret_key ~/.zeroclaw-discord/.secret_key
chmod 600 ~/.zeroclaw-discord/.secret_key
```

Now you can copy the encrypted `api_key` line directly from the voice `config.toml` into the Discord one. Or use a different (separately encrypted) key — the point is, **don't let ZeroClaw auto-generate a new `.secret_key` in the Discord dir if you've already copied encrypted secrets in**.

## systemd units

Two units, one per daemon. Both run as the same user as your single-daemon setup (typically `<ZEROCLAW_USER>` or root, depending on how you set up the bridge originally).

`/etc/systemd/system/zeroclaw-bridge.service` (voice — unchanged from single-daemon setup):

```ini
[Unit]
Description=ZeroClaw bridge (voice path) + ACP child
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=<BRIDGE_PATH>
ExecStart=<BRIDGE_PATH>.venv/bin/python bridge.py
Restart=on-failure
RestartSec=2
Environment=ZEROCLAW_CONFIG_DIR=<ZEROCLAW_HOME>.zeroclaw

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/zeroclaw-discord.service` (new — Discord path):

```ini
[Unit]
Description=ZeroClaw Discord daemon
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=<ZEROCLAW_HOME>.cargo/bin/zeroclaw daemon --config-dir <ZEROCLAW_HOME>.zeroclaw-discord
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Enable both:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zeroclaw-bridge.service zeroclaw-discord.service
```

## bridge.py and kid-mode

Kid-mode (the English+emoji sandwich, content filter, restricted tool allowlist) lives in `bridge.py` and **only wraps voice traffic**. Specifically, the bridge guards its wrapping logic with `channel in VOICE_CHANNELS`, and Discord traffic never enters the bridge — it goes from Discord → ZeroClaw's Discord channel → the Discord daemon → LLM, with no FastAPI hop.

This is intentional and **load-bearing for the threat model**:

- Voice channel: real-time speech-to-text from a child in the room. Wrapping is mandatory.
- Discord channel: text from a known operator (locked down via `allowed_users`). Wrapping is unwanted — it'd cripple the agent's usefulness for ops/admin tasks.

If you ever want to add a *different* channel (say, Telegram) and route it through the voice safety wrapper, you'd add it to `VOICE_CHANNELS` in `bridge.py` *and* point that channel's traffic through the bridge — not just enable it on the voice daemon.

## Restricting Discord access

Because the Discord daemon runs with broad autonomy, lock the channel down to operator-only:

```toml
# ~/.zeroclaw-discord/config.toml
[channels.discord]
enabled = true
allowed_users = ["<YOUR_DISCORD_USER_ID>"]
```

Multiple IDs are fine if you have co-operators. Anyone not in the list will be ignored (or rejected, depending on ZeroClaw's policy — verify against your version's behavior before relying on it).

## Setup walkthrough

Assumes you have a working single-daemon (voice) setup already.

1. **Snapshot first.** `cp -a ~/.zeroclaw ~/.zeroclaw.bak-$(date +%Y%m%d-%H%M%S)` and back up `bridge.py` likewise.
2. **Stop the old all-purpose daemon** if you previously ran `zeroclaw.service` directly (without the bridge). The voice path now goes through `zeroclaw-bridge.service` only.
3. **Copy the config dir.**
   ```bash
   cp -a ~/.zeroclaw ~/.zeroclaw-discord
   ```
4. **Replace shared persona files with symlinks.** For each of `SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md`, `TOOLS.md`, `BOOTSTRAP.md`, `HEARTBEAT.md`, and `skills/` — delete the copy in `~/.zeroclaw-discord/workspace/` and replace with a symlink to the voice copy. Leave `MEMORY.md`, `memory.db`, `memory/`, and `sessions/` as real files.
5. **Reset Discord-side memory.** The copy from step 3 brought voice memories with it; clear them: `rm ~/.zeroclaw-discord/workspace/memory.db ~/.zeroclaw-discord/workspace/MEMORY.md` and let the daemon start fresh.
6. **Edit `~/.zeroclaw-discord/config.toml`:** flip `default_model` to your Discord-side model, set the autonomy level, enable Discord (`[channels.discord].enabled = true`), set `allowed_users`. Keep the encrypted `api_key` you copied; don't regenerate `.secret_key`.
7. **Edit `~/.zeroclaw/config.toml`:** set `[channels.discord].enabled = false` defensively.
8. **Drop in the systemd unit** at `/etc/systemd/system/zeroclaw-discord.service` (template above).
9. **Reload + enable + start.** `sudo systemctl daemon-reload && sudo systemctl enable --now zeroclaw-discord.service`.
10. **Verify.** Voice turn end-to-end (smoke test from the StackChan or a `curl` to the bridge), then a Discord DM from an `allowed_users` ID — they should hit different models. Tail both journals (see below) to confirm.

## What to check when it breaks

| Symptom | Where to look |
|---|---|
| Voice broken, Discord fine | `journalctl -u zeroclaw-bridge -f`, then the `zeroclaw acp` child's stderr (interleaved). |
| Discord broken, voice fine | `journalctl -u zeroclaw-discord -f`. Most likely cause: `.secret_key` mismatch (decryption error on `api_key`) or `allowed_users` typo. |
| Both broken after a persona edit | The symlinked file was replaced with a regular file by a non-atomic editor. Verify `ls -la ~/.zeroclaw-discord/workspace/` still shows arrows. |
| Same response on both channels (suspicious) | Check both daemons are actually running different models: `grep default_model ~/.zeroclaw/config.toml ~/.zeroclaw-discord/config.toml`. |
| Discord channel stops responding mid-conversation | Discord gateway hiccup — `systemctl restart zeroclaw-discord` and tail the journal. ZeroClaw will reconnect on its own most of the time. |
| Voice daemon picks up Discord messages | `[channels.discord].enabled` slipped back to `true` on the voice config. Set it back to `false` and restart. |

## Future: collapsing back to one daemon

Once ZeroClaw supports per-channel overrides for `model` and autonomy (tracked upstream — check the project's changelog), the right move is to merge the two daemons back into one. The two-daemon split exists *because* of a missing feature, not as a permanent architectural choice.

Migration outline (for when that day comes):

1. Move Discord-only settings into a `[channels.discord]` block on a single config.
2. Migrate Discord-side `MEMORY.md` and `memory.db` into the voice daemon (or merge selectively — your call).
3. Stop and disable `zeroclaw-discord.service`.
4. Verify both channels still hit the right models.

Until then, keep the split.

## See also

- [brain.md](./brain.md) — what's running inside each daemon (ZeroClaw runtime, ACP stdio, persona files).
- [protocols.md](./protocols.md#acp) — ACP wire format the voice daemon's bridge speaks.
- [voice-pipeline.md](./voice-pipeline.md) — the full voice path that terminates at `bridge.py`.
- [llm-backends.md](./llm-backends.md) — picking models per daemon (latency vs. capability tradeoff).
- [kid-mode.md](./kid-mode.md) — what the voice-only safety wrapper actually enforces.

Last verified: 2026-04-25.
