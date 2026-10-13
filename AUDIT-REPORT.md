# dotty-stackchan — Project-Wide Audit Report

## Executive summary

This audit covered the full `dotty-stackchan` stack — the four server-side containers (xiaozhi-server, dotty-pi, dotty-behaviour, bridge dashboard), the xiaozhi-server drop-in patches, the ASR/TTS providers, the voice LLM providers, the dotty-pi-ext voice tools, the StackChan ESP32-S3 firmware, the top-level scripts, the docs, the monitoring assets, the shipped compose templates, the open GitHub issues, and the open PRs.

**282 findings** were reported: **4 critical, 24 high, 114 medium, 125 low, 15 info**. By kind: **157 bugs, 47 doc, 44 quality, 19 PR reviews, 15 issue triages**. Four false positives were rejected during verification.

**Headline risks:**

1. **A remote-LAN → docker-host-root exploit chain (G9, critical).** Unauthenticated `/xiaozhi/admin/*` routes on a `0.0.0.0:8003` listener + a `/var/run/docker.sock` mount in the same container + host networking on the other three services compose into a single hop from LAN to host root. The individually-reported symptoms (unauth admin routes F16, arbitrary-path play-asset G10, all-root containers G11) are each one link of this chain.
2. **PR #141 is a personal de-safety fork mislabeled as a bug fix (F120–F124, two critical).** It blanks the kid-mode/safety turn suffix, drops all system messages, disables the perception relay, and adds an explicit-sexual-content persona with hardcoded PII to a repo whose default audience is children. **It must be closed, not merged.**
3. **The shipped all-in-one compose is structurally broken (G18, G19, critical/high).** `compose.all-in-one.yml` omits the docker.sock mount the default `PiVoiceLLM` provider requires and the xiaozhi-patches mounts the entire behaviour layer depends on — a fresh deploy following its own Quick start gets a non-functional voice path.
4. **Kid-safety guardrails are thinner than the docs imply.** There is no output content filter on any live path (#138 still open); `memory_lookup` can surface unreviewed `person_pending` minor facts (F251); and the dashboard `say`/`start-story` ingresses bypass filtering entirely (F186).

## How this was produced

Multi-agent adversarial audit of the whole repository: parallel area specialists, **3 deepen rounds** to chase compound/cross-file issues, **3-vote adversarial verification** (3 independent lenses per finding), and a completeness critic that swept files never targeted as primary audit areas (monitoring assets, compose templates, the emoji→firmware contract, the vendored `firmware/server/` tree).

**Caps hit:** verification was capped at **50 of 172 qualifying findings**; the lowest-severity overflow is reported **unverified**. Critical/high findings were prioritized for the 3-lens treatment. Where a finding shows "confirmed N/3 lenses" it was multi-vote verified; "confirmed 1/1 lenses" is a single completeness-critic confirmation; "unverified — over cap" / "low-sev, unverified" means it was reported but not put through the multi-vote gate.

---

## gap: Container privilege/trust boundary (docker.sock + host networking)

### [CRITICAL] Remote-LAN → docker-host-root exploit chain
**File:** `docker-compose.yml.template` (compose 43-60, 596) + `custom-providers/xiaozhi-patches/http_server.py` (596, 620-672)
**Issue:** The four-container trust boundary collapses into one exploitable chain. xiaozhi-server binds `0.0.0.0:8003` and registers all 11 `/xiaozhi/admin/*` routes with no auth; the same container mounts `/var/run/docker.sock` + `/usr/bin/docker` (the compose comment itself admits this gives "effective root on the docker host"); the other three services run `network_mode: host` with no segmentation; none of the four Dockerfiles set `USER`, so every container is root. An attacker on the LAN/Tailnet has a confirmed write primitive into the socket-holding root container.
**Fix:** Break the chain at >1 link: (1) replace the raw docker.sock with a least-privilege `docker-socket-proxy` exposing only `exec`; (2) add a shared-secret bearer-token aiohttp middleware over `/xiaozhi/admin/*`; (3) bind admin HTTP to `127.0.0.1` and reach it only from sibling containers on loopback; (4) add non-root `USER` to all four Dockerfiles. Do at least the socket-proxy + route auth.
**Verification:** confirmed 1/3 lenses (completeness critic; compound finding assembled from confirmed symptoms).

### [HIGH] play-asset accepts arbitrary absolute filesystem path with no allowlist
**File:** `custom-providers/xiaozhi-patches/http_server.py:376-411`
**Issue:** `/xiaozhi/admin/play-asset` reads `asset` as a raw absolute path; the only validation is `os.path.exists`, then it is handed to `AudioSegment.from_file` (ffmpeg). The sibling `songs` handler hard-codes a base dir + extension allowlist — play-asset has neither. An unauthenticated LAN caller can point ffmpeg at any readable file (existence-probe via 404-vs-200, exercise libav demuxer CVEs) inside the docker.sock-holding container.
**Fix:** `os.path.realpath(asset)` must start with the canonical songs base before any decode; enforce the `{opus,ogg,wav,mp3}` allowlist; prefer accepting only a basename joined onto the fixed base.
**Verification:** confirmed 1/1 lenses.

### [HIGH] All four containers run as root (no USER directive)
**File:** `Dockerfile`, `dotty-pi/Dockerfile`, `dotty-behaviour/Dockerfile`, `bridge/Dockerfile`
**Issue:** None of the four images declare `USER`. For xiaozhi-server the root process is the one mounting docker.sock, so any code-exec bug there is already host root. The FastAPI/uvicorn and pi workloads do not need root.
**Fix:** Add a non-root `USER` to each Dockerfile (create app user, chown state dirs). Pair with `no-new-privileges:true` and read-only rootfs where feasible; run the socket-proxy so root-in-container is no longer root-on-host.
**Verification:** confirmed 1/1 lenses.

### [MEDIUM] Host networking removes network segmentation
**File:** `dotty-pi/docker-compose.yml:17`, `dotty-behaviour/docker-compose.yml:23`, `bridge/docker-compose.yml:31`
**Issue:** Three of four services use `network_mode: host`, so their listeners bind every interface with no bridge isolation and a compromise of any one has unrestricted loopback access to every host service. This is the segmentation half of why the chain finding is critical rather than contained.
**Fix:** Where loopback-to-llama-swap is the only reason for host mode, use a shared user-defined bridge network instead; bind behaviour/bridge to `127.0.0.1` if callers are host-local; at minimum document the host-networking decision as an explicitly accepted risk.
**Verification:** low-sev, unverified.

---

## gap: Shipped compose templates & .env.example (config-drift surface)

### [CRITICAL] compose.all-in-one.yml omits the docker.sock + docker CLI mounts PiVoiceLLM requires
**File:** `compose.all-in-one.yml:64-79`
**Issue:** The shipped config defaults to `selected_module.LLM: PiVoiceLLM`, which reaches the brain via `docker exec -i dotty-pi pi`. That needs the host docker socket + docker CLI bind-mounted (provided by `docker-compose.yml.template:59-60`). compose.all-in-one.yml mounts the provider dir but NOT the socket or binary — so a new user following the file's own Quick start with the default config gets a container that cannot exec into dotty-pi, and every voice turn fails. The file's own header even says PiVoiceLLM "requires the host docker socket" but never mounts it.
**Fix:** Add the two PiVoiceLLM mounts to compose.all-in-one.yml. Or, if all-in-one is meant to be OpenAICompat-only, change the shipped `selected_module` default and say so in the header.
**Verification:** confirmed 1/1 lenses.

### [HIGH] compose.all-in-one.yml is missing the xiaozhi-patches mounts
**File:** `compose.all-in-one.yml:64-79`
**Issue:** The canonical template mounts `portal_bridge.py`, `websocket_server.py`, `http_server.py`, and `textMessageHandlerRegistry.py` (admin routes, active_connections registry, perception relay). all-in-one mounts none of them, so a user deploying with this file gets upstream xiaozhi-server with no admin portal and no perception relay — the entire dotty-behaviour layer is inert (consumers fire into 404s). Also missing vs the template: openai_compat, personas, receiveAudioHandle.py, dances.py, textUtils.py, songs, and the kid/smart state mount.
**Fix:** Bring the volume list to parity with `docker-compose.yml.template`, or generate the all-in-one from the template so it can't drift.
**Verification:** confirmed 1/1 lenses.

### [MEDIUM] compose.all-in-one.yml never sets VISION_BRIDGE_URL/BRIDGE_URL
**File:** `compose.all-in-one.yml:56-58`
**Issue:** The relay resolves its target from `BRIDGE_URL`/`VISION_BRIDGE_URL`; with neither set it logs "dropping perception event" and returns. all-in-one's environment block sets only `TZ`, so even if the relay handler were mounted, every perception event would be dropped.
**Fix:** Add `VISION_BRIDGE_URL=http://<host-LAN-IP>:8090` (document `BRIDGE_URL` as its alias) alongside mounting the relay handler.
**Verification:** confirmed 1/1 lenses.

### [MEDIUM] .env.example omits load-bearing xiaozhi-server vars
**File:** `.env.example:4-21`
**Issue:** Several vars the template declares and the code reads are absent: `VISION_BRIDGE_URL`, `DOTTY_KID_MODE_STATE`, `DOTTY_SMART_MODE_STATE`, `DOTTY_STATE_DIR`, `DOTTY_PI_CONTAINER`, `DOTTY_PI_EXTRA_FLAGS`. The state-dir/kid-mode ones desync the firmware LED pips from the dashboard if left at the dead default.
**Fix:** Add the missing vars with the same explanatory comments already in `docker-compose.yml.template`.
**Verification:** low-sev, unverified.

### [LOW] Dead '/root/zeroclaw-bridge default' fallback comment in template
**File:** `docker-compose.yml.template:38-40`
**Issue:** The comment warns the reader "falls through to the dead /root/zeroclaw-bridge default"; the real defaults are `/var/lib/dotty-bridge/state/{kid,smart}-mode`. ZeroClaw was retired in #36.
**Fix:** Rewrite the comment to state the real default; grep for other zeroclaw/RPi residue.
**Verification:** low-sev, unverified.

### [LOW] .env.example documents many dotty-behaviour vars but not SOUND_TURN_YAW_DEG
**File:** `.env.example:23-153`
**Issue:** The file mixes xiaozhi-server and dotty-behaviour vars; `SOUND_TURN_YAW_DEG` (read at `dotty-behaviour/config.py:105`) is absent while sibling perception vars are documented — arbitrary coverage that obscures which container consumes each var.
**Fix:** Split into a per-service `.env.example`, or section-label each block by consuming container; add `SOUND_TURN_YAW_DEG`.
**Verification:** low-sev, unverified.

---

## PR #141

### [CRITICAL] #141 is a personal de-safety fork, not a bug fix — close
**File:** PR #141
**Issue:** The title claims to fix kid-mode constraints and system-prompt leakage; the diff does the opposite — it (a) blanks the safety/format suffix (`_TURN_SUFFIX = ""`), (b) drops all system messages, (c) comments out the perception relay + room_view capture, (d) adds `personas/sweetheart.md`, an explicit-sexual-content persona with the author's hardcoded PII. None is gated, tested, or documented; all of it contradicts the repo's stated kid-safe scope (ages 4-8).
**Fix:** **Close the PR.** Extract only the legitimate piper emoji-strip into a standalone PR. Ask the author to keep the persona, prompt-stripping, and perception-disabling on a private fork.
**Verification:** confirmed 3/3 lenses.

### [CRITICAL] _TURN_SUFFIX blanked — disables English-only, emoji-prefix, length AND kid-safety constraints
**File:** `custom-providers/openai_compat/openai_compat.py:28`
**Issue:** `_TURN_SUFFIX = build_turn_suffix(KID_MODE)` is replaced with `_TURN_SUFFIX = ""`, stripping the full HARD-CONSTRAINTS block: English-only, the mandatory single-emoji prefix contract, length/no-Markdown rules, AND the entire kid-mode topic blocklist plus jailbreak resistance. `build_turn_suffix` is now dead while still imported.
**Fix:** Revert to `_TURN_SUFFIX = build_turn_suffix(KID_MODE)`. If the suffix was leaking into TTS, fix the echo, do not delete the contract.
**Verification:** confirmed 3/3 lenses.

### [HIGH] All system messages dropped from dialogue
**File:** `custom-providers/openai_compat/openai_compat.py:108-109`
**Issue:** `if role == "system": continue` skips every system message, removing the top-level `.config.yaml` `prompt:` safety/emoji layer (one of the two load-bearing layers per CLAUDE.md). The title calls this fixing "system prompt leakage"; it deletes the system prompt.
**Fix:** Remove the block. If one system message was being spoken, filter that source specifically.
**Verification:** confirmed 3/3 lenses.

### [HIGH] Perception event relay and room_view capture disabled by commenting out _spawn calls
**File:** `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:153-156, 193`
**Issue:** Both the room_view background capture and the perception-event POST to dotty-behaviour are commented out, silently disabling the entire perception bus relay and the room_view VLM identity feature, leaving dead scaffolding. Looks like personal-fork debugging left in.
**Fix:** Revert both comment-outs. Disable a misbehaving consumer in dotty-behaviour config instead of severing the relay.
**Verification:** confirmed 2/3 lenses.

### [HIGH] sweetheart.md adds NSFW persona with hardcoded PII to a kid-safe public repo
**File:** `personas/sweetheart.md:1-23`
**Issue:** New persona instructs "flirty"/"sexually explicit stories with detail" and hardcodes the author's real profile. Contradicts `personas/default.md` (ages 4-8, no romantic/adult topics), omits the mandatory emoji-prefix contract, and uses gemma chat-template tags that aren't this repo's persona format. PII + explicit content in a public repo is a privacy and content-safety regression.
**Fix:** Do not merge; remove the file. A personal/adult persona belongs on a private fork.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] Full conversation (system+user messages) logged at info level — privacy/PII regression
**File:** `custom-providers/openai_compat/openai_compat.py:150`
**Issue:** `logger.info(f"Sending messages to LLM: {json.dumps(messages,...)}")` dumps persona, system prompt, and every user utterance to persistent docker logs on every turn — including anything a child says. Reads as leftover debugging.
**Fix:** Remove the line, or gate behind a debug flag at DEBUG level with contents redacted.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] dotty-behaviour env toggles added with no code/doc reference
**File:** `dotty-behaviour/docker-compose.yml:49-50`
**Issue:** `SOUND_TURN_YAW_DEG=20` / `WAKE_TURN_YAW_DEG=20` added with no consumer change, no docs, no indication they're honored. Dead config if unread; incomplete if read. Also muddies the PR scope.
**Fix:** Confirm the consumers read them; if so ship the consumer code + doc and split into its own PR. If not, drop the lines.
**Verification:** unverified — over cap.

### [LOW] piper emoji/non-speakable strip is the one reasonable change but is bundled and untested
**File:** `custom-providers/piper_local/piper_local.py:137-140`
**Issue:** Stripping emoji/non-Latin codepoints before Piper synth is defensible, but the regex also strips CJK with no log, has no test, and is bundled into an un-mergeable PR. An all-emoji utterance produces silence with no log.
**Fix:** If salvaged into a standalone PR: add tests for emoji-only/CJK inputs and log at debug when a chunk is fully stripped.
**Verification:** unverified — over cap.

---

## xiaozhi-server drop-in patches (http/ws/ota/admin routes)

### [HIGH] All /xiaozhi/admin/* routes are completely unauthenticated
**File:** `custom-providers/xiaozhi-patches/http_server.py:620-672` (handlers 34-589)
**Issue:** Every admin endpoint (inject-text, abort, set-head-angles, set-state, set-toggle, set-face-identified, take-photo, play-asset, songs, say, devices) is registered with no auth check, while OTA and WS paths guard themselves. Anyone reaching port 8003 can make the robot speak arbitrary text, move, change state, trigger the camera, and (play-asset) read any absolute path. inject-text is a remote prompt-injection vector into the pi agent.
**Fix:** Gate the routes behind a shared secret (Bearer / `X-Admin-Token` aiohttp middleware, 401 otherwise). At minimum confine play-asset to an allow-listed base via realpath+startswith.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] face_detected during talk state silently drops the bridge perception relay
**File:** `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:134-138`
**Issue:** The `face_detected`/`talk` branch does an early `return` that also skips the relay POST at the bottom of `handle()`. The talk-gate's intent is only to suppress re-triggering room_view capture, not forwarding to dotty-behaviour, so consumers desync for the duration of a conversation.
**Fix:** Wrap only the capture kick-off in `if cstate != 'talk':` and fall through to the relay.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] face_lost does not reset _room_description_in_flight, can wedge room_view capture
**File:** `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:92-102, 139-141`
**Issue:** `face_lost` clears the room-view caches but not `_room_description_in_flight`. If a capture task crashed without clearing the flag, every subsequent `face_detected` refuses to re-capture for the life of the connection.
**Fix:** Also set `conn._room_description_in_flight = False` in the `face_lost` branch.
**Verification:** unverified — over cap.

### [MEDIUM] WebSocket close uses removed `closed` attribute on modern websockets
**File:** `custom-providers/xiaozhi-patches/websocket_server.py:143-151`
**Issue:** The finally block checks `hasattr(websocket,'closed')` — gone in the `ServerConnection` API this code targets — so the branch is dead and the guard is largely pointless.
**Fix:** Drop the `closed` branch; rely on `getattr(websocket,'state',None)` or a try/except `await websocket.close()`.
**Verification:** unverified — over cap.

### [MEDIUM] Query-param device-id can bypass token auth via the allowlist short-circuit
**File:** `custom-providers/xiaozhi-patches/websocket_server.py:84-108`
**Issue:** Query params are injected into `request.headers` and read back by `_handle_auth`; a `?device-id=<allowed-device>` is trusted by the `allowed_devices` whitelist the same as a header, letting a client skip token verification. Headers mutation semantics are also version-fragile.
**Fix:** Treat query-param identity as untrusted — do not let a query-string device-id satisfy the allowlist bypass; use a separate dict rather than mutating `request.headers`.
**Verification:** unverified — over cap.

### [MEDIUM] OTA _is_higher_version treats pre-release/build-suffix versions as newer
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:24-43, 315`
**Issue:** `_parse_version` does `re.findall(r'\d+', ver)`, so `1.2.3-rc1 → (1,2,3,1)` compares greater than GA `(1,2,3)`, and `1.2` equals `1.2.0`. A device on GA is offered a stale rc; build-suffix versions invert ordering. Verified: `_is_higher_version('1.2.3-rc1','1.2.3')` returns True.
**Fix:** Use a real semver-ish compare (strip leading `v`, parse the numeric core, rank pre-release suffixes BELOW the release, consider only the first 3 segments). Add unit tests for rc/build cases.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] play-asset clobbers conn.client_abort / client_is_speaking, corrupting a concurrent voice turn
**File:** `custom-providers/xiaozhi-patches/http_server.py:452-453, 474`
**Issue:** `_dispatch()` unconditionally sets `conn.client_abort=False` then `conn.client_is_speaking=True`, with a finally that marks idle. play-asset is timer-driven on the "first available device", so a play-asset landing mid-turn cancels a user's barge-in and marks the device idle while chat TTS streams.
**Fix:** Refuse with 409 if the conn is mid-turn; save/restore prior flags, or route admin audio through the TTS-priority queue.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] Admin handlers call conn.websocket.send() concurrently with the chat-path writer
**File:** `custom-providers/xiaozhi-patches/http_server.py:146, 194, 242, 288, 338, 456-468`
**Issue:** Fire-and-forget `_spawn(conn.websocket.send(...))` admin sends run concurrently with xiaozhi's own send on the same `ServerConnection`. The websockets library forbids interleaved sends; a `ConcurrencyError` is swallowed by the spawned task and the chat frame can be corrupted. play-asset (hundreds of opus frames) maximizes overlap.
**Fix:** Serialize device-bound writes through a per-conn `asyncio.Lock`; at minimum log the `ConcurrencyError` and gate play-asset behind an idle check.
**Verification:** unverified — over cap.

### [MEDIUM] _dotty_say mutates conn.sentence_id from a worker thread, racing the consumer and pre-empting chat TTS
**File:** `custom-providers/xiaozhi-patches/http_server.py:547-574, 582`
**Issue:** `_enqueue()` runs on a thread and sets `conn.sentence_id` with no busy-check or lock; the greeter fires on perception events that can coincide with a live conversation, so a `/say` mid-turn overwrites `sentence_id` and the consumer drops the chat turn's remaining sentences. Also no `tts_text_queue` guard.
**Fix:** Refuse `/say` with 409 when mid-turn; guard for `tts_text_queue`; set `sentence_id` on the loop thread.
**Verification:** unverified — over cap.

### [MEDIUM] play-asset omits the tts 'start' lifecycle frame and uses an unvalidated Opus rate
**File:** `custom-providers/xiaozhi-patches/http_server.py:414, 421-423, 456-461`
**Issue:** Playback never sends `{type:tts,state:start}` (firmware-fragile), and feeds `conn.sample_rate` straight into the Opus encoder without checking it is in `{8000,12000,16000,24000,48000}`; a non-legal rate raises, is swallowed by the broad except, and the endpoint still returns 200.
**Fix:** Emit the full lifecycle; validate/resample the rate; propagate decode failure as 500.
**Verification:** unverified — over cap.

### [MEDIUM] EventTextMessageHandler relays perception events to a stale BRIDGE_URL
**File:** `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:67-78, 178`
**Issue:** Events POST to `{BRIDGE_URL or VISION_BRIDGE_URL}/api/perception/event`, but the bus moved to dotty-behaviour (:8090) in #115. If the deploy sets only a behaviour URL var, or BRIDGE_URL points at the dashboard container (which no longer serves that route), every event 404s and is dropped — silently disabling face_greeter/sound_turner/state gating.
**Fix:** Rename/select the var to point at dotty-behaviour explicitly and update the warning text; verify the deploy compose sets the var the handler reads.
**Verification:** unverified — over cap.

### [MEDIUM] state_changed/face_detected ordering can corrupt the room_view gate
**File:** `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:109-115, 133-138`
**Issue:** The room_view gate reads `conn.current_state`, set from `state_changed`. If the firmware emits `face_detected` before the IDLE→TALK `state_changed` it acted on, the gate sees `idle` and fires a capture for a mid-talk flicker — the "stacked Hi NAME" failure the comment claims to prevent.
**Fix:** Gate on a positive "room_view already captured this session" flag rather than `current_state`, or have firmware stamp `face_detected` with its state.
**Verification:** unverified — over cap.

### [LOW] OTAHandler.handle_post finally-block can swallow a secondary exception
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:359-361`
**Issue:** Unlike `handle_download`, `handle_post`'s finally calls `_add_cors_headers` with no try/except and a bare `return` that masks a propagating exception; an edge case 500s the device with non-JSON.
**Fix:** Wrap `_add_cors_headers` in try/except; initialize `response = None` and short-circuit.
**Verification:** unverified — over cap.

### [LOW] _dotty_inject_text reads conn.headers without the 'or {}' guard used elsewhere
**File:** `custom-providers/xiaozhi-patches/http_server.py:66`
**Issue:** Line 66 lacks the `(getattr(conn,'headers',{}) or {})` guard every sibling handler uses; if `conn.headers` is None during teardown, inject-text raises after already dispatching chat, returning a misleading 500.
**Fix:** Apply the same guard on line 66.
**Verification:** unverified — over cap.

### [LOW] OTA mqtt branch ships an empty-password block with no websocket fallback
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:260-279`
**Issue:** When mqtt_gateway is set but the signature key is missing/fails, the handler sends an mqtt block with `password:""` and (being exclusive with the websocket else) no fallback — the device gets a config it cannot authenticate with.
**Fix:** Fall back to the websocket block (or return an error) when the signature is unavailable.
**Verification:** unverified — over cap.

### [LOW] OTA model-name matching is brittle — updates silently never offered
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:84-90, 200-201, 304`
**Issue:** `files_by_model.get(device_model, [])` requires an exact match between the firmware-reported model and the operator's bin-filename prefix, which rarely align; the device is silently told it's up-to-date and the same log fires for both "unknown model" and "already latest".
**Fix:** Log the parsed model + known keys when candidates is empty; allow a configured fallback bucket or case-insensitive match; distinct log lines for the two cases.
**Verification:** unverified — over cap.

### [LOW] OTA timezone_offset / firmware_cache_ttl assume numeric YAML
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:62, 226`
**Issue:** `timezone_offset * 60` on a quoted `"8"` yields a 60-copy string in the OTA payload; a non-int ttl makes `int(...)` raise.
**Fix:** Coerce with `int(...)` and validate `firmware_cache_ttl` at construction.
**Verification:** unverified — over cap.

### [LOW] Firmware bin cache re-scans disk on every OTA POST until a valid bin exists
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:66-72`
**Issue:** The TTL short-circuit requires a non-empty `files_by_model`; with no conforming bins yet that's falsy, so the glob+regex scan runs on every poll and `updated_at` is meaningless.
**Fix:** Track freshness with a separate `_scanned` flag/timestamp independent of emptiness.
**Verification:** unverified — over cap.

### [LOW] Admin MCP handlers fire-and-forget send() with no guard for a torn-down socket
**File:** `custom-providers/xiaozhi-patches/http_server.py:146, 194, 242, 288, 338`
**Issue:** Between the registry lookup and the spawned send, the WS can close; `send()` on a dead socket raises in the spawned task while the HTTP caller already got `{"ok":true}`.
**Fix:** Check `conn.websocket` state in a small coroutine and catch+log; or await the send before returning.
**Verification:** unverified — over cap.

### [LOW] set-toggle/set-state have no response correlation — device-side failures invisible; head-angles unclamped
**File:** `custom-providers/xiaozhi-patches/http_server.py:143, 191, 239, 285, 335`
**Issue:** MCP calls use a truncated wall-clock id and never match the device's jsonrpc reply, so a device-side rejection still returns `{"ok":true}`. set-head-angles forwards arbitrary ints (no clamp).
**Fix:** Clamp head-angle yaw/pitch/speed to firmware-legal ranges; optionally correlate by request id with a timeout, or document best-effort.
**Verification:** unverified — over cap.

### [LOW] play-asset format inference treats bare .opus as Ogg
**File:** `custom-providers/xiaozhi-patches/http_server.py:406-411`
**Issue:** `.opus → format=ogg` fails on raw Opus; unmapped extensions yield `fmt=None` (autodetect) with no distinct log; the songs picker advertises `.opus` so it can silently fail behind an already-sent 200.
**Fix:** Let ffmpeg autodetect for `.opus`; on decode failure return a distinguishable error rather than a swallowed warning.
**Verification:** unverified — over cap.

### [LOW] OTA mqtt allowlist token logic undocumented coupling
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:283-295`
**Issue:** A whitelisted device gets an empty token and relies on the WS-side allowlist bypass; remove it from the whitelist later and it has no token to fall back on.
**Fix:** Document the coupling, or always issue a token regardless of whitelist membership.
**Verification:** unverified — over cap.

### [LOW] play-asset resample can feed huge factors to resample_poly; no sample_rate guard
**File:** `custom-providers/xiaozhi-patches/http_server.py:415-420`
**Issue:** Coprime rates give large up/down factors (slow/memory-heavy filter); a 0/None `conn.sample_rate` blows up inside `_decode` and is masked by the broad except.
**Fix:** Validate `conn.sample_rate > 0`; log src/tgt rates on decode failure.
**Verification:** unverified — over cap.

### [LOW] Admin routes resolve target via next(iter(...)) — nondeterministic device with multiple StackChans
**File:** `custom-providers/xiaozhi-patches/http_server.py:55, 87, 123, 171, 218, 266, 316, 388, 523`
**Issue:** With >1 device and no `device_id`, handlers act on an arbitrary dict entry; abort/set-state can hit the wrong robot.
**Fix:** Require explicit `device_id` (409/400) when multiple devices are connected.
**Verification:** unverified — over cap.

### [LOW] EventTextMessageHandler attributes events to "unknown" device on header miss
**File:** `custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py:60-65`
**Issue:** A bare try/except leaves `device_id="unknown"` if `conn.headers` is None, so dotty-behaviour accumulates per-device state under a bogus id that never matches the real device — consumers silently never fire.
**Fix:** Resolve device_id like the WS registration does; drop the event with a warning rather than relay under a fabricated id.
**Verification:** unverified — over cap.

### [QUALITY/LOW] Massive duplication + ms-truncated ids across the MCP admin handlers
**File:** `custom-providers/xiaozhi-patches/http_server.py:101-343`
**Issue:** Five near-identical handlers repeat parse/lookup/envelope/spawn (~250 lines), each using `id = int(time.time()*1000) % 0x7FFFFFFF` which collides within a millisecond.
**Fix:** Extract a `_mcp_call(...)` helper; use a monotonic counter / uuid-derived id.
**Verification:** unverified — over cap.

### [INFO] play-asset path containment (bridge-callee view)
**File:** `custom-providers/xiaozhi-patches/http_server.py:376-384`
**Issue:** The bridge's own play-song basename-guards, but the xiaozhi callee validates only `os.path.exists`, so the trust boundary is weaker than the bridge layer implies (duplicate of G10, flagged from the bridge scope).
**Fix:** Resolve realpath under the songs base in play-asset.
**Verification:** unverified — over cap.

### [INFO] _http_response param name does not match the websockets process_request contract
**File:** `custom-providers/xiaozhi-patches/websocket_server.py:157-164`
**Issue:** The param is really a `Request` (accessed as `request_headers.headers`), which misleads readers.
**Fix:** Rename to `request`, access `request.headers`.
**Verification:** unverified — over cap.

---

## Bridge dashboard (FastAPI :8081)

### [MEDIUM] Dashboard action endpoints always block 8s and report "no reply" — the event producer is dead
**File:** `bridge.py:613-626` (consumed at `bridge/dashboard.py:658-704, 2091-2133`)
**Issue:** `_inject_or_error` subscribes to the bridge event bus and waits 8s for Dotty's reply, but nothing ever enqueues onto `_dashboard_event_listeners` — the producer moved to dotty-pi in #36 and was never re-wired. Every Say/Dance/Mood/Story click blocks the full 8s then renders "Sent — no reply in 8s."; the SSE stream only emits heartbeats.
**Fix:** Either wire a real producer (dotty-pi/xiaozhi POSTs completed turns to a bridge endpoint that calls `put_nowait`), or drop the subscribe/wait and return "Sent" immediately.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] Blocking synchronous HTTP inside async dashboard routes stalls the event loop
**File:** `bridge.py:471-504` (`_dotty_behaviour_get`); `bridge/dashboard.py:193-218` (`_fetch_robot_photo`), 1366, 2085, 801, 1925
**Issue:** The dotty-behaviour getters and `_fetch_robot_photo` do synchronous `requests.get` with no `to_thread`, called from async routes; each uncached call blocks the whole asyncio loop for 1.5-2.0s, freezing all clients. The HTMX 10s poll fans each render into 3-6 such calls. The codebase already uses `to_thread` for device-count/songs, so these are clear omissions. `_build_perception_card_ctx`'s "no I/O" docstring is now false.
**Fix:** Wrap blocking HTTP in `asyncio.to_thread` (or use `httpx.AsyncClient`); fix the stale docstring.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] CSRF middleware blocks the localhost /admin/* operator endpoints
**File:** `bridge/csrf.py:33, 69-70, 83`; `bridge.py:800-924`
**Issue:** CSRFMiddleware is app-wide and exempts only `/api/`, `/metrics`, `/health`. The `/admin/*` router (documented as the localhost CLI back-channel) is not exempt and has no CSRF cookie, so curl/script calls are 403'd before reaching `_admin_require_localhost` — the entire documented admin surface is unusable via its intended caller.
**Fix:** Add `/admin/` to `_EXEMPT_PREFIXES` (the router already has its own localhost auth).
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] SSE /events served under BaseHTTPMiddleware (CSRF) — Starlette buffering hazard
**File:** `bridge/dashboard.py:2091-2133`; `bridge/csrf.py:73-98`
**Issue:** CSRFMiddleware subclasses `BaseHTTPMiddleware`, a documented SSE footgun that buffers chunks and breaks `is_disconnected()`, defeating `X-Accel-Buffering: no`. Today only heartbeats flow, but the channel is structurally degraded once a producer is wired back.
**Fix:** Exempt the SSE path, or migrate CSRFMiddleware to raw ASGI so streaming responses pass through.
**Verification:** unverified — over cap.

### [MEDIUM] Kid/smart-mode toggles report ok:False while having already persisted+applied the flip
**File:** `bridge.py:630-643, 695-707`
**Issue:** `_dashboard_set_kid_mode` persists + applies the state first, then dispatches the firmware toggle; if the device doesn't ack, it returns `{ok:False}` even though the bridge already flipped. The UI shows "toggle failed", the operator re-toggles, and the state ping-pongs out of sync.
**Fix:** Return `ok:True` with a `device_pushed:false` warning when the bridge write succeeded but the LED push failed (mirror the `/admin/kid-mode` shape).
**Verification:** unverified — over cap.

### [MEDIUM] network_mode: host binds dashboard + /metrics to 0.0.0.0 with auth unset by default
**File:** `bridge/docker-compose.yml:31, 41-60`; `bridge.py:936`; `bridge/dashboard.py:259-267`
**Issue:** The shipped compose sets no dashboard user/pass/CSRF secret; auth is opt-in and returns immediately when either env var is empty; host networking exposes the full mutation surface and `/metrics` to the whole LAN unauthenticated.
**Fix:** Ship the compose with dashboard user/pass + a persistent CSRF secret from `.env`, and/or bind `127.0.0.1` by default; at minimum document the required auth env.
**Verification:** unverified — over cap.

### [MEDIUM] Async dashboard routes read + JSON-parse the entire daily convo log synchronously
**File:** `bridge/dashboard.py:377-398, 472-514, 517-555, 1631-1648`
**Issue:** `_stackchan_last_seen`, `alerts_count`, `alerts_detail` read the whole `convo-*.ndjson` and `json.loads` every line with no `to_thread`, from async routes polled every ~10s per tab; a multi-MB log blocks the loop on each poll.
**Fix:** Wrap the read+parse in `asyncio.to_thread`; optionally tail rather than parse the whole day.
**Verification:** unverified — over cap.

### [MEDIUM] Dashboard /actions/say and /actions/start-story bypass the kid-mode content filter
**File:** `bridge/dashboard.py:706-729, 732-773`
**Issue:** `say()`/`start_story()` sanitise control chars and length but never call `content_filter()` (imported and available), so an operator (or any LAN client when auth is unset) can make Dotty speak arbitrary unfiltered text, and these turns never populate the safety ring.
**Fix:** Run the sanitised text through `content_filter()` when kid-mode is on; record the hit.
**Verification:** unverified — over cap.

### [QUALITY/MEDIUM] security_watch.py is entirely dead code; only get_recent_cycles() is reachable
**File:** `bridge/security_watch.py:1-630`
**Issue:** Nothing calls `run_security_consumer`, `start_device_timer`, etc.; the perception role moved to dotty-behaviour in #36. ~580 lines run nowhere; `RECENT_CYCLES` is always empty; the docstring still describes firmware tools that "do not exist as of 2026-04-27".
**Fix:** Delete the module + the `_build_security_panel_ctx` route, or repoint the panel at a dotty-behaviour HTTP getter.
**Verification:** unverified — over cap.

### [LOW] host/footer 'disk' vitals read the container overlay fs, not the host disk
**File:** `bridge/dashboard.py:1614-1620, 1734-1739, 1793-1796`
**Issue:** `shutil.disk_usage('/')` inside the container reports the overlay fs, not the Unraid appdata volume, so the disk gauge is misleading (mem/cpu are host-wide via the shared kernel).
**Fix:** Point at a bind-mounted host path, or relabel as "container disk".
**Verification:** unverified — over cap.

### [LOW] say/start-story length cap enforced before whitespace collapse
**File:** `bridge/dashboard.py:714-723, 746-753`
**Issue:** The `len > 500` check runs before whitespace collapse, so a pasted message with long whitespace runs but short visible content is wrongly rejected.
**Fix:** Sanitise/collapse first, then length-check the cleaned text.
**Verification:** unverified — over cap.

### [LOW] play-asset / vision-photo proxy interpolate unencoded device_id into an internal URL
**File:** `bridge/dashboard.py:201, 638, 1924`
**Issue:** `_fetch_robot_photo` builds the behaviour URL with the raw `device_id` path-param (unvalidated, unencoded), allowing query/fragment injection into the internal request.
**Fix:** `urllib.parse.quote(device_id, safe='')` and validate against an id charset, returning 400 on mismatch.
**Verification:** unverified — over cap.

### [LOW] _dotty_behaviour_get caches the failure fallback for the full TTL
**File:** `bridge.py:492-504`
**Issue:** On any error it writes the empty fallback into the cache for the TTL, so every tile blanks for ~2s after each blip even after recovery.
**Fix:** Cache only on success; return `fallback` on the error path without caching.
**Verification:** unverified — over cap.

### [LOW] bridge writes to brain.db (RW) primarily owned by dotty-pi — approve/redact can lock/race
**File:** `bridge.py:316, 349`
**Issue:** `_voice_memory_approve/_delete_blocking` open brain.db RW across containers; in non-WAL delete mode a writer blocks readers, and failures are swallowed and surface as a generic "not found".
**Fix:** Set `PRAGMA busy_timeout`, surface lock failures distinctly; longer term route approve/redact through dotty-pi (the canonical writer).
**Verification:** unverified — over cap.

### [LOW] host_detail 'server' modal hardcodes 'Recent errors today: —' despite the count being computed elsewhere
**File:** `bridge/dashboard.py:1832`
**Issue:** `alerts_count()` already tallies today's errored turns from the convo log; the modal shows a permanent dash.
**Fix:** Factor the tally into a helper and reuse it.
**Verification:** unverified — over cap.

### [LOW] security_watch defaults use retired RPi/zeroclaw paths and wrong port
**File:** `bridge/security_watch.py:85-101`
**Issue:** `SECURITY_LOG_DIR` defaults `CONVO_LOG_DIR` to `/root/zeroclaw-bridge/logs` (disagreeing with dashboard.py's default) and `_BRIDGE_INTERNAL_URL` defaults to port 8080 while the bridge listens on 8081.
**Fix:** Align the default with dashboard.py; fix the port to 8081 — or delete the dead module.
**Verification:** confirmed 2/3 lenses.

### [LOW] Voice-tools inventory shows 5 tools but dotty-pi-ext ships 7
**File:** `bridge/dashboard.py:965-981`
**Issue:** `_VOICE_TOOLS` hardcodes 5 and the docstring says "same five values"; recall_person/remember_person (added in #53) are missing.
**Fix:** Add the two person-memory tools; source from a shared manifest.
**Verification:** unverified — over cap.

### [LOW] Host-detail 'bridge' modal hardcodes Device: Raspberry Pi
**File:** `bridge/dashboard.py:1773`
**Issue:** The bridge now runs as the `dotty-bridge` Docker container on Unraid; the modal reports stale hardware provenance.
**Fix:** Relabel as "Docker container (Unraid host)" or drop the row.
**Verification:** unverified — over cap.

### [LOW] CSRF warning + /admin/safety self-rewrite reference retired zeroclaw paths
**File:** `bridge/csrf.py:43`; `bridge.py:872-921`
**Issue:** The CSRF warning points operators at `/etc/default/zeroclaw-bridge` (no effect in a container; CSRF cookies invalidate on every restart). `_admin_safety` self-edits bridge.py on disk — changes nothing live (the consumer was in ZeroClaw) and is wiped on the next image rebuild.
**Fix:** Update the CSRF message to the container env mechanism; remove the `/admin/safety` self-rewrite or move the allowlist to a mounted state file.
**Verification:** unverified — over cap (both).

### [QUALITY/LOW–INFO] Dead/retired metrics + Tier1Slim narrative
**File:** `bridge/metrics.py:100-160` (128 = `dotty_active_acp_sessions`); `bridge/dashboard.py:847-872, 1113-1126, 1438-1480`
**Issue:** Only `dotty_kid_mode_active` and `dotty_content_filter_hits_total` are ever observed; the rest (incl. the ACP gauge) always export 0. The logger name is `zeroclaw-bridge.metrics`. Smart-mode code carries large dead Tier1Slim/zeroclaw commentary for a card that does nothing.
**Fix:** Remove unproduced metrics (esp. the ACP gauge), rename the logger, collapse the smart-mode card to its real behaviour.
**Verification:** unverified — over cap.

### [INFO] Dashboard live perception feed (EventSource '/api/perception/feed') 404s — endpoint ripped in #111, consumer left behind
**File:** `bridge/templates/dashboard.html:564`
**Issue:** The page (served from :8081) opens `EventSource('/api/perception/feed')`, but the bridge serves no `/api/*` routes — commit c6df5c5 (#111) deleted the endpoint and left the EventSource. The live feed never streams, every dotty-refresh nudge is dead, and the browser hammers a 404 reconnect every few seconds per open tab. The real SSE lives on dotty-behaviour:8090 (different origin).
**Fix:** Add a bridge SSE passthrough under `/api/`, or point the EventSource at the dotty-behaviour origin with CORS configured.
**Verification:** confirmed 2/3 lenses.

---

## Voice LLM providers (PiVoiceLLM, OpenAICompat)

### [MEDIUM] Turn timeout is a fixed wall-clock deadline that ignores streaming progress
**File:** `custom-providers/pi_voice/pi_client.py:220-269`
**Issue:** `deadline` is set once (default 120s) and never extended while text streams. A healthy long reply (think_hard escalation, 6-sentence story) is killed mid-stream and the user hears the "(brain offline)" fallback. The timeout should bound silence, not total length.
**Fix:** Reset `deadline` on each `text_delta` so the cap only fires when pi goes quiet.
**Verification:** unverified — over cap.

### [MEDIUM] new_session failure proceeds with un-reset pi session, leaking prior-turn context
**File:** `custom-providers/pi_voice/pi_voice.py:136-141`
**Issue:** On `PiClientError` from `new_session()`, the code logs and continues and sets `_first_turn = False` unconditionally, so the next turn runs against pi's non-reset session — carrying prior content (or a partially-absorbed jailbreak) into the next, possibly child, interaction.
**Fix:** Force a hard reset (close + respawn) on `new_session` failure, or surface the failure rather than treating a failed reset as a fresh turn.
**Verification:** unverified — over cap.

### [MEDIUM] _closed flag never reset after close() + reuse, suppressing reader-thread crash logging
**File:** `custom-providers/pi_voice/pi_client.py:171-182, 311-313, 352-354`
**Issue:** `close()` sets `_closed=True`; `_ensure_started()` respawns without clearing it, so the reader threads' `if not self._closed:` guards swallow genuine crashes — turns time out with no diagnostic.
**Fix:** Reset `_closed=False` on respawn; scope the suppression to the process generation.
**Verification:** unverified — over cap.

### [MEDIUM] Abandoned iter_turn_text leaves pi streaming; a stale agent_end can terminate the next turn
**File:** `custom-providers/pi_voice/pi_client.py:211-269` (also 188-209)
**Issue:** On barge-in/TTS-abort the generator is GC'd without consuming `agent_end`; pi keeps generating and queues frames. `agent_end` is not id-matched, so an abandoned turn's trailing `agent_end` can terminate the NEXT `iter_turn_text`, truncating the next reply to empty/partial.
**Fix:** Send an explicit cancel to pi on abandon (try/finally), drain prior frames by req_id, and id-match `agent_end`.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] OpenAICompat can emit two emojis when a valid emoji is not at the start
**File:** `custom-providers/openai_compat/openai_compat.py:218-229`
**Issue:** Enforcement only checks `so_far.startswith(emoji)`. For "Well, 😊 hi there" the check fails, so it prepends `😐` AND yields content still containing `😊` — two emojis, violating the single-emoji HARD CONSTRAINT; firmware face keys on the first, the second garbles TTS.
**Fix:** Strip embedded emojis from the body before yielding, or buffer until the first non-whitespace char and filter subsequent emojis.
**Verification:** unverified — over cap.

### [MEDIUM] OpenAICompat speaks chain-of-thought when pointed at a reasoning model
**File:** `custom-providers/openai_compat/openai_compat.py:189-229`
**Issue:** Unlike PiClient, OpenAICompat has no thinking filter — it yields `delta.content` verbatim. Inline `<think>...</think>` is spoken aloud and the leading `<` defeats the emoji check; separate `reasoning_content` is dropped but the silent gap can trip the turn timeout.
**Fix:** Strip `<think>...</think>` (stateful across chunks), ignore `reasoning_content`, or send a param to disable thinking.
**Verification:** unverified — over cap.

### [MEDIUM] OpenAICompat appends the safety/format suffix to the wrong message when no user turn exists
**File:** `custom-providers/openai_compat/openai_compat.py:101-114`
**Issue:** `_TURN_SUFFIX` is appended only at `last_user_idx`; if the final turn is system/assistant/tool (greeter injections), `last_user_idx` is None and the suffix — carrying emoji rule + kid-mode filter — is never injected, so the model runs unconstrained for that turn.
**Fix:** When `last_user_idx` is None, append the suffix to a synthesized trailing user message or the last message regardless of role.
**Verification:** unverified — over cap.

### [MEDIUM] OpenAICompat yields whitespace-only content chunks to TTS before emoji enforcement
**File:** `custom-providers/openai_compat/openai_compat.py:216-229`
**Issue:** Whitespace-only deltas are yielded before `so_far` is non-empty and the emoji check runs; the first thing TTS/firmware sees is whitespace, not the emoji the contract promises. If the stream is all whitespace, the fallback fires after whitespace already went out.
**Fix:** Buffer until `so_far` is non-empty before yielding anything.
**Verification:** unverified — over cap (duplicate-class with F139).

### [LOW] Documented extra_pi_flags config key is silently ignored
**File:** `custom-providers/pi_voice/pi_voice.py:29-30, 107-117`
**Issue:** The docstring documents `extra_pi_flags`, but `__init__` reads only `container_name`; `make_default_pi_client()` consults only the `DOTTY_PI_EXTRA_FLAGS` env var. A user setting `extra_pi_flags` in `.config.yaml` gets no effect.
**Fix:** Read `config.get('extra_pi_flags')` and thread it in, or remove it from the docstring and document the env var as the only mechanism.
**Verification:** confirmed 2/3 lenses.

### [LOW] make_default_pi_client ignores the resolved container_name
**File:** `custom-providers/pi_voice/pi_voice.py:107-117`
**Issue:** The provider resolves and logs `container_name`, but `make_default_pi_client()` re-reads `DOTTY_PI_CONTAINER` itself and ignores it — the configured container name is logged but inert in production.
**Fix:** Pass `self._container` into `make_default_pi_client`.
**Verification:** unverified — over cap.

### [LOW] PiVoiceLLM fallback strings violate the mandatory emoji-prefix protocol
**File:** `custom-providers/pi_voice/pi_voice.py:130, 150`
**Issue:** `yield "(empty turn)"` and `yield "(brain offline ...)"` begin with `(`; the firmware emotion parser sees `(` and produces no/garbage face. OpenAICompat correctly prefixes its fallbacks.
**Fix:** Prepend an allowed emoji (e.g. `😐`) to both fallbacks.
**Verification:** confirmed 2/3 lenses.

### [LOW] new_session response match does not verify request id
**File:** `custom-providers/pi_voice/pi_client.py:192-209`
**Issue:** The drain loop matches only `type==response, command==new_session`, not `id==req_id`; a stale `new_session` response can be accepted early.
**Fix:** Add `and frame.get('id') == req_id`.
**Verification:** unverified — over cap.

### [LOW] PiVoiceLLM _first_turn desyncs from the pi process after a respawn
**File:** `custom-providers/pi_voice/pi_voice.py:136-141`
**Issue:** If pi dies after turn 1, turn 2 runs `new_session()` against a freshly respawned process that has no session — a spurious 10s `new_session timed out` is swallowed, adding dead air.
**Fix:** Have PiClient signal a fresh spawn so the first-turn-after-respawn skips `new_session()`.
**Verification:** unverified — over cap.

### [LOW] iter_turn_text process-exit detection only runs on queue-empty
**File:** `custom-providers/pi_voice/pi_client.py:222-269`
**Issue:** A crash with frames still queued never hits the Empty branch, so it spins to the 120s turn timeout and reports "turn timed out" instead of "pi process exited", delaying the fallback by up to 2 minutes.
**Fix:** Check `self._proc.poll()` on every iteration.
**Verification:** unverified — over cap.

### [LOW] _next_id increments shared counter without the lock; reader-thread frame routing races a respawn
**File:** `custom-providers/pi_voice/pi_client.py:283-285, 149-169`
**Issue:** A respawn swaps `_event_queue` under lock while the old daemon reader (never joined) can still `put` stale frames into the new queue, injecting a previous process's frames into the new turn.
**Fix:** Join/stop prior reader threads before swapping the queue; bind each reader to its specific queue/proc; lock the id mutation.
**Verification:** unverified — over cap.

### [LOW] _completions_url mishandles a base ending in /chat/completions
**File:** `custom-providers/openai_compat/openai_compat.py:125-132`
**Issue:** The endsWith heuristic can double the path (e.g. a gateway route `/chat/completions/stream`).
**Fix:** Make the endpoint construction explicit via a config flag, or document url must be the base.
**Verification:** unverified — over cap.

### [LOW] OpenAICompat yields leading whitespace before emoji-prefix engages
**File:** `custom-providers/openai_compat/openai_compat.py:218-229`
**Issue:** A whitespace-only first chunk is yielded before enforcement, so the first TTS chunk isn't the emoji.
**Fix:** Buffer until `so_far` is non-empty; strip leading whitespace.
**Verification:** unverified — over cap.

### [QUALITY/LOW–INFO] Dead/unreachable helpers + unsynchronized stderr ring
**File:** `custom-providers/openai_compat/openai_compat.py:134-140` (`_chunk_sentences`); `custom-providers/pi_voice/pi_client.py:63-81` (`default_subprocess_factory`), 275-277/343-354 (stderr ring)
**Issue:** `_chunk_sentences` + its `_SENTENCE_BOUNDARY` import are dead; the ssh `default_subprocess_factory` is unreachable in production; the stderr ring is mutated/snapshotted across threads without the lock the rest of the class uses.
**Fix:** Remove dead code or wire it; use a `deque(maxlen=...)` and/or guard with `self._lock`.
**Verification:** unverified — over cap.

---

## ASR/TTS providers (FunASR, Whisper, EdgeStream, Piper)

### [HIGH] Mid-utterance synth failure emits SentenceType.LAST, truncating the rest of the reply
**File:** `custom-providers/edge_stream/edge_stream.py:139-143` (mirror: `piper_local.py:187-191`)
**Issue:** The except handler unconditionally pushes `(SentenceType.LAST, [], None)` regardless of `is_last`. A multi-segment reply where an early segment's synth throws tells xiaozhi/firmware the whole response is finished, so every remaining segment is dropped and the device stops mid-sentence.
**Fix:** Only emit LAST from the failure path when `is_last` is True; otherwise log and continue.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] processed_chars over-count in edge_stream and piper_local
**File:** `custom-providers/edge_stream/edge_stream.py:67-74`; `custom-providers/piper_local/piper_local.py:114-121`
**Issue:** `remaining_text = full_text[self.processed_chars:]` (absolute index) then `self.processed_chars += len(full_text)` over-counts; post-condition should be `== len(full_text)`. Masked today by the FIRST reset, but a re-entered LAST drops the final sentence's audio.
**Fix:** `self.processed_chars = len(full_text)` (or `+= len(remaining_text)`) in both; consider a shared mixin.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] fun_local crashes in __init__ when output_dir is unset
**File:** `custom-providers/asr/fun_local.py:51-58`
**Issue:** `os.makedirs(None)` raises TypeError when `output_dir` is absent; whisper_local guards this, fun_local does not, so the two ASR providers are inconsistent.
**Fix:** `if self.output_dir: os.makedirs(self.output_dir, exist_ok=True)`.
**Verification:** unverified — over cap.

### [MEDIUM] ASR model invoked from worker threads with no lock — not thread-safe under concurrent transcription
**File:** `custom-providers/asr/whisper_local.py:107-109, 161-173` (mirror: `fun_local.py:81-88`)
**Issue:** The singleton model is called via `asyncio.to_thread` from separate threads for overlapping sessions; faster-whisper/FunASR aren't safe for concurrent calls on the same model, risking corrupted output or a segfault.
**Fix:** Serialize inference with a per-instance `threading.Lock` inside the blocking call.
**Verification:** unverified — over cap.

### [MEDIUM] before_stop play files dropped when the final segment's synthesis fails
**File:** `custom-providers/edge_stream/edge_stream.py:139-143` (mirror piper 187-191)
**Issue:** The except handler never calls `_process_before_stop_play_files()`, so a LAST-segment synth failure abandons queued trailing assets (and can leak them into the next utterance).
**Fix:** On the except path, when `is_last`, still process+clear the before_stop play files.
**Verification:** unverified — over cap.

### [MEDIUM] SentenceType.FIRST emitted once per segment instead of once per reply
**File:** `custom-providers/edge_stream/edge_stream.py:101` (mirror `piper_local.py:146`)
**Issue:** `text_to_speak` puts FIRST at the top of every call; a multi-segment reply pushes multiple FIRSTs, which downstream treats as start-of-response and can re-trigger talk animation mid-sentence.
**Fix:** Track a per-reply first-segment flag; emit FIRST only for the first segment.
**Verification:** unverified — over cap.

### [LOW] Whisper odd-length PCM buffer raises ValueError instead of degrading
**File:** `custom-providers/asr/whisper_local.py:104`
**Issue:** `np.frombuffer(..., int16)` raises on an odd byte count (truncated final frame); caught by the broad except → empty transcript with no retry.
**Fix:** Trim to an even length before `frombuffer`.
**Verification:** unverified — over cap.

### [LOW] get_emotion matches the first mapped emoji anywhere, not the required leading emoji; default 🙂 outside the set
**File:** `custom-providers/textUtils.py:146-152`
**Issue:** The protocol requires the FIRST char to be the emotion emoji, but `get_emotion` breaks on the first mapped emoji anywhere, selecting a wrong/secondary face; the default `🙂` is outside `ALLOWED_EMOJIS`/the firmware set.
**Fix:** Resolve emotion from the leading grapheme only; default to `😐` (the in-set neutral).
**Verification:** unverified — over cap.

### [LOW] piper effective_rate==24000 shortcut skips resampling when native rate isn't 24000
**File:** `custom-providers/piper_local/piper_local.py:67-74`
**Issue:** The no-resample shortcut conflates "no pitch shift" with "no rate conversion"; a 22050 Hz voice with a pitch_scale that lands on 24000 emits raw native-rate PCM into a 24000 Hz encoder, corrupting playback speed/pitch.
**Fix:** Gate the shortcut on `src_rate == 24000 AND pitch_scale == 1.0`; otherwise always go through the gcd/resample branch.
**Verification:** unverified — over cap.

### [LOW] piper length_scale not validated for <= 0 (unlike pitch_scale)
**File:** `custom-providers/piper_local/piper_local.py:32-42`
**Issue:** `pitch_scale` raises on `<= 0` but `length_scale` (Piper's speed knob) is unchecked; a 0/negative value yields empty/garbage audio with no early error.
**Fix:** Mirror the pitch_scale guard for length_scale.
**Verification:** unverified — over cap.

### [LOW] whisper metrics: lang_prob can be None with forced language, raising in the f-string
**File:** `custom-providers/asr/whisper_local.py:132-137`
**Issue:** With a forced language, `language_probability` can be present-but-None; the `getattr(..., 0.0)` default doesn't fire and `{lang_prob:.3f}` raises, swallowed by the except — so the #105 ASR-METRICS line is silently dropped on every such utterance.
**Fix:** `lang_prob = getattr(...) or 0.0`.
**Verification:** unverified — over cap.

### [LOW] FIRST audio-start marker emitted even when synth yields zero audio
**File:** `custom-providers/edge_stream/edge_stream.py:101-115` (mirror piper 146-159)
**Issue:** A mid-reply empty-synth segment emits FIRST then returns with no frames and no LAST, wedging per-sentence marker accounting.
**Fix:** Emit FIRST only after confirming there is audio, or enqueue a terminating marker on the empty-synth early return.
**Verification:** unverified — over cap.

### [QUALITY/LOW–INFO] delete_audio_file unused; end_of_stream always True on non-last segments
**File:** `custom-providers/asr/fun_local.py:55` (and `whisper_local.py:46`); `custom-providers/edge_stream/edge_stream.py:121-137` (mirror piper 176-182)
**Issue:** `delete_audio_file` is stored but never honored (utterance WAVs accumulate); the trailing partial frame is flushed with `end_of_stream=True` on every segment, not just the last.
**Fix:** Honor or remove `delete_audio_file`; pass `end_of_stream=is_last`.
**Verification:** unverified — over cap.

---

## dotty-behaviour core (app, routes, perception, dispatch, config)

### [HIGH] room_view roster recognition silently fails when person id != display_name
**File:** `dotty-behaviour/vision/room_view.py:115-124, 159-167`
**Issue:** `name_choices` is built from `p.display_name`, but `parse_room_view_response` validates against `roster_ids` (which are `p.id` from `roster_ids_with_appearance()`). Whenever `id != display_name.lower()` (the common case), a correct VLM identification falls through to None, the synthetic `face_recognized` never fires, and named greetings regress to bare/no greeting. Masked because the test fake reimplements the helper as `display_name.lower()` with id==display_name fixtures.
**Fix:** Make the prompt vocabulary and the validation set use the same key (build `name_choices` from `p.id`, or resolve the returned display-name back to the id). Fix the test fake to return `p.id` and add an id != display_name fixture.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] Consumer task crashes are silently swallowed (no log, no restart)
**File:** `dotty-behaviour/main.py:325-341`
**Issue:** Consumers are launched with `create_task` and only awaited at shutdown via `gather(..., return_exceptions=True)`; a runtime exception is captured and discarded at shutdown, never logged when it happens, and the consumer is never restarted while the daemon still reports "ready".
**Fix:** Attach a done-callback that logs non-cancelled exceptions; optionally supervise/restart each consumer.
**Verification:** unverified — over cap.

### [MEDIUM] No upload size limit on /api/vision/explain and /api/audio/explain (LAN OOM)
**File:** `dotty-behaviour/routes/vision.py:145` (and `audio.py:87`)
**Issue:** `await file.read()` reads the entire multipart upload into memory then base64-encodes (~1.33x) with no Content-Length cap; both endpoints bind `0.0.0.0:8090`. A single large/malicious POST can exhaust container memory and take down the daemon (and all consumers). The JPEG is also retained per-device in `vision_cache`.
**Fix:** Enforce a max upload size (e.g. 5 MB JPEG / 10 MB audio) up front and reject >N with 413.
**Verification:** unverified — over cap.

### [MEDIUM] vision_latest lost-wakeup race produces spurious 404s
**File:** `dotty-behaviour/routes/vision.py:331-352`
**Issue:** `vision_latest` pops the cache, registers a waiter, then awaits the Event. If `explain`'s `signal_vision_waiters()` fires between pop and registration, the signal is lost (signal only set()s already-registered Events), so the waiter blocks to the 15s timeout and 404s even though a valid description exists.
**Fix:** Make the wakeup level-triggered: re-check the cache after registering before awaiting, or use a generation counter. Same for the audio waiter.
**Verification:** unverified — over cap.

### [MEDIUM] vision_latest unconditionally evicts a fresh capture (and its JPEG) written by another producer
**File:** `dotty-behaviour/routes/vision.py:332`
**Issue:** The unconditional `pop` deletes whatever is cached, including a fresh entry another producer wrote microseconds earlier (idle_photographer / room_view), discarding its `jpeg_bytes` that `/api/vision/photo` serves; a subsequent photo GET 404s.
**Fix:** Pop only entries older than the TTL; or snapshot a request-start time and accept any entry newer than it.
**Verification:** unverified — over cap.

### [MEDIUM] Failed room_view VLM call still arms the 120s cooldown
**File:** `dotty-behaviour/routes/vision.py:188-204`
**Issue:** `last_room_view_capture_t` is set before the VLM call, which never raises (returns sentinels on failure), so a transient VLM blip arms the full 120s cooldown despite no usable description — repeated blips disable named greetings for minutes.
**Fix:** Arm the cooldown only after a successful, non-sentinel response, or use a short failure-cooldown.
**Verification:** unverified — over cap.

### [LOW] /api/perception/state emits invalid JSON (Infinity) for devices with no last_event_t
**File:** `dotty-behaviour/perception/state.py:328-336`
**Issue:** `_annotate` sets `age = float('inf')` → serialized as bare `Infinity` (not valid JSON), breaking strict parsers/the dashboard card. Reachable for a known device that got a room_view capture but never sent a perception event, and unconditionally for any unknown `device_id`.
**Fix:** Use a JSON-safe value (None / -1) for missing `last_event_t`; add a regression test asserting the body parses as standard JSON.
**Verification:** unverified — over cap.

### [LOW] Failed/empty weather fetch arms the full 30-min TTL
**File:** `dotty-behaviour/calendar_/cache.py:124-127` (driven by `poll.py:46-48`)
**Issue:** `set_weather` updates `weather_text` only on truthy text but always bumps `weather_fetched_perf`, so a transient weather failure marks the cache fresh for 30 minutes and serves stale (or empty) text. Same class as the room_view cooldown bug, distinct site (reported twice as F206/F246).
**Fix:** Advance `weather_fetched_perf` only on a non-empty fetch; add a test.
**Verification:** unverified — over cap.

### [LOW] perception/state limit query params unvalidated (negative/zero slice quirks)
**File:** `dotty-behaviour/routes/perception.py:88-99, 124`
**Issue:** `limit` flows into `items[:limit]` / `balances[-limit:]` unbounded; `limit=-3` drops the 3 newest, `limit=0` returns `[]` for one path and the FULL list for `balances[-0:]`.
**Fix:** Clamp `limit = max(0, min(limit, MAX))`; special-case `limit <= 0` for the balance series.
**Verification:** unverified — over cap.

### [LOW] Client-supplied event ts can be set in the future, defeating staleness gates
**File:** `dotty-behaviour/routes/perception.py:61-68`
**Issue:** `payload.ts` is used verbatim; a future ts makes `now - last_t` negative → clamped to 0, so `get_fresh_face_id` treats a stale identity as permanently fresh. ESP32 RTCs are frequently unsynced.
**Fix:** Reject/clamp implausible future timestamps on ingest; guard `get_fresh_face_id` against negative age.
**Verification:** unverified — over cap.

### [LOW] VLM error/offline SENTINEL strings cached as a real description and leaked into the perception snapshot
**File:** `dotty-behaviour/routes/vision.py:195-242`
**Issue:** `vision_explain` stores the `VLM_OFFLINE_SENTINEL`/`VLM_NETWORK_ERROR_SENTINEL` verbatim with a fresh `wall_ts`; the snapshot then surfaces `You see: ERROR: the vision service didn't respond...` into Dotty's voice prompt for up to 60s, and the idle photographer can persist it to NDJSON.
**Fix:** Detect the sentinels after `describe_image` and skip the cache write + `signal_vision_waiters`. Same for the audio fallback.
**Verification:** unverified — over cap.

### [LOW] snapshot.py age-gate constants hardcoded, ignoring env-configurable counterparts
**File:** `dotty-behaviour/perception/snapshot.py:18-20`
**Issue:** `VISION_AGE_GATE_SEC` etc. are literals, while config defines the env-overridable TTLs; raising `VISION_CACHE_TTL_SEC` keeps entries longer but the snapshot still gates at 60s. `config.SCENE_SYNTHESIS_AGE_GATE_SEC` has zero readers.
**Fix:** Import the gates from config (or pass them in); remove the unused config var or document the gates are fixed.
**Verification:** unverified — over cap.

### [LOW] perception/state stale dance_active never cleared on a missed terminal event
**File:** `dotty-behaviour/perception/state.py:185-199`
**Issue:** `dance_active` has no time-based expiry; a dropped `dance_ended`/`state_changed` latches it True indefinitely, permanently suppressing room_view captures and greetings for that device.
**Fix:** Make `is_dance_active` freshness-bounded against `last_dance_started_t` with a max-dance TTL.
**Verification:** unverified — over cap.

### [LOW] SSE feed only re-checks client disconnect every 15s, leaking a subscriber queue
**File:** `dotty-behaviour/routes/perception.py:142-153`
**Issue:** `is_disconnected()` is checked once per loop before a 15s `wait_for`, so a disconnect mid-block isn't noticed for up to 15s; the queue stays registered and `broadcast()` keeps filling/dropping it.
**Fix:** Reduce the poll timeout or race the disconnect check against the queue get.
**Verification:** unverified — over cap.

### [QUALITY/LOW–INFO] Dead prometheus dep; per-request weather subprocess; misplaced imports; None-guard inconsistency
**File:** `dotty-behaviour/requirements.txt:12-14`; `routes/calendar.py:51-62`; `main.py:40`; `consumers/sleep_dreamer.py:167` & `dance_reflector.py:90`
**Issue:** `prometheus-client` is pinned but no `/metrics` or counters exist; `/api/calendar/today` spawns a `curl wttr.in` subprocess on the request path even with no calendars configured; `import re`/`import asyncio` are misplaced; two consumers dereference `event.data` directly while siblings use `(event.data or {})`.
**Fix:** Implement or drop the metrics dep; gate weather refresh on `CALENDAR_IDS`; tidy imports; normalize `event.data` (default_factory or guard uniformly).
**Verification:** unverified — over cap.

---

## dotty-behaviour consumers/greeter/household/calendar/vision

### [HIGH] Double greeting on every face_recognized — FaceGreeter and ProactiveGreeter both fire
**File:** `dotty-behaviour/consumers/face_greeter.py:133-186`
**Issue:** FaceGreeter's named greet ("Oh, it's {name}!") and ProactiveGreeter's LLM greeting both subscribe to `face_recognized`, keep separate cooldown state, and don't coordinate — one recognition produces two back-to-back utterances. The bare path was deliberately suppressed when the roster is identifiable; the named path was not given the same deference.
**Fix:** Pick one owner (disable FaceGreeter's named path when the proactive greeter is enabled, or share a per-identity cooldown).
**Verification:** confirmed 2/3 lenses.

### [HIGH] room_view name parser cannot match multi-word display names
**File:** `dotty-behaviour/vision/room_view.py:72-81, 115-124`
**Issue:** `name_choices` injects `display_name`, but the NAME regex is a single whitespace-free token; a reply `NAME: Mary Anne` fails the anchored regex entirely, so any member with a space in their display name is a 100% silent identification failure even on a perfect VLM response.
**Fix:** Allow internal spaces in the NAME group (and normalise), or request a single-token id and map ids→display names. Add a two-word display_name test.
**Verification:** confirmed 3/3 lenses.

### [HIGH] room_view roster recognition fails when person id != display_name (greeter path)
**File:** `dotty-behaviour/vision/room_view.py:115-124, 159-167`
**Issue:** Same id/display_name name-space mismatch as the core finding; `room_match_person_id` is the VLM's display-name token lowercased, then passed as `identity` to `household.get()` which is keyed by id and also misses. (Reported as both F38 and F201.)
**Fix:** Agree on a single key (prefer `p.id`); fix the test fake and add an id != display_name fixture.
**Verification:** confirmed 3/3 lenses.

### [HIGH] Greeter calendar lookup never matches an identified person (case mismatch)
**File:** `dotty-behaviour/greeter/greeter.py:257-259` → `calendar_/cache.py:69-73`
**Issue:** `identity` is a lowercased person id, but the calendar `person` is set from the title prefix with no case folding (`[Hudson] → "Hudson"`), so `"Hudson" != "hudson"` drops that person's own events from their greeting. Household-bucket events mask the bug.
**Fix:** Normalise both sides to a common case (lower-case the parsed calendar `person`, or compare case-insensitively); prefer mapping the prefix to the canonical id at fetch time.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] FaceGreeter consumes the greet cooldown even when suppressed by an active dance
**File:** `dotty-behaviour/consumers/face_greeter.py:148-168, 105-126`
**Issue:** The cooldown slot is written before the dance-active gate, so every face event during a dance silently burns the cooldown without greeting; the user walks in during a dance, is never greeted, and stays un-greeted for the full cooldown after. `last_face_greet_t` also arms the aborter. (Reported as F160/F207.)
**Fix:** Move the dance-active (and empty-text) checks above the cooldown write in both handlers.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] last_chat_t is never written on real conversations — QUIET_AFTER_CHAT gates are dead
**File:** `dotty-behaviour/consumers/face_greeter.py:149-156` + `sound_turner.py:77-79` + `perception/state.py:200-203`
**Issue:** Named-greet and sound-turner suppress themselves for `*_quiet_after_chat_sec` after the last chat by reading `last_chat_t`, but the only writer is `purr_player.py`; nothing records actual conversation activity, so the gate never fires and Dotty name-greets/head-turns over a live conversation. Regression from bridge.py's `/api/message` ingress.
**Fix:** Write `last_chat_t` on conversation activity (set it on `chat_status` listening, or POST a conversation event), or drop the gates as inert.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] Greeter day-GC discards every other day's slots; midnight-roll cooldown corruption
**File:** `dotty-behaviour/greeter/greeter.py:224-228`
**Issue:** `_take_slot` derives `today` from wall-now but uses `event_ts` for the cooldown; the GC collapses `_state` to `{today: ...}`, wiping (and persisting away) other days. A pre-midnight event handled just after midnight is filed under the new day with old-day cooldown math, letting a near-instant second greeting slip through.
**Fix:** Key the day on `event_ts`; GC by a retention window (keep today + yesterday).
**Verification:** unverified — over cap.

### [MEDIUM] SecurityCycle keeps capturing on a missed/early transition
**File:** `dotty-behaviour/consumers/security_cycle.py:238-257`
**Issue:** Capture starts/stops purely off live `state_changed`; if the device is already in `security` when the consumer subscribes the timer never starts, and if the non-security transition frame is dropped the per-device capture loop fires the camera forever.
**Fix:** Seed timers from current `current_state` on subscribe; reconcile each running timer against the live state per cycle.
**Verification:** unverified — over cap.

### [MEDIUM] calendar fetch time window excludes the final minute of the day
**File:** `dotty-behaviour/calendar_/fetch.py:60-66`
**Issue:** `time_max = 23:59:59` with an exclusive `timeMax` loses the last minute; the correct upper bound is the start of the next local day.
**Fix:** `time_max = (start_of_today + timedelta(days=1)).isoformat()`.
**Verification:** unverified — over cap.

### [MEDIUM] CalendarCache.flush_for_new_day produces a long empty-context window on day-roll fetch failure
**File:** `dotty-behaviour/calendar_/cache.py:116-122` + `poll.py:53-75`
**Issue:** The day-roll eagerly flushes events, and a failing refresh only bumps `calendar_failures`; with backoff the loop may sleep up to 600s showing an EMPTY calendar in the morning even though yesterday's data was serviceable a moment earlier.
**Fix:** Flush only on a successful fetch, or reset failures / use the base interval for the first retry after a day-roll flush.
**Verification:** unverified — over cap.

### [MEDIUM] room_view 'no one in view' sentinel uses substring match
**File:** `dotty-behaviour/vision/room_view.py:152-153`
**Issue:** `if ROOM_VIEW_NO_PERSON in cleaned.lower()` discards a valid identification whose DESC merely mentions the phrase, throwing away both the description and the matched identity.
**Fix:** Match the sentinel only when it's the entire reply; try the DESC/NAME regex first.
**Verification:** unverified — over cap.

### [MEDIUM] FaceIdentifiedRefresher keeps the green ID LED lit when identity came from room_view
**File:** `dotty-behaviour/consumers/face_identified_refresher.py:65-71`
**Issue:** The skip gate only suppresses refresh when the face was both absent AND previously lost; a room_view identity sets `last_face_id` without `face_present` or a `face_lost`, so the LED stays green for the full TTL even after the person walked off.
**Fix:** Treat "identity present but face_present False and never positively detected" as a stop condition.
**Verification:** unverified — over cap.

### [LOW] FaceLostAborter leaks completed abort tasks in _pending forever
**File:** `dotty-behaviour/consumers/face_lost_aborter.py:101-103, 47-55`
**Issue:** A completed abort task with no subsequent face event stays in `_pending`; the dict never shrinks for departed devices.
**Fix:** Add a done-callback that discards the entry.
**Verification:** unverified — over cap.

### [LOW] Vision room_view block-reason early return skips stale-cache eviction
**File:** `dotty-behaviour/routes/vision.py:176-186`
**Issue:** A gated room_view request writes a fresh cache entry and returns before the stale-eviction loop, so on frequently-gated deployments stale entries for other devices are never reclaimed.
**Fix:** Run eviction on every exit path, including the block-reason early return.
**Verification:** unverified — over cap.

### [LOW] Blocking file I/O on the event loop in greeter state persistence
**File:** `dotty-behaviour/greeter/greeter.py:246, 420-435`
**Issue:** `_take_slot` calls `_save_state` synchronously (mkdir + write + os.replace) on the loop on every greet.
**Fix:** `await asyncio.to_thread(self._save_state)` or batch/debounce.
**Verification:** unverified — over cap.

### [LOW] SleepDreamer / DanceReflector dereference event.data without None-guard, crashing the consumer loop
**File:** `dotty-behaviour/consumers/sleep_dreamer.py:166-168`; `dance_reflector.py:89-92`
**Issue:** Direct `.get` on `event.data`; a `data=None` frame raises AttributeError caught only by the OUTER try/except, which logs "crashed" and EXITS the consumer permanently.
**Fix:** Use `(event.data or {}).get(...)`; wrap per-event handling so one bad frame doesn't terminate the loop.
**Verification:** unverified — over cap.

### [LOW] HouseholdRegistry.get_by_calendar_prefix only matches with bracketed YAML; Person.days_until_birthday Feb-29 drift
**File:** `dotty-behaviour/household/registry.py:190-200, 293-294` (prefix); `82-92` (birthday)
**Issue:** The prefix index stores the raw YAML value but the query is bracket-normalized, so an unbracketed `calendar_prefix` is unreachable. A Feb-29 birthdate silently shifts to Feb-28 in common years, firing the birthday greeting a day early.
**Fix:** Bracket-normalize both sides; decide and document a deliberate Feb-29 policy.
**Verification:** unverified — over cap.

### [INFO/QUALITY] Inconsistent None-guarding; convoluted day-GC expression
**File:** `dotty-behaviour/consumers/sleep_dreamer.py:167` / `dance_reflector.py:90`; `greeter/greeter.py:226-227`
**Issue:** None-guard inconsistency across consumers (also flagged above); the day-GC `{today: self._state.get(today, {})}` is provably `{today: {}}` and misleads readers.
**Fix:** Give `PerceptionEvent.data` a default_factory or guard uniformly; simplify the GC expression with a comment.
**Verification:** unverified — over cap.

---

## dotty-pi-ext voice tools (TypeScript)

### [HIGH] memory_lookup FTS search bypasses the #53 kid-safety namespace gate
**File:** `dotty-pi-ext/src/lib/brain_db.ts:82-90` (leaked into `tools/memory_lookup.ts`)
**Issue:** `fetchPersonMemories` is scoped to `person:<id>` and deliberately never returns `person_pending:<id>` (unreviewed minor facts), but `searchMemories` runs a bare `WHERE memories_fts MATCH ?` over ALL rows, so a pending fact about a minor ("kiddo is allergic to peanuts") is returned to a live turn whenever the query phrase-matches. The oracle (bridge.py) is equally unfiltered, so the gap exists in both.
**Fix:** Add `AND m.namespace NOT LIKE 'person_pending:%'` to the FTS query; mirror into oracle.py; add a regression test.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] Admin/behaviour HTTP clients disarm the timeout before reading the body
**File:** `dotty-pi-ext/src/lib/xiaozhi_admin.ts:33-46`; `dotty-pi-ext/src/lib/dotty_behaviour.ts:20-37`
**Issue:** `adminFetch`/`behaviourFetch` clear the AbortController timer in `finally` after `await fetch()` (headers), then callers do an un-timed `.json()`/`.text()`; a server that sends headers then stalls the body hangs the voice tool forever with no exception, so the try/catch fallbacks never run. The sibling `llama_swap.ts` does this correctly.
**Fix:** Keep the timer armed across the body read (move the body read inside the fetch helper, mirroring `llama_swap.ts`).
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] turn_logger.formatTurnLog omits the final strip the oracle applies
**File:** `dotty-pi-ext/src/lib/turn_logger.ts:98-102`
**Issue:** The oracle stores `content.strip()` of the assembled string; the TS version doesn't, so a user-only turn produces `"user: hello | assistant: "` (trailing space) vs the oracle's stripped form — every user-only turn stores a non-byte-identical row. The integration test that would catch it (`assistant_empty_user_only`) only runs when `DOTTY_BRAIN_DB_SNAPSHOT` is set, which `npm test` never sets.
**Fix:** `return (\`user: ${u} | assistant: ${a}\`).trim();`; wire the Layer-2 pass into `npm test`.
**Verification:** unverified — over cap.

### [LOW] play_song caches an empty catalogue on transient fetch failure for 60s
**File:** `dotty-pi-ext/src/tools/play_song.ts:83-90`
**Issue:** `getCatalog` caches `fetchSongCatalog`'s `[]` (which means "no songs" OR "fetch failed") for the full 60s TTL, so a momentary xiaozhi blip pins "(catalogue is empty)" for up to a minute.
**Fix:** Cache only non-empty results; treat empty as a non-cacheable miss.
**Verification:** unverified — over cap.

### [LOW] play_song matchSong: empty stem from a dotfile matches any query, diverging from the oracle
**File:** `dotty-pi-ext/src/tools/play_song.ts:39-74`
**Issue:** A leading-dot catalogue entry yields stem `''`, and `qStem.includes('')` is always true, so the dotfile matches every query and can win — Python's `splitext('.mp3')` keeps `.mp3`, so the implementations disagree, violating the byte-equal contract.
**Fix:** Only strip an extension when the dot index > 0; guard against an empty stem; add a leading-dot fixture.
**Verification:** unverified — over cap.

### [LOW] think_hard truncates by UTF-16 code units, diverging from the oracle and sibling tools
**File:** `dotty-pi-ext/src/tools/think_hard.ts:72-77`
**Issue:** `.slice(0, 500)` cuts by code units while the oracle and the other tools use codepoint slicing; an emoji-prefixed near-cap reply mis-cuts. think_hard is the lone inconsistent path.
**Fix:** Use `Array.from(...)` codepoint slicing like the sibling tools.
**Verification:** unverified — over cap.

### [LOW] play_song host-presence guard ignores the _XIAOZHI_HOST fallback
**File:** `dotty-pi-ext/src/tools/play_song.ts:104`
**Issue:** The guard checks only `XIAOZHI_HOST`, but the admin client also honors `_XIAOZHI_HOST`; the two layers disagree on "host configured".
**Fix:** Use the same precedence as the admin client, or drop the guard and let the empty-list fallback drive the message.
**Verification:** unverified — over cap.

### [LOW] extractTurnText concatenates assistant text blocks with no separator
**File:** `dotty-pi-ext/src/lib/turn_logger.ts:56-62, 79-91`
**Issue:** Multi-message/multi-block assistant turns join with `""`, fusing fragments ("Let me check…The answer is 4") in the logged turn.
**Fix:** Join with a space/newline.
**Verification:** unverified — over cap.

### [LOW] think_hard default model resolved at module load, diverging from the per-call oracle
**File:** `dotty-pi-ext/src/tools/think_hard.ts:25`
**Issue:** `DEFAULT_MODEL` reads `VOICE_THINKER_MODEL` once at import; the oracle reads it per call, so a later env change diverges the request body.
**Fix:** Resolve the env lazily inside `buildThinkRequest`.
**Verification:** unverified — over cap.

### [LOW] play_song getCatalog TOCTOU lets concurrent turns both refetch
**File:** `dotty-pi-ext/src/tools/play_song.ts:83-90`
**Issue:** Two interleaved `play_song` calls both see the stale timestamp and both fetch; combined with the empty-cache bug, one can poison the other.
**Fix:** Store an in-flight promise so concurrent callers await the same fetch; cache only non-empty success.
**Verification:** unverified — over cap.

### [DOC/QUALITY/LOW–INFO] Stale ZeroClaw comments; take_photo 300-char cap claim; turn_logger "background write" wording; brain_db WAL/handle-staleness; take_photo test not run
**File:** `dotty-pi-ext/src/lib/brain_db.ts:5,44-46,176,42-52,32-52`; `src/tools/take_photo.ts:1-9`; `src/lib/turn_logger.ts:104-128`; `package.json:17-26`
**Issue:** brain_db comments still claim ZeroClaw co-writes (now the extension is the sole writer); take_photo's "capped at 300 chars" cap lives server-side, not in the TS client; turn_logger claims a background write but the sqlite INSERT is synchronous inline; brain_db's "WAL concurrency" comment never enables WAL and relies on the default busy_timeout; cached handles go stale if brain.db is swapped under a running process; `tests/take_photo.test.ts` exists but `npm test` never runs it.
**Fix:** Update the comments to the post-#36 reality; add the take_photo test to the aggregate; either enable WAL or fix the comment; document the no-swap-under-running-process contract.
**Verification:** unverified — over cap.

---

## StackChan firmware (ESP32-S3 C++/ESP-IDF)

### [MEDIUM] Face recognizer reads the camera frame buffer after releasing the arbiter (latent UAF)
**File:** `firmware/firmware/main/stackchan/face/face_detector.cpp:236-292`
**Issue:** `processFrame()` releases the detection arbiter at :236 but still uses the raw V4L2 `frame_data` (captured at :183) afterward — stored into `face_img.data` (:277) and passed to `recognize()` (:292). Once released, the capture path can dequeue/requeue the buffer and overwrite it. The stub ignores the data today, but the planned ESP-DL embedding crop will make this a live use-after-free across two camera tasks.
**Fix:** Hold the detection lock across `recognize()`, or copy the bounded crop while still holding the arbiter and point `face_img.data` at the copy.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] SoundLocalizer high-pass filter state goes stale during cooldown, producing a spurious spike
**File:** `firmware/firmware/main/stackchan/sound_localizer.cpp:19-23, 44-51`
**Issue:** `OnStereoFrame()` returns early on the cooldown gate before advancing the HPF state, so the stored x[n-1]/y[n-1] freeze for up to 750 ms; the first post-cooldown frame evaluates across that discontinuity, injecting a transient that can push energy past threshold and fire a spurious direction change.
**Fix:** Advance the HPF state every frame regardless of the gates (or prime `_hp_*_prev_x` to the current input on cooldown).
**Verification:** unverified — over cap.

### [MEDIUM] ImuEventModifier steals and releases the shared modify-lock it may not own
**File:** `firmware/firmware/main/stackchan/modifiers/imu.h:58-61, 117, 79, 105`
**Issue:** IMU sets the motion lock only if not held, but `restore_state()` always clears it; if `Capture()` holds the lock to keep the head still for the shutter, a shake's restore releases Capture's lock mid-photo, letting the head move. The avatar lock has the same shape.
**Fix:** Give the lock real ownership (refcount/owner-token), or have IMU remember whether it acquired it and only release if so.
**Verification:** unverified — over cap.

### [MEDIUM] FaceDetector::stop() frees buffer and deletes the stop-semaphore without checking the take succeeded — UAF on overrun
**File:** `firmware/firmware/main/stackchan/face/face_detector.cpp:100-117`
**Issue:** `stop()` ignores the `xSemaphoreTake(_stop_sem, 2000ms)` return, then unconditionally deletes the semaphore and frees `_rgb_buffer`; if the task overruns the 2s timeout it later gives a deleted semaphore and uses freed state. Currently unwired but a real teardown defect.
**Fix:** Capture the take result; only delete/free when it succeeded.
**Verification:** unverified — over cap.

### [LOW] Seqlock reader can return a torn read on weak memory (missing acquire barrier before the second seq load)
**File:** `firmware/firmware/main/stackchan/face/face_detection_result.h:33-47`
**Issue:** `read()` loads s1 (acquire), copies plain data fields, loads s2 (acquire) — but an acquire load doesn't forbid the earlier data reads from sinking below the s2 load, so a torn read can pass the seq check. The writer side is correct; the reader is missing the symmetric barrier.
**Fix:** Insert `std::atomic_thread_fence(std::memory_order_acquire)` between the data copies and the second seq load.
**Verification:** unverified — over cap.

### [LOW] Idle-channel proactive reconnect churns close+reopen every ~30 ticks
**File:** `firmware/firmware/patches/xiaozhi-esp32.patch:23-32`
**Issue:** The reconnect fires on `IsTimeout()` (time-since-last-incoming, 120s); a reopened quiet-but-healthy channel keeps `IsTimeout()` true, so it close+reopens every 30 ticks indefinitely, each iteration paying a 200ms delay + full TLS/WS handshake on the main task.
**Fix:** Gate on an actual dead-channel signal or a backoff; reset the incoming-frame clock on `OpenAudioChannel()`.
**Verification:** unverified — over cap.

### [LOW] CloseAudioChannel() blocks the calling task 200ms on every close
**File:** `firmware/firmware/patches/xiaozhi-esp32.patch:582`
**Issue:** An added `vTaskDelay(200ms)` stalls whatever task calls close (incl. the main Run loop), compounding the reconnect churn.
**Fix:** Move the drain delay to only the reopen path that needs it, or make close async.
**Verification:** unverified — over cap.

### [LOW] PrivacyLeds::update() non-atomic RMW races the guard's setMicState(Off), latching the listening LED on
**File:** `firmware/firmware/main/stackchan/privacy/privacy_leds.cpp:49-60`
**Issue:** `update()` load→derive→store of `_mic_state` isn't atomic as a unit; the codec-close dtor can set Off in the window and `update()` overwrites it, so the green mic LED keeps showing "listening" after the mic closed (privacy over-indication).
**Fix:** Use a CAS that only updates while non-Off so a concurrent Off isn't resurrected.
**Verification:** unverified — over cap.

### [LOW] CameraPeripheralGuard ctor-fallback/dtor-normal mismatch underflows the refcount
**File:** `firmware/firmware/main/stackchan/privacy/camera_peripheral_guard.cpp:40-66, 68-86`
**Issue:** The null-mutex ctor fallback doesn't increment `g_refcount`, but the dtor unconditionally `fetch_sub(1)`; a ctor-fallback/dtor-normal mismatch underflows uint32 to UINT32_MAX, so the stream never tears down and the camera LED stays Active indefinitely.
**Fix:** Track per-guard `_incremented`; only `fetch_sub` if it incremented; clamp against underflow.
**Verification:** unverified — over cap.

### [LOW] MCP take_photo arms a fixed 10s camera-glow timer decoupled from capture duration
**File:** `firmware/firmware/patches/xiaozhi-esp32.patch:523`
**Issue:** `setCameraLedActive(true, 10000)` is cosmetic but hardcoded to 10s with no off-call; the glow lingers after a fast capture or goes dark before a slow one, no longer tracking the real camera lifecycle.
**Fix:** Tie the glow to the capture scope (no timeout; clear after Capture, or drive from the guard refcount).
**Verification:** unverified — over cap.

### [LOW] Unescaped name/session_id in Protocol::SendEvent JSON construction
**File:** `firmware/firmware/patches/xiaozhi-esp32.patch:537-542`
**Issue:** The frame is built by raw concatenation; neither `name` nor server-supplied `session_id_` is escaped. Not currently exploitable (literal names, pre-escaped data), but any future runtime name or a quoted session id emits malformed JSON the relay drops.
**Fix:** Build with cJSON (escapes both); validate `data_json` parses; or assert+escape.
**Verification:** unverified — over cap.

### [LOW] Debug log leftovers fire at WARN/ERROR on every WS and LLM frame
**File:** `firmware/firmware/patches/xiaozhi-esp32.patch:65, 590`
**Issue:** An `ESP_LOGW` on every LLM frame and an `ESP_LOGE` on every inbound WS frame bypass log-level filtering, spam serial, and bury real errors.
**Fix:** Drop or demote to `ESP_LOGD`.
**Verification:** unverified — over cap.

### [LOW] HeadPet/Imu cross-task gesture flags are plain volatile bool, not atomic; fixed Press-before-Release order can invert fast sequences
**File:** `firmware/firmware/main/stackchan/modifiers/head_pet.h:50-52` (and `head_pet.cpp:48-83`, `imu.h:124`)
**Issue:** `volatile bool` flags written from the signal callback and read+cleared on the tick task aren't atomic (a gesture can be lost), and processing press→swipe→release in fixed order can end with `_is_touched=false` on a fast release-then-press, silently never arming the hold-to-listen window.
**Fix:** Use `std::atomic<bool>` with `exchange(false)`, or an ordered event queue.
**Verification:** unverified — over cap.

### [LOW] ParentalGate unlock at millis()==0 treated as "never unlocked"
**File:** `firmware/firmware/main/stackchan/face/parental_gate.cpp:43, 71-72`
**Issue:** `_unlocked_at_ms` uses 0 as the "never" sentinel; an unlock in the first ms after boot stores 0 and is silently dropped. Dormant scaffold but a latent collision.
**Fix:** Use a separate `_is_unlocked` flag, or store `max(now,1)`.
**Verification:** unverified — over cap.

### [LOW] CameraArbiter::acquireForCapture clears the shared _capture_pending on timeout
**File:** `firmware/firmware/main/stackchan/face/camera_arbiter.cpp` (header :38-44)
**Issue:** `_capture_pending` is a single shared atomic, not per-call; a future second capture path plus a timeout could drop both callers' intent, wedging the other's still-pending capture — the TOCTOU the flag exists to prevent.
**Fix:** Make `_capture_pending` a refcount, or document+assert single-caller.
**Verification:** unverified — over cap.

### [QUALITY/DOC/LOW–INFO] PrivacyLeds doc/comment drift; dead FaceRecognizer members
**File:** `firmware/firmware/main/stackchan/privacy/privacy_leds.{h,cpp}` (h:15-19,137-141; cpp:64-65,89); `face/face_recognizer.h:172-173`
**Issue:** Header docs describe MIC LEDs as white (actually green) and the self-test as amber→cyan→red (actually green→red→off); inline comments say the hal guard "rejects 6/7" and the camera LED is at "index 7" (actually 6/11, camera at kCameraLedIndex=11); `_last_recognize_ms`/`_last_identity` are dead members.
**Fix:** Update the docs/comments to match the implementation; delete the dead members.
**Verification:** unverified — over cap.

---

## Top-level + scripts (receiveAudioHandle, dances, scripts/, provision)

### [HIGH] provision.py: voice "General" collides with text "general" and gets moved on every run
**File:** `community/discord/provision.py:93-96, 197-202`
**Issue:** `find_channel` searches the whole guild case-insensitively and ignores channel type, so syncing the VOICE "General" matches the existing TEXT "general" and `existing.edit(category=VOICE)` MOVES #general into the VOICE category (destructive, recurs every run); the real voice channel is never created.
**Fix:** Scope `find_channel` by channel type (compare (name, type) tuples); never move a channel whose type doesn't match.
**Verification:** confirmed 2/3 lenses.

### [HIGH] dotty_doctor model checks look under data/models instead of repo-root models — false FAIL
**File:** `scripts/dotty_doctor.py:128-155`
**Issue:** With config at `data/.config.yaml`, `root = config_path.parent` is `data/`, so the checks look for `data/models/...` while the models actually live at repo-root `models/` — both checks FAIL and the doctor exits non-zero on a healthy standard deploy.
**Fix:** Don't anchor to `config_path.parent`; if it's a `data/` dir use the parent, or search both `root/models` and `root.parent/models`.
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] startToChat: missing "language" key throws KeyError and feeds raw JSON to the pipeline
**File:** `receiveAudioHandle.py:1057-1063`
**Issue:** `_language_tag = data["language"]` (subscript) inside a try that swallows KeyError; when speaker+content are present but language is absent, `actual_text` stays the RAW JSON envelope and gets run through ASR corrections/intent/LLM, and `current_speaker` is never set.
**Fix:** `data.get("language")`.
**Verification:** unverified — over cap.

### [MEDIUM] provision.py: read-only permission overwrites never re-applied to existing channels
**File:** `community/discord/provision.py:197-202`
**Issue:** The existing-channel branch fixes only the category and `continue`s, never applying overwrites, so READ_ONLY channels that already exist keep `@everyone` send_messages — the script isn't idempotent for permissions on a public server.
**Fix:** Compute and `existing.edit(overwrites=ov)` in the existing branch when non-empty.
**Verification:** unverified — over cap.

### [MEDIUM] dotty_doctor check_http treats 404 (and all <500) as pass for the OTA endpoint
**File:** `scripts/dotty_doctor.py:166-168`
**Issue:** A 404 on `/xiaozhi/ota/` — exactly the misconfiguration the doctor exists to catch — is reported as PASS.
**Fix:** Treat 2xx/3xx as pass; flag 4xx as warn/fail with the status code.
**Verification:** unverified — over cap.

### [MEDIUM] MCP JSON-RPC request ids collide for calls in the same millisecond
**File:** `receiveAudioHandle.py:281, 319, 349, 373, 399, 456, 559`
**Issue:** Every MCP id is `int(time.time()*1000) % 0x7FFFFFFF`; back-to-back sends (e.g. two `set_toggle`, or HEAD+LED at the same timeline mark) get identical ids, so a relay correlating by id can mismatch/drop responses.
**Fix:** Use a monotonic per-connection counter via a shared `_next_mcp_id(conn)` helper.
**Verification:** unverified — over cap.

### [LOW] dotty_doctor passes Piper without the required .onnx.json config pair
**File:** `scripts/dotty_doctor.py:146-155`
**Issue:** The check globs only `*.onnx`; a missing/stub `.onnx.json` (the exact corrupt-download failure the SenseVoice check was hardened against) still reports green PASS while the TTS provider crashes at runtime.
**Fix:** For each `*.onnx`, assert the sibling `*.onnx.json` exists above a byte floor.
**Verification:** unverified — over cap.

### [LOW] render_singing_sinsy crashes with ZeroDivisionError on empty/all-comment lyrics
**File:** `scripts/render_singing_sinsy.py:89`
**Issue:** `syllables[i % len(syllables)]` with `len==0` raises an opaque traceback when the lyrics file is empty/comments-only; the notes list is guarded but syllables isn't.
**Fix:** Add a `if not syllables:` guard mirroring the notes guard.
**Verification:** unverified — over cap.

### [LOW] _send_led_color swallows all exceptions silently
**File:** `receiveAudioHandle.py:285-286`
**Issue:** A bare `except Exception: pass` with no log; a broken WS during a dance silently no-ops every LED update, making "dance ran but no lights" undebuggable.
**Fix:** Log (warn-once) like `_send_led_multi`.
**Verification:** unverified — over cap.

### [LOW] _encode_midi_to_opus forces ALL set_tempo events to one value
**File:** `receiveAudioHandle.py:850-857`
**Issue:** Rewriting every tempo event to one value flattens intended tempo changes; benign for the current registry, latent for any song with tempo automation.
**Fix:** Rewrite only the first/global tempo, or scale all tempos by the same ratio.
**Verification:** unverified — over cap.

### [LOW] _handle_dance: singing stream task untracked, so a new utterance cancels choreography but not the audio
**File:** `receiveAudioHandle.py:789-802, 1092-1096`
**Issue:** Choreography is tracked and cancelled on the next turn; the singing audio task handle is never stored, so on barge-in it stops only if it observes `client_abort` flip between 60ms packets — fragile, no explicit cancel.
**Fix:** Store `conn._singing_task` and cancel it alongside `_dance_task`.
**Verification:** unverified — over cap.

### [LOW] Idle end-prompt re-enters the full intent pipeline via startToChat
**File:** `receiveAudioHandle.py:1208-1211`
**Issue:** The idle-timeout end-prompt is fed through `startToChat`, which runs state/dance/vision detection; a customised prompt containing "dance"/"sleep"/a vision phrase fires that side-effect at the moment the connection is closing.
**Fix:** Submit the end-prompt directly to the LLM, bypassing intent detection (or add a skip flag for system prompts).
**Verification:** unverified — over cap.

### [LOW] provision.py create_forum drops the computed permission overwrites
**File:** `community/discord/provision.py:204-213`
**Issue:** The FORUM branch omits `overwrites=ov`; benign today (no forum in READ_ONLY) but silently no-ops a future read-only forum lockdown.
**Fix:** Pass `overwrites=ov` to `guild.create_forum`.
**Verification:** unverified — over cap.

### [QUALITY/LOW] Dead _write_smart_mode_state; stale ZeroClaw docstrings; redundant JSON re-parse; shared-list in-place sort; unreachable _encode_song_to_opus
**File:** `receiveAudioHandle.py:75-81, 634/644/651, 1054-1063/1114-1119, 761-762/928`; `dances.py:111-112, 302`
**Issue:** `_write_smart_mode_state` is never called (xiaozhi side is read-only by design); `_with_room_view_marker` docstring still describes the retired "zeroclaw `_payload`"; `actual_text` is JSON-re-parsed a second time after it's already unwrapped; `_macarena_moves` returns the shared module-global list that `resolve_timeline` sorts in place; `_encode_song_to_opus` (WAV path) is unreachable because every registry entry is `.mid`, so rendered singing `.wav` never plays.
**Fix:** Delete dead code; fix stale docstrings to name PiVoiceLLM; drop the redundant parse; return `list(_MACARENA_TIMELINE)`; wire a `.wav` registry entry or remove the WAV path.
**Verification:** unverified — over cap.

---

## docs: architecture, internals, setup, operations, policy

### [HIGH] disable-kid-mode.md references non-existent _content_filter and _ensure_emoji_prefix as active
**File:** `docs/cookbook/disable-kid-mode.md:43-44`
**Issue:** The doc claims `_content_filter` and `_ensure_emoji_prefix` "remain active in both modes"; neither symbol exists anywhere (the only content filter, planned per #138, lives unused in bridge/text.py; `_ensure_emoji_prefix` was ZeroClaw-era). Emoji enforcement is now in `textUtils.build_turn_suffix()` / the `ALLOWED_EMOJIS` fallback in openai_compat.py.
**Fix:** Delete the sentence; replace with the real enforcement points and note there is no content-word filter yet (#138).
**Verification:** confirmed 3/3 lenses.

### [HIGH] disable-kid-mode.md tells the user to restart the bridge, but kid mode is read by xiaozhi-server
**File:** `docs/cookbook/disable-kid-mode.md:19`
**Issue:** Kid mode drives the live voice path via `pi_voice._read_kid_mode()` (and openai_compat) running inside the xiaozhi-server container; restarting only the bridge won't change the suffix.
**Fix:** Change to `docker compose restart xiaozhi-server`; mention the dashboard's live toggle as the no-restart alternative.
**Verification:** confirmed 3/3 lenses.

### [MEDIUM] Tool count says 'five voice tools'; there are seven (recall_person/remember_person, #53)
**File:** `docs/brain.md:12,58-68`; `docs/voice-pipeline.md:74`; `docs/llm-backends.md:131-145,22`; `docs/COMPATIBILITY.md:16-18`; `COMPATIBILITY.md:18`; `docs/architecture.md:125,151,239`; `docs/protocols.md:249`; `docs/latent-capabilities.md:66`; `docs/modes.md:175`
**Issue:** Ten+ docs undercount the dotty-pi-ext tools as five and omit recall_person/remember_person (registered in `src/index.ts:21-27`). COMPATIBILITY.md also pins firmware v1.2.4 while the submodule is at fw-v1.3.1.
**Fix:** Update every "five voice tools" reference to seven with the full list; bump the firmware version.
**Verification:** confirmed 2/3 (brain.md, voice-pipeline.md, COMPATIBILITY, architecture, protocols, modes) / 3/3 (latent-capabilities).

### [MEDIUM] architecture.md claims smart-mode model swap is 'now handled in PiVoiceLLM' — it isn't
**File:** `docs/architecture.md:175`
**Issue:** PiVoiceLLM has no smart-mode/model-swap logic; bridge.py and brain.md/modes.md all say the swap is unwired v2 scope.
**Fix:** Replace with "Smart-mode model swap is v2 scope and NOT wired on the PiVoiceLLM path."
**Verification:** confirmed 2/3 lenses.

### [MEDIUM] Other architecture/protocol doc drift (shared_llm, /api/voice/escalate, port 8000, ZeroClaw paths)
**File:** `docs/architecture.md:248,172-176`; `docs/protocols.md:292`; `docs/voice-mode-entry.md:40`; `docs/cookbook/add-emoji.md:8-37`; `docs/troubleshooting.md:86`; `docs/proactive-greetings.md:66/83,129-157`
**Issue:** `shared_llm singleton` was removed with Tier1Slim; `/api/voice/escalate` does not exist (not "non-functional"); inject-text is on 8003 not 8000; add-emoji.md edits bridge.py for symbols that live in textUtils.py (and never mentions EMOJI_MAP, the load-bearing edit); troubleshooting.md maps a directory mount where compose mounts a file; proactive-greetings.md points all code refs at retired `bridge/` paths and gives a wrong `GREETER_PER_DAY_MAX` default.
**Fix:** Repoint each to the real symbol/path/port; rewrite add-emoji.md around `textUtils.py` + `EMOJI_MAP`.
**Verification:** confirmed 2/3 (proactive-greetings Files) / others unverified — over cap.

### [LOW] Misc doc nits (container name `bridge` vs `dotty-bridge`, persona source, missing scripts/backup.sh, README section, SceneSynthesisLoop, taxonomy, /admin table, observability metric)
**File:** `SETUP.md:33`; `docs/troubleshooting.md:135,138`; `docs/COMPATIBILITY.md:60`; `COMPATIBILITY.md:59`; `CLAUDE.md:48`/`SETUP.md:9`/`CONTRIBUTING.md:32,70`; `docs/architecture.md:216`/`modes.md:13,202`; `docs/interaction-map.md:65`; `docs/architecture.md:172-176`; `docs/observability.md:74-84`; `docs/cookbook/change-persona.md:24,47`; `docs/faq.md`/`docs/kid-mode.md`
**Issue:** `docker logs bridge` should be `dotty-bridge`; `scripts/backup.sh` doesn't exist; the "Configuring for your environment" README section is gone (now `docs/quickstart.md`); the consumer class is `SceneSynthesisLoop`; the modes taxonomy is the six-state mutex; the /admin table omits /state and /persona; observability.md omits the one live metric (`dotty_content_filter_hits_total`); change-persona/faq/kid-mode misattribute the persona source (loaded by the pi agent from `/root/.pi`, not the extension).
**Fix:** Apply each correction as listed in the per-finding suggestions.
**Verification:** unverified — over cap.

---

## root + policy docs

### [LOW] CHANGELOG [Unreleased] presents retired ZeroClaw systemd bridge infra as pending
**File:** `CHANGELOG.md:10`
**Issue:** The [Unreleased] Added entry describes a `zeroclaw-bridge.service.template` + `scripts/install-bridge.sh` that no longer exist; the bridge now ships as the `dotty-bridge` container.
**Fix:** Move the entry to the historical ZeroClaw-era section or strike it.
**Verification:** unverified — over cap.

### [LOW] SECURITY.md threat model cites a retired /api/message bridge endpoint
**File:** `SECURITY.md:23-24`
**Issue:** `/api/message` was the ZeroClaw voice endpoint; the bridge registers no such route and its `/admin/*` is localhost-gated. The real unauthenticated LAN surface is xiaozhi-server's `/xiaozhi/admin/*` and dotty-behaviour's `/api/*`.
**Fix:** Replace the `/api/message` reference with the real surface.
**Verification:** confirmed 3/3 lenses.

### [LOW] ROADMAP / FAQ / persona docs carry stale Tier1Slim framing
**File:** `ROADMAP.md:52`; `docs/faq.md:59,73`; `docs/kid-mode.md:50,59,225,297`; `personas/dotty_voice.md` (title)
**Issue:** ROADMAP frames latency as a "two-tier path" (Tier1Slim removed); FAQ/kid-mode imply `personas/dotty_voice.md` is the live agent persona while the only wired persona is `personas/default.md` (OpenAICompat) and the agent loads from `/root/.pi`; `dotty_voice.md` is still titled "(Tier 1)".
**Fix:** Reword to the single PiVoiceLLM + think_hard design; clarify the persona mapping; drop the "Tier 1" label.
**Verification:** unverified — over cap.

---

## monitoring/grafana-dashboard.json (completeness sweep)

### [HIGH] Six of eight dashboard panels query metrics that are never recorded
**File:** `monitoring/grafana-dashboard.json:107-461, 523-537`
**Issue:** Only `dotty_kid_mode_active` and `dotty_content_filter_hits_total` are ever written; the first-audio-latency, request-rate, error-rate, smart-mode, perception-events, and calendar-failure panels all render empty on import (7 of 8 panels broken).
**Fix:** Wire the metrics at their call sites, or trim the dashboard to live panels and add a Content-Filter-hits panel.
**Verification:** confirmed 1/1 lenses.

### [HIGH] Grafana description + tag still name the retired ZeroClaw system
**File:** `monitoring/grafana-dashboard.json:18, 629, 660`
**Issue:** The description says "zeroclaw-bridge ... ACP sessions" and the tags array carries a literal `zeroclaw` tag (polluting Grafana tag search).
**Fix:** Rename the description, drop the ACP mention, remove the `zeroclaw` tag.
**Verification:** confirmed 1/1 lenses.

### [HIGH] Active ACP sessions panel queries a dead metric
**File:** `monitoring/grafana-dashboard.json:266-337`
**Issue:** Panel id 4 queries `dotty_active_acp_sessions` (defined but never observed; ACP retired in #36), with a red/green threshold that shows a permanently RED "ACP child not respawning" for a subsystem that no longer exists.
**Fix:** Delete panel id 4.
**Verification:** confirmed 1/1 lenses.

### [MEDIUM/LOW] monitoring README/observability.md overstate live coverage; metrics.py identity still 'zeroclaw-bridge'
**File:** `monitoring/README.md:5-7`; `docs/observability.md:8-10,66-84`; `bridge/metrics.py:1,36`
**Issue:** The docs list 9 metrics as emitted while only 2 are, and omit the one live metric (`dotty_content_filter_hits_total`); the metrics module docstring/logger are still named `zeroclaw-bridge`.
**Fix:** Mark unwired metrics as "defined, not yet wired", add the content-filter row, rename the logger to `dotty-bridge.metrics`.
**Verification:** low-sev, unverified.

---

## gap: Emoji-glyph → firmware-emotion contract

### [MEDIUM] add-emoji.md omits the EMOJI_MAP edit and points at stale bridge.py locations
**File:** `docs/cookbook/add-emoji.md:8-37`
**Issue:** The only edit that makes a new glyph produce a face is adding it to `EMOJI_MAP` in `custom-providers/textUtils.py` — the doc never mentions it, and instead tells the user to edit `bridge.py` `ALLOWED_EMOJIS`/`_BASE_SUFFIX`, which now live in textUtils.py (and bridge.py is off the voice path). Following it verbatim adds a glyph that passes prompt enforcement but never resolves to an emotion frame.
**Fix:** Rewrite around textUtils.py: add to `ALLOWED_EMOJIS`, add a glyph→firmware-name entry to `EMOJI_MAP` (matching a `stackchan_display.cc` strcmp branch), update `_BASE_SUFFIX` + the `.config.yaml` prompt, redeploy xiaozhi-server.
**Verification:** low-sev, unverified.

### [INFO] Emoji→firmware-emotion contract verified intact (no verb-form fallthrough)
**File:** `custom-providers/textUtils.py:64-90` + `firmware/.../stackchan_display.cc:355-432`
**Issue:** None — the hypothesised `laugh`/`laughing` mismatch is NOT a bug; `EMOJI_MAP` already emits the gerund/past-tense names the firmware expects, and all 9 protocol emojis resolve end-to-end. `laughing≡happy` and `crying≡sad` collapse by design.
**Fix:** No code change; optionally document the two intentional collapses.
**Verification:** low-sev, unverified.

### [LOW] CLAUDE.md says the firmware parses the emoji glyph; the server does
**File:** `CLAUDE.md:84`
**Issue:** The firmware reads a pre-resolved english emotion string; the glyph→name translation happens server-side in `get_emotion()`/`EMOJI_MAP`. This imprecision is the conceptual root of the add-emoji.md misdirection.
**Fix:** Amend to say xiaozhi-server's `get_emotion()`/`EMOJI_MAP` parses the leading emoji into an emotion name and the firmware renders the face.
**Verification:** low-sev, unverified.

---

## gap: Test-coverage gaps for confirmed bugs

### [HIGH] TTS processed_chars over-count is untested AND the provider package is excluded from coverage
**File:** `custom-providers/edge_stream/edge_stream.py:67-78`; `custom-providers/piper_local/piper_local.py:114-125`
**Issue:** Both providers carry the confirmed over-count bug, neither is in `[tool.coverage.run] source`, and no test imports them — so the 56% floor is computed over a source set that physically cannot see these lines; a fix can regress green.
**Fix:** Fix the bug, add a pure-logic unit test, add both packages to the coverage source and the ci.yml pytest targets.
**Verification:** confirmed 1/1 lenses.

### [HIGH] OTA _is_higher_version pre-release ordering bug is outside the coverage source set with no covering test
**File:** `custom-providers/xiaozhi-patches/ota_handler.py:24-43`
**Issue:** The pure version-compare functions are trivially testable but 0% covered and invisible to the CI floor (xiaozhi-patches is only ruff-linted).
**Fix:** Replace the digits-only parser with a semver-aware comparator; add a version-table unit test; add the module to coverage and wire a test dir into ci.yml.
**Verification:** confirmed 1/1 lenses.

### [HIGH] Firmware C++ concurrency code has no native test harness and is invisible to every CI gate
**File:** `firmware/firmware/main/stackchan/face/face_detection_result.h:19-46` (+ camera path)
**Issue:** The seqlock torn-read and camera UAF findings have no host/native unit-test target; ci.yml has no firmware build-or-test job, so these subtle-ordering bugs can be refactored with zero signal.
**Fix:** Stand up a host-native (TSan-enabled) test target for the hardware-free headers (FaceDetectionResult compiles standalone); add a firmware-host-tests job to ci.yml.
**Verification:** confirmed 1/1 lenses.

### [MEDIUM] openai_compat emoji-prefix + turn-suffix logic untested and excluded from coverage; 56% floor is blind to the high-sev modules
**File:** `custom-providers/openai_compat/openai_compat.py`; `pyproject.toml:22-33` + `.github/workflows/ci.yml:88-99`
**Issue:** OpenAICompat (the only PiVoiceLLM fallback) is entirely untested and outside the coverage source, and the `--cov-fail-under=56` gate measures only 3 of the shipped Python modules — every module carrying a confirmed high/medium bug (edge_stream, piper_local, openai_compat, asr, ota_handler) is outside the source set, so the floor provides no regression protection for them.
**Fix:** Add a streaming-generator unit test for the emoji/suffix logic; expand `[tool.coverage.run] source` + the ci.yml pytest targets as part of each fix; document that the floor is per-source-set.
**Verification:** low-sev, unverified.

---

## gap: firmware/server/ subtree

### [INFO] firmware/server/ is vendored upstream m5stack Go backend — dead in Dotty, undocumented as such
**File:** `firmware/server/` (entire subtree)
**Issue:** The hypothesis that this is live OTA/provisioning that could drift from `ota_handler.py` does NOT hold — `firmware/` is a submodule and `firmware/server/` is the stock m5stack GoFrame social-feed backend vendored inside the fork, referenced nowhere in the parent repo, never built by the ESP-IDF flow, and not COPY'd by any container. The flagged config files are upstream placeholders (GoFrame default ports, boilerplate image prefixes), not Dotty values. The only real issue is navigational: CLAUDE.md never says the submodule carries this unrelated tree, so a reviewer can mistake it for live infra.
**Fix:** No code change. Add one line to CLAUDE.md's "Firmware iteration" section noting the firmware submodule vendors upstream `server/`/`app/`/`remote/` trees that are NOT part of Dotty's stack; Dotty's OTA is `custom-providers/xiaozhi-patches/ota_handler.py`.
**Verification:** low-sev, unverified.

---

## Open issues & PRs

| Ref | Disposition | Action |
|-----|-------------|--------|
| #104 | still-valid (P1) | Keep open. Implement fix (b): give server-pushed audio its own `server_push_sentence_id`; pair with #105. |
| #105 | still-valid (P1) | Keep open. Start with SileroVAD threshold bump; then ASR RMS floor / wake-word gating. Triage with #104. |
| #138 | still-valid (planned) | Keep open. If picked up: lift `content_filter()` into a shared module, gate on kid_mode, wire onto TTS-bound text, red-team on device. |
| #125 | still-valid | Resolved by PR #139 (deploy-dotty-pi.sh). Keep open until merged. |
| #98 / #135 | still-valid (speculative) | Keep open. Largely delivered by PR #140 (SenseVoiceOnnx). No urgency — funasr path works. |
| #60 | still-valid | Keep open. Add docker.sock mount to bridge compose + a CSRF-guarded restart POST. |
| #21 | still-valid | Keep open. Implement firmware fix (b): re-fire `state_changed` on audio-channel (re)open. |
| #77 | still-valid (low) | Keep open. Add a CSRF-guarded `/ui/reload-config` for the reload-safe env subset. |
| #122 | still-valid (tracker) | Keep open as bench tracker. Drain 2-3 checklist items per session; note #39 partially passed. |
| #121 | still-valid (tracker) | Keep open as firmware observation umbrella. No code action until a symptom recurs instrumentably. |
| PR #137 | merge | Docs-only AI transparency policy. Verified sound. Optionally fold in the two cosmetic doc nits. |
| PR #139 | merge | deploy-dotty-pi.sh faithfully mirrors the sibling scripts. Optionally add IMAGE_TAG/node_modules/image-running guards. |
| PR #140 | changes-needed | Sound SenseVoiceOnnx provider. **Blocker:** fix the Makefile `doctor` to SKIP-when-absent (it hard-fails every default FunASR install). |
| PR #141 | **close** | De-safety fork mislabeled as a fix (blanks safety suffix, drops system messages, disables perception, adds NSFW+PII persona). Extract only the piper emoji-strip into a standalone PR. |

---

## Recommended priorities

1. **Close PR #141** (F120–F124) — it removes the child-safety guardrails and adds NSFW+PII content to a public kid-safe repo. Highest urgency; no code archaeology needed.
2. **Break the LAN→host-root chain** (G9): add a docker-socket-proxy + a shared-secret auth gate on `/xiaozhi/admin/*`, confine play-asset to the songs base (G10), and add non-root `USER` to the four Dockerfiles (G11).
3. **Fix the shipped all-in-one compose** (G18, G19, G21): add the docker.sock + xiaozhi-patches mounts and the VISION_BRIDGE_URL var, or re-scope all-in-one to OpenAICompat-only with an honest header.
4. **Close the kid-safety leaks**: namespace-guard `memory_lookup` against `person_pending` (F251), run dashboard say/start-story through `content_filter()` (F186), and pursue #138.
5. **Fix the room_view id/display_name + multi-word name mismatches** (F38/F201, F161/F202) — the entire named-greet feature silently regresses; also de-duplicate the double greeting (F39).
6. **Fix the TTS truncation bugs** (F148, F26/F27 over-count) and the OTA version comparator (F141), and add them to the coverage source set (G13, G14, G17).
7. **Un-block the event loop in the bridge** (F128, F185) — wrap blocking HTTP and convo-log parsing in `to_thread` — and exempt `/admin/` from CSRF (F129) so the operator back-channel works.
8. **Fix the dotty-pi-ext HTTP client timeout-before-body bug** (F212) — a stalled body wedges the whole voice turn — and the dead bridge perception EventSource (F226).
9. **Repair dotty_doctor** (F257 model path, F51 404-as-pass, F216 Piper config) so `make doctor` is trustworthy on a standard deploy.
10. **Clean the monitoring dashboard + ZeroClaw/tool-count doc drift** (G1–G4, F76/F77, the "five vs seven tools" sweep) so the docs and the Grafana import match reality.
