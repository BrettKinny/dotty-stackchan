---
title: Vision & Alignment Review (2026-05-29)
description: Whole-repo vision and alignment snapshot reconciling README, CLAUDE.md, docs, ROADMAP, CHANGELOG, all four service trees, firmware, scripts, and tests after the post-#36 / #115 drift.
---

# Dotty — Whole-Repo Vision & Alignment Review

*Snapshot: 2026-05-29 · Branch `main` · Reconciles `README.md`, `CLAUDE.md`, `docs/*`, `ROADMAP.md`, `CHANGELOG.md`, all four service trees, firmware, scripts, and tests.*

---

## 0. Decisions Locked (2026-05-29, Brett)

The six vision ambiguities from §3 were resolved by the maintainer. These now **override** the open-question framing below — §3 is kept for the rationale, but the answers are authoritative:

- **A. story_time + security backing paths → both still PENDING (Phase 7/8).** `SecurityCycle` exists in `dotty-behaviour` but is treated as *scaffolding, not a live path*. Docs must mark both story_time and security as unimplemented; the dashboard's security ring reads scaffolding (L10) and should say so.
- **B. Canonical consumer count → 11 classes, runtime config-gated.** State "11 consumer classes" with the running set noted as env-gated; enumerate by class name everywhere a count appears.
- **C. Kid-safety gap → content filter only.** The `build_turn_suffix(kid_mode)` sandwich *ships* on the live PiVoiceLLM path; the gap (#22) is specifically the absent blocked-words **content filter** (exists in no live code). Restate all kid-safety docs accordingly.
- **D. Cloud egress → vision/VLM path, swappable.** The only LAN egress today is the vision/VLM call via `dotty-behaviour` (default `openrouter.ai`, repointable to a local VLM). The text LLM is fully local; smart-mode LLM model-swap is v2/unwired. State the self-host invariant this way.
- **E. Tier1Slim → REMOVE ENTIRELY.** Delete the provider, its tests, and its docs. The vision's "two alternate fallback providers" framing collapses to: PiVoiceLLM (default) + OpenAICompat (alternate). Affects M4, M5, L5, and `docs/tier1slim.md`.
- **F. Dashboard self-update/restart/reboot actions → REMOVE.** Drop the `update_bridge`/`restart_bridge`/`reboot_all` handlers and their UI buttons (M6, M7); rely on `restart: unless-stopped` + `deploy-bridge-unraid.sh`.

---

## 1. Canonical Vision (Ground Truth)

### 1.1 One-sentence vision

Dotty is a fully self-hosted, kid-safe-by-default voice assistant for the M5Stack **StackChan** desktop robot, where every seam (ASR, TTS, LLM, agent, firmware) is swappable and nothing leaves the LAN except an explicitly-routed, replaceable LLM/VLM call.

### 1.2 Canonical architecture

**Two hosts:** the **robot** (StackChan, ESP32-S3, LAN WiFi only) and a **single Docker host** (`<XIAOZHI_HOST>`) running all four server-side containers.

| Service | Role / owns | Authoritative port |
|---|---|---|
| **xiaozhi-esp32-server** | Voice I/O pipeline: VAD → ASR (FunASR SenseVoiceSmall / WhisperLocal) → LLM provider → TTS (LocalPiper default; EdgeTTS alternate). Emotion dispatch, OTA, the `/xiaozhi/admin/*` live-session control surface, the perception event relay. | **8000** (WS), **8003** (OTA/HTTP) |
| **dotty-pi** | The brain. A `pi` coding agent + `dotty-pi-ext` extension; owns the agent loop and tool dispatch. Reached only via `docker exec -i dotty-pi pi --mode rpc` (JSONL over stdio). | (no host port; stdio exec) |
| **dotty-behaviour** | Perception event bus, ambient consumers, vision/audio **data** endpoints, proactive greeter, calendar context. Successor to the bridge's perception role. **Serves dashboard data; does not host the dashboard UI.** | **8090** |
| **bridge.py** | Admin **dashboard only** (FastAPI, `/ui`). Localhost-only `/admin/*` mutation routes. | **8081** |

**Voice path (default `PiVoiceLLM`, selected via `selected_module.LLM` in `data/.config.yaml`):**
robot → (Opus/WS) → xiaozhi-server (VAD/ASR → text) → `PiVoiceLLM` → `docker exec` into **dotty-pi** → pi outer loop on `qwen3.5:4b` (`--thinking off`), `think_hard` escalates to `qwen3.6:27b-think` → TTS-bound text streams back → xiaozhi-server strips the leading emoji into an emotion frame, runs TTS → audio + face to robot. **PiVoiceLLM enforces the kid-safety sandwich** via `_wrap_with_sandwich()` → `build_turn_suffix(kid_mode)` (`pi_voice.py:94-132`).

**Voice tools live in `dotty-pi-ext` (`src/index.ts:21-27`), and there are SEVEN registered:** `memory_lookup`, `remember`, `recall_person`, `remember_person`, `think_hard`, `take_photo`, `play_song`. (`recall_person`/`remember_person` were added in #53; the historical "five voice tools" framing predates them. There is no `set_led` tool — LED control is firmware-owned by design.)

**Perception path:** firmware emits JSON `event` frames (`face_detected`, `face_lost`, `sound_event`, `state_changed`, `head_pet_started`/`head_pet_ended`, `chat_status`, `dance_started`/`dance_ended`). xiaozhi-server's `EventTextMessageHandler` (`custom-providers/xiaozhi-patches/textMessageHandlerRegistry.py`) POSTs each to **dotty-behaviour** `/api/perception/event`. dotty-behaviour runs **11 consumer classes** (`dotty-behaviour/consumers/`): `face_greeter`, `sound_turner`, `face_lost_aborter`, `wake_word_turner`, `face_identified_refresher`, `purr_player`, `scene_synthesis`, `idle_photographer`, `sleep_dreamer`, `dance_reflector`, `security_cycle` (count is env-gated at runtime; the *class* count is the canonical figure). The #115 series rewired the **dashboard** to pull perception/vision/audio cards from dotty-behaviour.

**WS lifecycle invariant:** xiaozhi only opens the WS during a conversation. Idle perception producers must call `OpenAudioChannel()` first (done in firmware `Application::SendEvent`) or events silently drop.

### 1.3 Known-good invariants (treat as spec)

1. **Emoji-prefix requirement** — every LLM response starts with one of 9 mapped emojis (😊😆😢😮🤔😠😐😍😴). On the live PiVoiceLLM path there is **no code fallback**; the persona prompt + xiaozhi `prompt:` block are load-bearing. (The old `_ensure_emoji_prefix` fallback was ZeroClaw-only and is gone.)
2. **Six-state mutex, firmware-owned** — exactly one of `idle / talk / story_time / security / sleep / dance` (`state_manager.{h,cpp}`).
3. **Two orthogonal toggles** — `kid_mode`, `smart_mode`; dashboard/admin-only, sticky across turns/reboots.
4. **12-pixel LED contract** — left ring 0–5 state arc; right ring 6/8/9/11 indicators, 7/10 reserved; 5 Hz re-assert. `docs/modes.md` is authoritative.
5. **Self-host invariant** — only explicitly-routed cloud LLM/VLM/audio-caption calls (OpenRouter when smart_mode/vision active) and EdgeTTS-if-selected leave the LAN.
6. **`/admin/*` mutation endpoints are `127.0.0.1`-only** on bridge.py (`_admin_require_localhost`, `bridge.py:864-896`).
7. **The brain seam is a custom xiaozhi LLM provider** (`custom-providers/pi_voice/`), so the brain is swappable without touching xiaozhi.
8. **`:8080` is the llama-swap endpoint, never the dashboard** — the dashboard is `:8081` everywhere.

### 1.4 Retired as of #36 (2026-05-19)

ZeroClaw (Rust brain) + its FastAPI bridge on the RPi; the ACP protocol; the `ZeroClawLLM` provider (`custom-providers/zeroclaw/` removed); `docs/multi-daemon-split.md`, `docs/advanced/multi-host.md`; the `_ensure_emoji_prefix` bridge fallback; the `zeroclaw-bridge` systemd unit; the RPi host. References to these are **legitimate only** in `CHANGELOG.md` history and `docs/cutover-behaviour.md`. Anywhere else they are stale and should be flagged.

---

## 2. Executive Summary

**Overall alignment health: GOOD with a long tail of post-#36 doc/code rot.** The production architecture (four containers, PiVoiceLLM voice path, dotty-behaviour perception) is coherent and the code largely matches the vision. The defects are concentrated in two patterns: (1) **retired-ZeroClaw references** that survived the #36 cutover in live code, scripts, tests, and docs; (2) **the `:8080` vs `:8081` dashboard-port slip** propagated corpus-wide. There are **no critical (production-breaking) defects on the live voice or perception path** — the highest-impact issues are in operator tooling (`make doctor`/`status`/`audit`), cross-container state-file wiring, and an authoritative doc (`modes.md`) being stale.

**Issue counts (post-verification, adjusted severities; duplicates merged):**

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 3 |
| Medium | 14 |
| Low | 22 |

All findings below are **confirmed** against the working tree.

---

## 3. Vision Ambiguities to Resolve (Brett decides)

These are genuine product/architecture decisions the audit cannot make. Each is framed as a concrete choice.

**A. Story_time & security backing-path liveness.**
`docs/modes.md:176-179` routes `story_time` through `bridge → direct OpenRouter` and `security` through a "bridge ambient task" — but #36 retired bridge.py's voice/perception roles, and `bridge/security_watch.py` is dead code (never started). **Decide:** (a) these moved to dotty-behaviour (then `modes.md` is stale and `security_cycle.py` is the live path), or (b) they are genuinely unimplemented (Phase 7/8 pending, #26). The security panel's empty-cycles behaviour and the `modes.md` source-of-truth table both hinge on this.

**B. Canonical ambient-consumer count.**
Code registers **11 consumer classes** (`dotty-behaviour/consumers/`); docs variously say "9" (architecture.md, protocols.md, dotty-behaviour/README.md) or "six" (modes.md, ROADMAP.md), and no enumeration matches reality. **Decide:** adopt **11** as canonical (or state "up to N, config-gated" with the full class list), then propagate. The vision §1.2 above now uses 11.

**C. Does "kid-safety not on the live path" still hold?**
The vision's stub note says PiVoiceLLM has "no equivalent enforcement layer," but `pi_voice.py:94-132` *does* apply the `build_turn_suffix(kid_mode)` sandwich. **Decide:** is the #22 gap the *content filter* (blocked-words regex, genuinely absent) and not the sandwich (present)? If so, restate the gap precisely so the stub status stops contradicting shipped code.

**D. Does any OpenRouter egress occur on the live PiVoiceLLM path today?**
Invariant §1.3.5 names OpenRouter as cloud egress for smart_mode/vision, but smart-mode model-swap is unimplemented on the default path (v2 scope). **Decide:** confirm whether OpenRouter is reached at all on PiVoiceLLM today (e.g. only via vision-explain?), or only on the retired ZeroClaw/Tier1Slim paths — so the self-host invariant is stated accurately.

**E. Tier1Slim escalation — revive or retire?**
Tier1Slim still POSTs to the dead `/api/voice/escalate` (`:8080`). **Decide:** mark it permanently chitchat-only (then fix docstring + tests to say so), or re-point escalation at a live endpoint.

**F. In-app dashboard self-update — keep or drop?**
The `/ui` Update/Restart/Reboot-All actions assume the RPi/systemd model. **Decide:** drop them on the container deploy, or reimplement via `docker`/compose.

---

## 4. Findings by Severity

### HIGH

#### H1. `make doctor`/`status`/`audit`/`setup` treat the retired ZeroClaw RPi bridge (`:8080`) as a live service — and probe the wrong host
- **Files:** `Makefile:122-124, 340, 352-362, 406-428, 527-534`; `scripts/dotty_doctor.py:4-5, 94-97, 174-183`
- **What's wrong:** `setup` prompts for `ZEROCLAW_HOST`/`ZEROCLAW_USER` (default `dietpi`); `doctor`/`status` extract a host via `grep -oP 'url: http://\K[0-9.]+'` and curl `http://$ZEROCLAW_HOST:8080/health` labelled "Bridge health"; `audit` SSHes `dietpi@$ZEROCLAW_HOST`. Post-#36 the only `url: http://` block in the rendered config is the **llama-swap** Tier1Slim endpoint — so the regex extracts the llama-swap IP and the "Bridge health" check actually probes llama-swap. No Makefile reference points at `:8081` or `:8090`.
- **Why it matters:** This is live operator tooling. `make doctor`/`status` report a misleading PASS/FAIL against an unrelated service; `make setup` forces users to enter a nonexistent host; `make audit` SSHes a powered-off RPi. The exact `:8080` conflation the vision warns about, now actively mis-probing.
- **Fix (code):** Drop the ZeroClaw prompts/extraction. Health-check `http://<XIAOZHI_HOST>:8081/health` (bridge) and add `http://<XIAOZHI_HOST>:8090/health` (dotty-behaviour). Apply the identical fix to `scripts/dotty_doctor.py` (the finding's "mirror the Makefile fix" wording is inaccurate — both files share the bug; neither is fixed yet).

#### H2. Kid/smart-mode state-file contract is broken across the xiaozhi and bridge containers
- **Files:** `receiveAudioHandle.py:27-32`; `bridge/dashboard.py:1434`; `docker-compose.yml.template:30, 61`; `compose.all-in-one.yml`; `bridge/docker-compose.yml:51-52, 62-63`
- **What's wrong:** Inside the xiaozhi container, `receiveAudioHandle.py` *reads* kid/smart-mode from `/root/zeroclaw-bridge/state/{kid-mode,smart-mode}` (env-overridable via `DOTTY_KID_MODE_STATE`/`DOTTY_SMART_MODE_STATE`). The bridge container *writes* under `DOTTY_BRIDGE_DIR` (default `/root/zeroclaw-bridge`), and `bridge/docker-compose.yml` correctly redirects it to `/var/lib/dotty-bridge/state/…`. But the **xiaozhi compose sets neither env var nor a shared volume** — so the reader falls through to a dead RPi path that doesn't exist in the container, and reads its env/default (`kid_mode` → `true`, `smart_mode` → `false`) instead of the dashboard's persisted state.
- **Why it matters:** Producer and consumer point at disjoint, non-shared locations. The firmware kid/smart pips desync from the dashboard on every WS reconnect/restart — violating the "toggles sticky across reboots" invariant in practice. (Mitigating: the kid-mode fall-through default is `true`, i.e. toward safety; `_write_smart_mode_state` is dead on the xiaozhi side, so no orphaned-dir write actually occurs.)
- **Fix (code):** Pick one shared state location (named volume mounted into **both** containers), set `DOTTY_KID_MODE_STATE`/`DOTTY_SMART_MODE_STATE` in the xiaozhi compose and `DOTTY_BRIDGE_DIR` in the bridge compose to point at it, and stop defaulting to `/root/zeroclaw-bridge`. Add a `make doctor` check that the write path and read path resolve to the same mount.

#### H3. Brain tool-count contract disagrees everywhere — code registers 7, all docs say 5, and one README invents a tool
- **Files:** `dotty-pi-ext/src/index.ts:21-27`; `dotty-pi/README.md:20-22`; `dotty-pi-ext/README.md:8, 14-27, 78-96`; `dotty-pi/docker-compose.yml:8`; `dotty-pi-ext/package.json:4, 20-22`; `CLAUDE.md` (architecture diagram + dotty-pi-ext bullet)
- **What's wrong:** `index.ts` registers **7** tools (`memory_lookup`, `recall_person`, `remember`, `remember_person`, `think_hard`, `play_song`, `take_photo`). Every doc says "five voice tools." Worse, `dotty-pi/README.md:20-22` lists a **non-existent `set_led`** as shipped (which the sibling `dotty-pi-ext/README.md:48-56` explicitly documents as *not* a tool, and which would violate the firmware-owned-LED invariant) and **omits the real `remember`**. `package.json` even has `test:recall`/`test:rememberperson` scripts (lines 20-22) for tools its own description omits. The "five" framing was inherited into this vision doc's §2 too.
- **Why it matters:** A maintainer reading the deploy doc would believe a tool exists that doesn't (and contradicts a stated invariant), and would miss two real tools and the live `remember`.
- **Fix (doc):** Remove `set_led` from `dotty-pi/README.md`, restore `remember`. Update `dotty-pi-ext/README.md` table + "5 of 5" status, `docker-compose.yml:8`, `package.json:4` description, the `(planned)` layout block (`README.md:78-96` still shows `set_led.ts` and stale `lib/` names), and `CLAUDE.md` to the canonical 7 (or "the original five + the #53 person pair"). Refresh the layout tree to the real `src/` files.

---

### MEDIUM

#### M1. Bridge dashboard documented at `:8080` across multiple active docs
- **Files:** `SETUP.md:23, 244, 245`; `docs/quickstart.md:210, 243`; `docs/observability.md:31, 48`; `COMPATIBILITY.md:62`; `docs/advanced/variant-port-guide.md:185`; `compose.all-in-one.yml:9`; `CLAUDE.md:29`; `CHANGELOG.md:13`
- **What's wrong:** Authoritative dashboard port is **8081** (`bridge.py:1` docstring + `:1040` PORT default, `bridge/Dockerfile:45/47`, `bridge/docker-compose.yml:43`, `deploy-bridge-unraid.sh`). These docs/configs still say `:8080` for the dashboard URL, health check, `/metrics` scrape, and (in `variant-port-guide.md:185`) POST to the *retired* `/api/message` endpoint. `quickstart.md:210` even contradicts its own line 209 (which correctly says 8081). `CLAUDE.md:29` prose says `:8080` while its own Ports table (`:63`) says 8081. `bridge/docker-compose.yml:24-25` documents the exact 8080→8081 correction that these were supposed to follow.
- **Why it matters:** Copy-pasted health/metrics/dashboard commands hit connection-refused; `variant-port-guide.md` doubly points at a dead endpoint.
- **Fix (doc):** Change every **dashboard** `:8080` → `:8081`. Leave `:8080` intact where it is the llama-swap endpoint (`llm-backends.md`, `tier1slim.md` `TIER1SLIM_LOCAL_URL`, `llama-swap-concurrent-models.md`, `CHANGELOG.md:39`). Correct `CHANGELOG.md:13`, `CLAUDE.md:29`, `compose.all-in-one.yml:9`.

#### M2. `docs/modes.md` (the authoritative behaviour doc) routes voice/perception through retired bridge.py
- **Files:** `docs/modes.md:48, 176-177, 179, 193, 202`; `docs/architecture.md:206-216`; `bridge.py`
- **What's wrong:** `modes.md` "Sources of truth" cites bridge.py methods that no longer exist (`_apply_model_swap`, `_apply_tier1slim_runtime`, `_update_perception_state`, `_capture_room_view`, "all `_perception_*` consumers" — all 0 occurrences in the current 1042-line `bridge.py`), and routes `story_time`/`security`/`dance` through bridge.py. `architecture.md:206-216` labels dotty-behaviour consumers with the same stale `_perception_*` method names and a wrong "3 additional consumers" list.
- **Why it matters:** `modes.md` is the doc the whole system treats as canonical for states/toggles/LED/transitions — and it is the one most stale about where perception lives.
- **Fix (doc):** Re-attribute consumers/admin mutations to dotty-behaviour by **class name** (`FaceGreeter`, `SoundTurner`, …) and `perception/state.py`; resolve the story_time/security backing path per Ambiguity A.

#### M3. Retired `_ensure_emoji_prefix`/sandwich machinery documented as the live enforcement mechanism (multi-doc)
- **Files:** `docs/emoji-mapping.md:32-46`; `docs/troubleshooting.md:33-34`; `docs/kid-mode.md:299-306`; `bridge.py`
- **What's wrong:** `emoji-mapping.md` presents `bridge.py::_ensure_emoji_prefix()` as the live fallback; `troubleshooting.md` tells users to verify `bridge.py::_build_sandwich_prompt` and "tail the bridge logs" to fix Chinese responses; `kid-mode.md` lists `_wrap_voice`, `_content_filter`, `_BLOCKED_WORDS_RE`, `ACPClient.prompt()`, `on_chunk` as current bridge.py code. All these symbols are **0 occurrences** in the current `bridge.py` — they were the ZeroClaw path. `protocols.md:157` and `voice-pipeline.md:120` already say the fallback is gone, so the corpus is self-inconsistent.
- **Why it matters:** Contradicts the load-bearing no-code-fallback invariant (§1.3.1) and misdirects troubleshooting at a service not in the voice path. `troubleshooting.md` and `emoji-mapping.md` are the worst because they imply a safety net that doesn't run.
- **Fix (doc):** Rewrite all three to point at the persona prompt + `.config.yaml prompt:` + `custom-providers/textUtils.py` (`build_turn_suffix`, `EMOJI_MAP`, `get_emotion`). State explicitly the PiVoiceLLM path has no code emoji fallback. Reconcile the `personas/dotty_voice.md` vs `personas/default.md` naming while editing.

#### M4. Tier1Slim is live-selectable but its escalation/memory calls and docstring describe the retired ZeroClaw bridge at `:8080`
- **Files:** `custom-providers/tier1_slim/tier1_slim.py:9-18, 41, 356-396`
- **What's wrong:** Module docstring: "POSTs each tool call to bridge.py's `/api/voice/escalate` … which dispatches to ZeroClaw memory"; `BRIDGE_URL` defaults to `http://localhost:8080`; `_dispatch_tool`/`_post_remember`/`_post_memory_log` POST to dead endpoints. Endpoints were retired in #36; dashboard is `:8081`.
- **Why it matters:** Live, user-selectable code presenting a dead path as current, with a doubly-wrong default. Misleads anyone treating Tier1Slim as functional.
- **Fix (code/doc, pending Ambiguity E):** Add a docstring banner that escalation is non-functional post-#36 (chitchat-only rollback); correct `:8080` → `:8081` if kept; remove "dispatches to ZeroClaw memory."

#### M5. `tests/test_tier1_slim.py` asserts the ZeroClaw escalation handshake as a live contract
- **Files:** `tests/test_tier1_slim.py:1-13, 279-310, 408-459`
- **What's wrong:** Docstring claims "tier1_slim.py is the live voice path"; `DispatchToolTests` asserts `/api/voice/escalate` is hit; `ResponseToolPathTests.test_escalation_chains_to_streaming_final` asserts escalation succeeds. PiVoiceLLM is the live path; Tier1Slim escalation is dead.
- **Why it matters:** A green test gives false confidence that a retired handshake works.
- **Fix (test):** Fix the "live voice path" docstring (it's PiVoiceLLM). `xfail`/mark `DispatchToolTests` + `ResponseToolPathTests` as covering a retired rollback path, or restrict to the chitchat fast-path.

#### M6. Dashboard self-restart/update/reboot actions invoke the retired `zeroclaw-bridge` systemd unit
- **Files:** `bridge/dashboard.py:1741, 1766, 1893`
- **What's wrong:** `update_bridge`, `restart_bridge`, `reboot_all` all spawn `systemctl restart zeroclaw-bridge`. The container has no systemd and no such unit (`CMD ["python","bridge.py"]` under tini). Because the commands are `sh -c`-wrapped, `Popen` succeeds and the handlers report success while the inner call silently fails.
- **Why it matters:** Restart/Update/Reboot-All buttons no-op while reporting success. Admin-only, behind auth, not on the production path.
- **Fix (code, pending Ambiguity F):** Exit the process and rely on `restart: unless-stopped`, or remove these self-mutation actions, or gate behind a config flag.

#### M7. Dashboard self-update git-pulls into the retired RPi install dir `/root/zeroclaw-bridge`
- **Files:** `bridge/dashboard.py:1434, 1705-1715`
- **What's wrong:** `BRIDGE_INSTALL_DIR` defaults to `/root/zeroclaw-bridge`; `_pull_and_install_bridge` copies `bridge.py` + `bridge/` over it and expects a service restart. Under the container deploy the real source is the immutable `/app` image layer; copying into `/root/zeroclaw-bridge` does nothing and the image is never rebuilt. Live deploy is `deploy-bridge-unraid.sh` (build + `compose up -d`).
- **Why it matters:** Exposed-but-dead `/ui/actions/update-bridge` reports success while doing nothing.
- **Fix (code):** Remove/disable the in-app self-update on the container deploy; keep a status-only "update available" chip if wanted (drop the install+restart half).

#### M8. `dotty-pi-ext` README + `turn_logger.ts` describe the #36 cutover as still pending
- **Files:** `dotty-pi-ext/README.md:8-12`; `dotty-pi-ext/src/lib/turn_logger.ts:5-7`
- **What's wrong:** README: "Bridge.py is still the source of truth in production until the … cutover (#36)…"; `turn_logger.ts`: "Pre-cutover this code is dormant … Post-cutover, when xiaozhi flips to PiVoiceLLM, this is the last write path…". #36 executed 2026-05-19; PiVoiceLLM is the live default and this turn-logger is the *active* write path.
- **Why it matters:** Labels the live write path "dormant," misleading a maintainer of the PiVoiceLLM path.
- **Fix (doc):** Update both to post-#36 reality.

#### M9. `dotty-behaviour` README + app description claim it hosts the admin dashboard
- **Files:** `dotty-behaviour/README.md:3-6, 83`; `dotty-behaviour/main.py:347-351`
- **What's wrong:** README opener and the FastAPI description say dotty-behaviour "hosts the admin dashboard." It mounts only health/perception/vision/audio/voice/calendar/scene_synthesis routers — no dashboard router/templates/static. Its own in-code comments correctly say "the bridge dashboard" consumes its data. README.md:83 also lists a future "Dashboard (ported from bridge/dashboard.py)" slice that contradicts the #115 decision (dashboard stays in bridge.py, pulls data from dotty-behaviour).
- **Why it matters:** Two services claim the same role; bridge.py-is-dashboard-only is a vision invariant.
- **Fix (doc):** Reword to "serves perception/vision/audio data endpoints consumed by the bridge dashboard"; drop/mark-abandoned the Dashboard slice row.

#### M10. `dotty-pi/README.md` lists `set_led` and omits `remember`
*(Merged into H3.)*

#### M11. `docs/brain.md` states smart-mode model-swap as live on the default path
- **Files:** `docs/brain.md:13`
- **What's wrong:** Unqualified TL;DR bullet "Smart-mode flips the inner-loop model to a cloud model," under a section naming PiVoiceLLM the default. `modes.md:155, 181` correctly caveat this as v2 scope for PiVoiceLLM (instantaneous only with `DOTTY_VOICE_PROVIDER=tier1slim`). brain.md is internally inconsistent (its own model matrix attributes the swap to Tier1Slim rows).
- **Why it matters:** Propagates a wrong "live" claim for a v2/unimplemented feature.
- **Fix (doc):** Add the modes.md v2-scope caveat to the bullet.

#### M12. `docs/voice-mode-entry.md` attributes face-greeter to bridge.py and references a removed Discord daemon
- **Files:** `docs/voice-mode-entry.md:24, 26, 42`; `bridge.py`
- **What's wrong:** "the relay forwards that event to the bridge, where `_perception_face_greeter` (in `bridge.py`)…" (0 occurrences in bridge.py); "see the env block in `bridge.py` for the full set of knobs" (knobs `FACE_GREET_TEXT`/`FACE_GREET_MIN_INTERVAL_SEC` now live in `dotty-behaviour/config.py:277-281`); "the Discord daemon and the portal 'Greet' button" (the Discord *daemon* was removed — `bridge/templates/discord.html` is deleted per git status; the surviving `community/discord/provision.py` is an unrelated provisioning bot).
- **Why it matters:** Misdirects anyone configuring the face-greeting path to the wrong service and file.
- **Fix (doc):** Repoint to `dotty-behaviour/consumers/face_greeter.py` + `config.py` knobs; remove the Discord-daemon reference.

#### M13. `mkdocs.yml` nav omits `modes.md` (the authoritative doc) and 7 others
- **Files:** `mkdocs.yml:39-80`; orphans: `docs/modes.md`, `docs/quickstart.md`, `docs/tier1slim.md`, `docs/wake-word.md`, `docs/proactive-greetings.md`, `docs/cutover-behaviour.md`, `docs/speaker-id-investigation.md`, `docs/cookbook/llama-swap-concurrent-models.md`
- **What's wrong:** Eight docs are present but absent from the nav (and there's no `not_in_nav`/exclude mechanism — MkDocs Material warns per orphan). `modes.md` and `quickstart.md` are cross-linked from in-nav `docs/README.md` yet unreachable via site nav.
- **Why it matters:** The doc the vision designates authoritative isn't in the published site.
- **Fix (doc):** Add `modes.md`, `tier1slim.md`, `wake-word.md`, `quickstart.md`, `proactive-greetings.md`, and the llama-swap cookbook page to nav. `cutover-behaviour.md` and `speaker-id-investigation.md` may stay out as historical/investigation notes (document why).

#### M14. `bridge.py` `/admin/*` localhost-only gate has no test coverage
- **Files:** `bridge.py:864-896`; `tests/test_dashboard_csrf.py`; `tests/test_bridge_routes.py`
- **What's wrong:** `_admin_require_localhost` (403 for non-loopback) is a router-level dependency on every `/admin/*` route — invariant §1.3.6 — but no test asserts a spoofed non-loopback `client.host` gets 403, nor that loopback passes.
- **Why it matters:** A security invariant guarded only by code, with zero regression test.
- **Fix (test):** Add a test POSTing to an `/admin/*` route with a spoofed non-loopback `request.client.host` (assert 403) plus a loopback case (assert pass).

---

### LOW

| # | Title | Files | Fix |
|---|---|---|---|
| L1 | `pi_voice` README "What's not yet done" lists the sandwich as unimplemented, but it ships (`_wrap_with_sandwich`) and the provider is the live default | `custom-providers/pi_voice/README.md:21-28, 148-164` | Delete the stale "not yet done"/"open questions" sections; keep only the genuinely-open memory write-back / persona items |
| L2 | `pi_voice` README diagram cites `qwen3.6:27b`/`--thinking minimal`/`--extensions`; actual flags are `qwen3.5:4b`/`--thinking off`, no `--extensions` | `README.md:40-43`; `pi_client.py:49-60` | Fix the diagram to `qwen3.5:4b`/`--thinking off`, drop `--extensions` |
| L3 | `textUtils.py` header lists removed `zeroclaw.py` as an active importer | `custom-providers/textUtils.py:10-21` | Drop the zeroclaw bullet; list `pi_voice`, `tier1_slim`, `openai_compat` |
| L4 | `openai_compat._ensure_emoji_prefix` is dead code (never called; live logic is inline) | `custom-providers/openai_compat/openai_compat.py:51-58` | Remove or wire it in |
| L5 | `kid_mode` is a process-start snapshot in tier1_slim/openai_compat with no doc warning; `set_runtime` doesn't re-derive the suffix | `tier1_slim.py:38-39, 193`; `openai_compat.py:27-28`; `pi_voice.py:111-114` | Mirror the pi_voice comment into both; note `set_runtime` doesn't hot-swap kid_mode |
| L6 | Perception relay docstring names the retired `zeroclaw-bridge` as relay target | `xiaozhi-patches/textMessageHandlerRegistry.py:1-8` | Reword to dotty-behaviour `/api/perception/event` |
| L7 | `ota_handler` comment cites `vision_explain` pointing at the ZeroClaw host | `xiaozhi-patches/ota_handler.py:322-327` | Reword to dotty-behaviour `:8090` |
| L8 | `CLAUDE.md:75` misdescribes the xiaozhi-admin route set (`kid-mode`/`smart-mode`/`tts` routes don't exist) and patch-file inventory | `CLAUDE.md:75`; `http_server.py:680-735` | Match the implemented routes (per `docs/architecture.md:184-194`); list `ota_handler.py` + `textMessageHandlerRegistry.py` |
| L9 | `SimpleHttpServer._get_websocket_url` is dead code (and logic-drifted from the live `OTAHandler` twin) | `http_server.py:33-40` | Remove |
| L10 | `bridge/security_watch.py` consumer loop is dead (ported to dotty-behaviour); dashboard reads its permanently-empty ring | `bridge/security_watch.py:108`; `bridge/dashboard.py:2478`; `dotty-behaviour/consumers/security_cycle.py:16` | Route cycles through a dotty-behaviour getter, or drop the dead read |
| L11 | `bridge.py:319` cites non-existent `bridge/MEMORY-INDEX.md` / `brain-db-fts-only.md` | `bridge.py:319` | Drop the dangling pointer |
| L12 | Consumer count wrong in `dotty-behaviour` README/main.py (says 9, code has 11; README slice-table drops `sound_turner`, `face_identified_refresher`) | `dotty-behaviour/README.md:4, 80`; `main.py:349`; `consumers/__init__.py:31` | Adopt 11; fix the slice table *(see Ambiguity B)* |
| L13 | `architecture.md` consumer table uses stale bridge.py `_perception_*` names + wrong "3 additional" list | `docs/architecture.md:206-216` | Rename to dotty-behaviour classes; list real extras *(merge with M2)* |
| L14 | `household/registry.py` docstring default `~/.zeroclaw/household.yaml` + "running ZeroClaw host"; real default is `STATE_DIR/household.yaml` | `dotty-behaviour/household/registry.py:3, 5, 19`; `config.py:298-300` | Fix docstring to the dotty-behaviour state-dir path |
| L15 | `perception.py` ingress docstring omits `head_pet_ended` (firmware emits it) | `dotty-behaviour/routes/perception.py:53-56`; `firmware/.../head_pet.cpp:78` | Add `head_pet_ended` |
| L16 | Tool count 5-vs-7 in dotty-pi-ext README/compose/package.json | `dotty-pi-ext/README.md:8, 14-27`; `docker-compose.yml:8`; `package.json:4` | *(merge with H3)* |
| L17 | dotty-pi-ext README "(planned) layout" shows `set_led.ts`, omits person-tool/lib files | `dotty-pi-ext/README.md:78-96` | Refresh tree or drop "(planned)" *(merge with H3)* |
| L18 | `docs/kid-mode.md` "Where the Code Lives" table lists retired ZeroClaw symbols | `docs/kid-mode.md:299-306` | Point at `build_turn_suffix`/textUtils (Tier1Slim path); note PiVoiceLLM layers 1+2 *(relates to M3)* |
| L19 | `compose.all-in-one.yml:9` labels dashboard "port 8080" | `compose.all-in-one.yml:9` | `8080` → `8081` *(merge with M1)* |
| L20 | `deploy-bridge-unraid.sh` header references `deploy-bridge.sh`/`install-bridge.sh` which no longer exist | `scripts/deploy-bridge-unraid.sh:9-14` | Update/drop the comment |
| L21 | `compose.local.override.yml` attaches ollama to undefined `dotty` network — offline-stack `up` fails at parse | `compose.local.override.yml:29-30`; `compose.all-in-one.yml` | Add top-level `networks: { dotty: {} }` or drop the block |
| L22 | `style.md` teaches `../protocols.md` for a sibling doc (broken link) | `docs/style.md:72` | Change to `./protocols.md` |
| L23 | `textUtils` emoji-parse machinery (`get_emotion`/`EMOJI_MAP`/`check_emoji`) has no test | `custom-providers/textUtils.py:64-176` | Add `test_textutils.py` pinning `EMOJI_MAP` vs the 9-emoji `ALLOWED_EMOJIS` set + basic parsing |
| L24 | `test_security_watch.py` tests the superseded bridge security loop | `tests/test_security_watch.py:163-204` | Drop `StateGatedConsumerTests`/`RunCaptureCycleTests` (live coverage is `dotty-behaviour/tests/test_consumer_security_cycle.py`); keep `get_recent_cycles` tests |
| L25 | `test_dashboard_csrf.py` probes `/api/perception/event` (removed from bridge.py) with a stale comment | `tests/test_dashboard_csrf.py:161-170`; `bridge.py:504` | Repoint at a live `/api/*`/`/metrics` route; fix comment |
| L26 | `ZEROCLAW_BIN` env stub in CSRF test bootstrap (ACP spawn path removed) | `tests/test_dashboard_csrf.py:34` | Remove the `setdefault` |
| L27 | `.env.example` carries a stale `zeroclaw-bridge` section: `ZEROCLAW_BIN`, `PORT=8080`, ACP `ZEROCLAW_*`, `~/.zeroclaw/` paths | `.env.example:16-37, 98, 122, 169, 193-194` | Remove ZeroClaw/ACP vars; `PORT=8080`→`8081`; repoint `~/.zeroclaw/` to dotty-behaviour/dotty-bridge state dirs |

---

## 5. Cross-Cutting Inconsistencies

Three patterns recur across components and account for most findings:

**(a) Retired-ZeroClaw references in live surfaces.** The RPi bridge / `zeroclaw-bridge` systemd unit / `/root/zeroclaw-bridge` path / `/api/voice/escalate` endpoint / ACP env vars survive in: `make doctor/status/audit/setup` (H1), `scripts/dotty_doctor.py` (H1), the cross-container state files (H2), dashboard self-update/restart actions (M6, M7), Tier1Slim (M4) + its tests (M5), `.env.example` (L27), and ~10 docstrings/comments (L3, L6, L7, L14) and docs (M2, M3, M12, L18). The **`dotty-deploy-bridge` skill is itself stale** — it still pushes to `dietpi@<ZEROCLAW_HOST>:/root/zeroclaw-bridge/` and restarts `zeroclaw-bridge`; the live deploy is `scripts/deploy-bridge-unraid.sh`. **Retire or rewrite that skill.**

**(b) The `:8080` dashboard-port slip.** `:8080` is correct *only* for llama-swap; it is wrong for the dashboard (canonically `:8081`) in `CHANGELOG.md:13`, `CLAUDE.md:29` prose, `compose.all-in-one.yml:9`, `SETUP.md`, `quickstart.md`, `observability.md`, `COMPATIBILITY.md`, `variant-port-guide.md`, and `.env.example` (M1, L19, L27). A single sweep should fix all dashboard mentions while preserving llama-swap `:8080`.

**(c) Stale perception ownership + consumer counts.** Post-#36 perception lives in `dotty-behaviour/consumers/` (11 classes), but `modes.md`, `architecture.md`, `protocols.md`, `ROADMAP.md`, and `dotty-behaviour/README.md` variously say "six"/"9", use bridge.py `_perception_*` method names, drop real consumers, or (M9) claim dotty-behaviour hosts the dashboard. Pick the canonical count once (Ambiguity B) and propagate by class name.

---

## 6. Recommended Remediation Order

**Phase 0 — Brett's decisions (unblocks the rest).** Resolve Ambiguities A–F (§3). A, B, C, E directly gate the wording of M2, L12/L13, M4/M5, and the vision's own stub/invariant text.

**Phase 1 — Code fixes that affect real behaviour (highest impact first):**
1. **H2** — wire a shared kid/smart state volume into both compose files. *(Restores toggle persistence — a stated invariant.)*
2. **H1** — fix `make doctor/status/audit/setup` + `dotty_doctor.py` to probe `:8081`/`:8090`, drop ZeroClaw host. *(Operators run these constantly.)*
3. **M6 + M7** — fix/remove the dashboard self-restart/update actions (pending Ambiguity F).
4. **L21** — add the `dotty` network so the offline-stack `up` parses.

**Phase 2 — Code/test hygiene (no behaviour change, prevents regressions):**
5. **M14** — add the localhost-gate test (security invariant guard).
6. **M5, L24, L25, L26** — fix/mark the stale tests so green ≠ false confidence.
7. **M4** — Tier1Slim docstring/default-URL banner (pending Ambiguity E).
8. **L4, L9, L10, L11** — remove dead code / dangling pointers.

**Phase 3 — Documentation sweep (largest count, lowest risk; batch the cross-cutting patterns):**
9. **Port sweep (M1, L19, L27, the doc half of the cross-cutting `:8080`):** one pass, dashboard `:8080`→`:8081`, leave llama-swap intact.
10. **ZeroClaw-reference sweep (M3, M8, M12, L3, L6, L7, L14, L18 + the `dotty-deploy-bridge` skill):** repoint to PiVoiceLLM/dotty-behaviour/textUtils; retire the skill.
11. **Authoritative-doc fixes (M2, L13):** update `modes.md`/`architecture.md` perception ownership + backing paths (depends on Ambiguity A).
12. **Brain-tool + consumer-count reconciliation (H3, L12, L16, L17, M9, M11):** propagate the canonical 7 tools and 11 consumers (depends on Ambiguity B); fix `dotty-behaviour` role wording.
13. **Smaller doc fixes (L1, L2, L5, L8, L15, L20, L22, M13):** README/diagram/nav/style cleanups.
14. **L23** — add the `textUtils` emoji-map test last (cheap spec guard).

**Doc-only vs code:** Of the 39 merged findings, **~26 are doc/comment-only** (Phase 3 + L8/L15/L22 etc.), **~9 are code/config** (H1, H2, M4, M6, M7, L4, L9, L21, plus the H3 doc-but-touches-manifests), and **~4 are test** (M5, M14, L24–L26). The doc fixes are low-risk and can be batched by the two cross-cutting sweeps; prioritize the Phase 1 code fixes because they affect live behaviour and operator trust.