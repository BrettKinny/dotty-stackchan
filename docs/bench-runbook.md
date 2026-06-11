# Bench Test Runbook — draining the bench-pending pile (epic #122)

A single ordered session plan covering all eight bench-pending issues
(#37 #38 #39 #40 #42 #43 #44 #45). Designed to be run **interactively**:
Brett at the device with a phone camera, a Claude Code session monitoring
serial + container logs and recording results.

> **AI-assistance note:** this document was drafted by an AI agent (Claude)
> from the canonical checklists in the issues above, with stale
> ZeroClaw-era references translated to the current container architecture.

**Session budget:** one full pass is ~45–60 min of device time. The epic's
goal of "2–3 items per session" also works — phases below are independent;
do them in order but stop anywhere.

---

## Roles

| Who | Does |
|---|---|
| **Brett** | Performs each step at the device. Films each check (say the check ID aloud at the start of each clip — e.g. *"B3"* — so clips map to results). |
| **Claude** | Tails serial + container logs in the background, calls out expected/missing log lines in real time, records pass/fail/notes per check, files issue stubs for failures. |

## Stale-reference translation (issues were written pre-#36 cutover)

| Issue says | Current reality |
|---|---|
| `systemctl restart zeroclaw-bridge` | `docker restart dotty-bridge` on the Docker host |
| `journalctl -u zeroclaw-bridge` | `docker logs -f dotty-behaviour` (security cycle) / `docker logs -f dotty-bridge` (dashboard) |
| `/root/zeroclaw-bridge/logs/security-*.ndjson` | `/var/lib/dotty-behaviour/logs/security-YYYY-MM-DD.ndjson` **inside the `dotty-behaviour` container** (`CONVO_LOG_DIR`) |
| `bridge/security_watch.py` consumer | `dotty-behaviour/consumers/security_cycle.py` |
| Dashboard "LED ring mirror" card (#44) | May not exist post-cutover — record what the dashboard actually shows; if the mirror card is gone, mark those sub-checks N/A-stale, not failed |

---

## Phase 0 — pre-session setup (~10 min, before touching the robot)

1. **Health:** `make doctor` — all green before starting.
2. **Versions:** note deployed git SHA (`git rev-parse --short HEAD` on the
   Docker host source dirs) and firmware build (serial boot banner). Record
   at the top of the results log.
3. **Serial monitor** (Claude, background): plug Dotty into the workstation
   via USB-C, then:
   ```bash
   docker run --rm -v "$PWD/firmware/firmware:/project" -w /project \
     --device=/dev/ttyACM0 espressif/idf:v5.5.4 \
     bash -lc 'idf.py -p /dev/ttyACM0 monitor'
   ```
   (Re-plug if `/dev/ttyACM0` is missing after a power-cycle.)
4. **Container logs** (Claude, background, over SSH to the Docker host):
   ```bash
   docker logs -f xiaozhi-esp32-server   # voice path, admin routes, state phrases
   docker logs -f dotty-behaviour        # perception events, consumers, security cycle
   docker logs -f dotty-bridge           # dashboard actions
   ```
5. **Dashboard:** open `http://<XIAOZHI_HOST>:8081/ui` on a second screen.
6. **Camera:** frame the whole robot — both LED rings, the screen, and head
   travel range. Good light, but check the face detector still fires (Phase B
   needs a detectable face).

**Results log:** copy the checklist below into the session notes (or tick
directly in this file on a branch). Every check gets `PASS` / `FAIL` /
`N/A-stale` + a note + clip reference.

---

## Phase A — boot & idle baseline (power-cycle once) — #44, #45

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| A1 | Power-cycle Dotty, let it boot | Right ring (pixels 6–11) **all dark** at idle | Boot banner; firmware version |
| A2 | Watch status bar during boot | Clock shows `—:— ——` first, flips to real local time once SNTP syncs (<10 s on healthy LAN) | SNTP sync line in serial |
| A3 | Wait ~10–15 s for xiaozhi STANDBY, touch nothing | Dashboard state card matches firmware (`idle`) with no clicks | One-shot resync `state_changed` after first STANDBY |

## Phase B — face & presence — #38, #43, #44

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| B1 | Walk into camera view, face at 30–60 cm, well lit | Left pixel 0 dim cyan (talk state); right pixel 6 yellow within ~1 s | Serial: `phase0 det>0` and `cmd>N` lines (**if absent with a clearly-framed face → regression is in commit `f372a3c`**, per #43) |
| B2 | Stay in frame, wait for VLM identify (room-view → roster match) | Pixel 6 turns **green**; ~4 s later reverts to yellow if no refresh | behaviour: `face_identified_refresher` activity; xiaozhi: `/xiaozhi/admin/set-face-identified` hits |
| B3 | Walk out of frame | Pixel 6 off after ~5 s grace; pixel 0 off | `face_lost` event; `last_face_id` cleared in `GET :8090/api/perception/state` |

## Phase C — listening & talk arc — #45

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| C1 | Tap screen, speak a short question | Right pixel 11 **red** while LISTENING; left ring all 6 pixels dim green during the chat | xiaozhi LISTENING/SPEAKING transitions; `chat_status` events |
| C2 | Let the reply finish (STANDBY) | Pixel 11 off; green arc off | STANDBY edge |

## Phase D — long-TTS stability (AXP2101 patch) — #37

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| D1 | Ask for a deliberately long answer (60 s+ of TTS), film the whole turn | Device does **NOT** reboot; battery % may briefly show 255% (acceptable glitch) | Serial: `W I2cDevice: ReadReg failed ...` warnings **instead of** `rst:0xc` reboot |

## Phase E — toggles — #38, #44

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| E1 | Toggle kid_mode on via dashboard | Right pixel 8 warm pink within ~1 s; dashboard kid dot matches. Off → both off | bridge → xiaozhi `/xiaozhi/admin/set-toggle` |
| E2 | Toggle smart_mode on via dashboard | Right pixel 9 orange; dashboard dot matches. Off → dark | same |
| E3 | During E1+E2, glance at pixels 7 and 10 | Stay dark throughout (reserved, locked off) | — |

## Phase F — MCP LED write-guard — #44

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| F1 | Voice: *"turn your LEDs blue"* | Only **left** ring lights blue; right-ring indicators untouched | `set_led_color` tool call in xiaozhi/pi logs |
| F2 | Voice: ask it to set LED **6** red (or inject via dashboard say) | Pixel 6 unchanged | Serial warn: `set_led_multi: index 6 not on left ring` |

## Phase G — story state & sticky exits — #38, #45

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| G1 | Voice: *"tell me a story"* | Pixel 0 warm colour + TTS ack | `state_changed` → `story_time` |
| G2 | Let a chat turn end while in story_time | State does **NOT** drop back to idle on STANDBY (sticky states own their exits) | no spurious `state_changed` on STANDBY |
| G3 | Voice: *"wake up"* | Pixel 0 off, back to IDLE | `state_changed` → `idle` |
| G4 | From dashboard, click the **current** state button | `state_changed` still fires (idempotent re-set), bridge cache refreshes | `state_changed` on idempotent set |

## Phase H — security state — #38, #40

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| H1 | Dashboard toggle → security (or voice *"keep watch"*) | Within ~3 s: yaw sweep −500 → +500 → 0 at speed 50 (~14 s/cycle), angry face latched, pixel 0 **flashing white 1 Hz** | `state_changed` → `security`; behaviour: `security capture loop started device=… interval=20s` |
| H2 | Wait ≥40 s | — | Inside `dotty-behaviour` container: `/var/lib/dotty-behaviour/logs/security-<date>.ndjson` gains a record with `photo_desc` populated, then another at +20 s. `audio_capture_pending` errors are **expected** (audio leg not shipped — #31) |
| H3 | Toggle back to idle | Pan stops within ~4 s, head home, face neutral | `security capture loop cancelled device=…`; NDJSON stops growing |

## Phase I — sleep state & wake paths — #39

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| I1 | Voice: *"goodnight Dotty"* | Smooth face-down travel (~3–4 s), pixel 0 very dim blue, sleepy emoji + `Zzz…` bubble, audible torque-release click ~1 s after settle | `state_changed` → `sleep` |
| I2 | Observe asleep, lights on | Gentle physical droop forward of commanded pose (expected — torque off) | — |
| I3 | While asleep, idle ~30 s | Idle motion does **NOT** fire | no idle-motion servo commands |
| I4 | Voice: *"wake up"* | Torque audibly re-engages **first**, wake-tilt up to ~70 pitch, neutral face, pixel 0 off (IDLE) | `state_changed` → `idle` |
| I5 | *"goodnight Dotty"* again; then **touch the head** | Same wake sequence, lands in IDLE (not TALK) | `head_pet_started` event |
| I6 | Sleep again; then **walk into camera** | Wakes straight to TALK (pixel 0 cyan); lookAt overrides wake-pose — reads as "looks up, then at you" | `face_detected` → TALK |
| I7 | With Dotty awake: pet the head | NO state change; existing pet behaviours (hearts + happy) still fire | no `state_changed` |
| I8 | Awake: provoke a "sleepy" emotion reply (😴) | Legacy hard-sleep path still works (face detector off, modifiers removed) | — |

## Phase J — dance + combined indicators — #44

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| J1 | Set kid_mode ON + smart_mode ON, get face identified (green pixel 6), then trigger DANCE and speak to light listening | Left ring runs rainbow; right ring holds **all four** indicators steady through the dance (6 green for its 4 s window, 8 pink, 9 orange, 11 red); 7 + 10 stay dark; worst-case 200 ms flicker acceptable (5 Hz re-assert) | `state_changed` → `dance` |

## Phase K — empty-room backoff (the coffee break) — #42

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| K1 | Return to idle, **leave the room** (camera keeps filming) for 2+ min | Idle-motion cadence drops from 4–8 s to 15–30 s after ~2 min with no face | idle-motion timing in serial |
| K2 | Walk back in front | Cadence back to 4–8 s within seconds | — |

## Phase L — restart resync (known-risk check) — #38, open bug #21

| ID | Brett does | Eyes / video | Logs (Claude) |
|---|---|---|---|
| L1 | With kid_mode + smart_mode ON: `docker restart dotty-bridge` on the Docker host, then speak one turn to Dotty | First turn after reconnect re-syncs both toggle pips from state files | bridge startup; pip re-assert |

> **Note:** L1 overlaps open bug **#21 (bridge ↔ firmware state divergence
> after bridge restart)**. A failure here is *expected possible* — record the
> exact divergence (which side held which state) as repro detail for #21
> rather than filing a new issue.

---

## Failure protocol (during the session)

1. **Don't stop to debug.** Note the check ID, wall-clock time, what was
   expected vs seen; say the ID + "fail" on camera.
2. Claude captures the surrounding log/serial context immediately (scrollback
   is lossy across reboots).
3. Move to the next check. Exception: a check whose failure invalidates its
   phase's remaining checks (e.g. B1 face detection dead) — skip the
   dependents, mark them `BLOCKED`.

## Post-session loop

1. **Triage** (same day, ~15 min): for each FAIL, file a fresh issue with the
   check ID, expected/actual, log excerpt, and video clip reference. Link it
   from #122. `N/A-stale` items: note in the source issue that the check no
   longer applies post-cutover.
2. **Fix**: work the failures (firmware → `dotty-flash-firmware` skill;
   services → `dotty-deploy-*` skills). Each fix's DoD = the specific bench
   check it failed, re-run.
3. **Re-run**: next bench session covers **only** the failed/blocked IDs plus
   anything the fixes could plausibly regress (same phase).
4. **Close out**: when a source issue's checks are all PASS (or N/A-stale),
   tick its boxes and comment the date + result. When all eight are drained,
   comment the pass-summary on #122 and close the epic.
