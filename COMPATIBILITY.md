# Firmware / Server Compatibility Policy

## What this document covers

This document defines the contract between the StackChan firmware and the
server-side components: xiaozhi-esp32-server, zeroclaw-bridge (`bridge.py`),
and the ZeroClaw agent. It describes what each component exposes, what counts
as a breaking change, and how to upgrade safely.

For protocol wire formats see [docs/protocols.md](docs/protocols.md).

## Compatibility matrix

| Component | Current Version | Protocol / Interface | Breaking Change Policy |
|---|---|---|---|
| StackChan firmware (m5stack/StackChan v1.2.4) | v1.2.4 | Xiaozhi WebSocket protocol, MCP over WS (JSON-RPC 2.0) | Pin firmware to a known-good build; do not OTA-update without verifying server compatibility first |
| xiaozhi-esp32-server (local build) | `xiaozhi-esp32-server-piper:local` | Custom LLM provider API, `.config.yaml` schema, Xiaozhi WS server | Rebuild image only after checking upstream changelog for provider API or config schema changes |
| zeroclaw-bridge (`bridge.py`) | unversioned (HEAD) | HTTP API (`/api/message`, `/api/message/stream`, `/health`), ACP JSON-RPC 2.0 over stdio | Endpoint signatures and NDJSON streaming format are stable; changes require updating the custom LLM provider in lockstep |
| ZeroClaw | latest (`zeroclaw acp`) | ACP protocol (session management, `session/prompt`, `session/update`), tool surface | Bridge auto-approves tool calls; new ZeroClaw versions that change ACP semantics require bridge review |

## What counts as a breaking change

Any of the following require coordinated updates across components:

- **MCP tool surface** -- adding, removing, or renaming tools the firmware
  advertises via `tools/list`, or changing their `inputSchema`.
- **WebSocket frame shape** -- changes to the JSON message-type catalog
  (`hello`, `listen`, `stt`, `tts`, `llm`, `mcp`, `abort`) or binary audio
  framing versions.
- **Emotion-emoji protocol** -- changes to the emoji allowlist in `bridge.py`
  (`_ensure_emoji_prefix`), the upstream 21-emotion catalog, or the
  `llm`-type frame format.
- **OTA handshake** -- changes to the OTA endpoint (`/ota/`), expected
  headers, or firmware version negotiation.
- **Config schema** -- structural changes to `.config.yaml` (new required
  keys, renamed sections, removed defaults).
- **Bridge HTTP API** -- changes to request/response shapes on `/api/message`
  or `/api/message/stream`, or to the NDJSON streaming format.
- **ACP session semantics** -- changes to `session/new`, `session/prompt`, or
  `session/request_permission` behavior between ZeroClaw and the bridge.

## Versioning strategy

No formal versioning is adopted yet (tracked in
[ROADMAP.md](ROADMAP.md#community-wishlist) under "Firmware/server
compatibility matrix"). When adopted, the plan is:

- Separate tag namespaces: `server-vX.Y.Z` and `fw-vX.Y.Z`.
- This matrix will document which server versions are compatible with which
  firmware versions.
- The bridge will carry its own version once it moves to a tagged release
  cadence.

## Upgrade guidance

1. **Check this matrix first.** Confirm the component you are upgrading is
   compatible with the versions of the other components you are running.
2. **Back up before upgrading.** Run `scripts/backup.sh` (or the equivalent
   manual steps) to snapshot config, persona files, and bridge state.
3. **Upgrade one component at a time.** Validate with a round-trip test
   (`curl -X POST http://<ZEROCLAW_HOST>:8080/api/message ...`) before moving to the
   next component.
4. **Tail logs during validation.** Watch both the xiaozhi-server container
   logs and the bridge journal simultaneously to catch mismatches early.
5. **Roll back if broken.** Restore from the backup taken in step 2 and
   revert to the previous image or binary.

## Release process

### Tag namespaces

Server and firmware are versioned independently:

- Server (bridge, custom providers, docker compose): `server-vX.Y.Z`
- Firmware (ESP32-S3 StackChan firmware): `fw-vX.Y.Z`

### SemVer rules

| Bump | Server | Firmware |
|------|--------|----------|
| **Major** | Breaking change to bridge HTTP API, NDJSON streaming format, or ACP session semantics | Breaking change to WebSocket frame shape, MCP tool surface, or OTA handshake |
| **Minor** | New endpoint, new provider, new config key (backward-compatible) | New emotion, new MCP tool, new config option |
| **Patch** | Bug fix, performance improvement, doc-only change | Bug fix, cosmetic animation change |

### Cutting a release

1. Update `CHANGELOG.md` — move items from `[Unreleased]` into a new
   `[server-vX.Y.Z]` or `[fw-vX.Y.Z]` section with today's date.
2. Update the compatibility matrix in this file if the new version changes
   any interface listed in "What counts as a breaking change."
3. Commit with message: `release: server-vX.Y.Z` (or `release: fw-vX.Y.Z`).
4. Create an annotated tag: `git tag -a server-vX.Y.Z -m "server-vX.Y.Z"`
5. Push the tag: `git push origin server-vX.Y.Z`
6. Deploy versioned docs (handled automatically by `.github/workflows/docs-deploy.yml`
   on tag push; if you need to deploy manually, run
   `mike deploy --push --update-aliases X.Y latest` from the repo root after
   `pip install -r docs/requirements.txt`). See
   [`docs/versioning.md`](docs/versioning.md) for the full URL/alias model.

GitHub Actions handles image builds, artifact publishing, and versioned doc
deploys from the tag.

### Compatibility matrix updates

When either component ships a new version, add a row (or update the existing
row) in the compatibility matrix above so operators can verify which server
versions work with which firmware versions.

---

Last verified: 2026-04-25.
