---
title: UAT Runbook
description: Full-coverage UAT session script — every Dotty + dashboard feature, filmed for Dotty's YouTube channel.
---

# UAT Runbook — full-feature session, filmed for Dotty's channel

One long interactive session that tests **every feature of Dotty and the
dashboard**, on camera. Each check is a dual-purpose unit: a pass/fail QA
result *and* a candidate YouTube Short for Dotty's channel. Failures become
GitHub issues; passes become clips (see [`uat-social.md`](./uat-social.md)
for the production side).

> **AI-assistance note:** this document was drafted by an AI agent (Claude)
> from the feature inventory in `docs/modes.md`, `docs/protocols.md`,
> `dotty-behaviour/README.md`, `dotty-pi-ext/`, and the bridge dashboard
> source, following the session format proven in
> [`bench-runbook.md`](./bench-runbook.md).

**Session budget:** a full pass is ~2.5–3.5 h of device time. Phases are
independent — do them in order but stop anywhere. A planned break sits after
Phase UL (the halfway mark). If splitting across days, re-run Phase U0 setup
each day.

---

## Roles

| Who | Does |
|---|---|
| **Brett** | Performs each check at the device, phone camera in **vertical (9:16)** — every check is filmed as a self-contained <60 s mini-demo. Say the check ID aloud at the start of each clip (e.g. *"U-B-3"*) so clips map to results. |
| **Claude** | Runs `scripts/uat-capture.sh start` before the session, tails the four container logs live, calls out expected/missing log lines, records every check in the results CSV, files issues for failures afterwards. |

## Recording setup (the three synchronized captures)

1. **Phone camera** (vertical) on the robot — one clip per check, or one long
   take per phase if easier (the slicer cuts by wall-clock time either way).
2. **Screen capture** of the dashboard (`http://<XIAOZHI_HOST>:8081/ui`) —
   one continuous recording for the whole session (OBS or any recorder).
3. **Logs** — `scripts/uat-capture.sh start` tails all four containers with
   timestamps into `uat-sessions/<date>/logs/`; serial monitor optional but
   recommended (command printed by the script).

### The sync mark (do this first, on camera)

At session start, with **both** recordings rolling: Brett presses a dashboard
**emoji button** (visible state change on the robot's face) while saying
*"sync"* and reading the wall-clock time aloud (e.g. *"sync, fourteen oh two
and ten seconds"*). This single moment appears in the phone video, the screen
capture, and the logs — it's how `scripts/uat-slice.py` aligns video time to
wall-clock time. Repeat the sync mark after any recording restart.

### The results CSV

Copy `docs/uat-results-template.csv` to
`uat-sessions/<date>/results.csv` and fill one row per check as you go:
`check_id, verdict (PASS/FAIL/BLOCKED/N-A), source (phone/screen), start,
end (wall-clock HH:MM:SS), note`. This one file drives issue filing *and*
clip slicing — keep the times honest.

## Known-pending features (test them anyway)

These are documented as unimplemented; their checks below are marked
**⚠ pending**. Run and film them like everything else — the point is to
record *current actual behaviour*. Their failures update existing tracking
issues rather than spawning new ones, and their clips are candidate
"work in progress" content (Brett's call — see uat-social.md).

| Feature | Status | Where tracked |
|---|---|---|
| `story_time` backing path | Phase 7 pending — state/LED rails only | modes.md Phase 7 |
| `security` capture path | SecurityCycle photo capture is live; audio leg unshipped (#31), servo sweep verification in #166 | modes.md Phase 8, #31, #166 |
| `smart_mode` model swap | toggle-only; swap is v2 scope | #36 cutover notes |
| Tools-inventory card count | dashboard card may list 5 legacy entries; Pi registers 7 voice tools | treat the dashboard as stale presentation, verify Pi inventory separately |

---

## Phase U0 — pre-session setup (~15 min, before touching the robot)

1. **Health:** `make doctor` — all green before starting.
2. **Versions:** deployed git SHA + firmware build (serial boot banner) — the
   capture script records these in the session manifest.
3. **Log capture:** `XIAOZHI_SSH=<XIAOZHI_USER>@<XIAOZHI_HOST> scripts/uat-capture.sh start`
   — verify all four tails are writing, start the serial monitor it prints.
4. **Screen capture:** start recording the dashboard window. Keep the whole
   dashboard column in frame; it records continuously until session end.
5. **Camera:** vertical, robot filling ~2/3 of frame, both LED rings and the
   screen visible, head-travel range clear, good light (face detector must
   still fire — Phase UP needs it).
6. **Sync mark** (see above) — on camera, both recordings rolling.
7. **Results CSV** created from the template.

---

## Phase UB — boot & idle baseline (power-cycle once)

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UB1 | Power-cycle Dotty, film the whole boot | Boot animation; right ring (6–11) all dark at idle; left ring off | Boot banner + firmware version in serial | "Waking my robot up" — time-lapse-able boot |
| UB2 | Point at the status bar during boot | Clock `—:— ——` flips to real local time (<10 s on healthy LAN) | SNTP sync line | Skip as a Short; QA-only |
| UB3 | Hands off ~15 s, show dashboard on second screen/phone | Dashboard state card reads `idle`, matching the device, no clicks | One-shot resync `state_changed` after first STANDBY | "She knows what she's doing even when I don't touch her" |
| UB4 | Let it idle ~1 min | Idle motions every 4–8 s (head glances, blinks) | idle-motion cadence in serial | "What does a robot do when nobody's watching?" |

## Phase UC — conversation basics & the emoji face protocol

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UC1 | Tap screen, ask a short question ("what's your name?") | Pixel 11 **red** while listening; left ring dim green during the chat; spoken reply | LISTENING/SPEAKING transitions; PiVoiceLLM turn; `chat_status` | "Meet Dotty" — the canonical intro clip |
| UC2 | Ask something that should make her happy ("do you like being a robot?") | Face animates to match the reply's leading emoji (smile/love) | LLM reply starts with an allowlisted emoji; emotion frame emitted | "My robot has feelings (nine of them)" |
| UC3 | Walk the emotion range: ask for something surprising, something sad, something to think about | Face changes per turn — 😮 surprise, 😢 sad, 🤔 thinking all visibly distinct | each reply's leading emoji in convo log (`emoji_used`) | "All of Dotty's faces in 40 seconds" — montage bait |
| UC4 | Ask for a deliberately long answer (60 s+ TTS), film the whole turn | No reboot; speech plays to the end (battery % may glitch to 255%) | `I2cDevice ReadReg failed` warnings acceptable; **no** `rst:0xc` | QA-only unless the answer is funny |
| UC5 | Mid-reply, tap the dashboard **abort** (or start a new turn) | Speech stops promptly, robot recovers to listening/idle | abort route hit; TTS queue flushed | "You can interrupt her, she doesn't mind" |

## Phase UT — the seven voice tools

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UT1 | Tell her a keepable fact: *"please remember my favourite colour is purple"* | Warm acknowledgement after the write | `remember` tool call → brain.db write (`category=core`) | Part 1 of the memory two-parter |
| UT2 | New turn (or later in session): *"what did I tell you about my favourite colour?"* | She recalls purple | `memory_lookup` tool call + FTS5 hit | Part 2 — "she actually remembered" payoff |
| UT3 | *"Remember that Brett loves flat whites"* (adult) | Confirms she'll remember | `remember_person` → behaviour `person_review_status` → `person:` store | "Dotty is learning who I am" |
| UT4 | Same as UT3 but for a **kid's** name | Confirms, but fact goes to the **pending** queue, not live | classifier routes to `person_pending:`; visible later in dashboard Memory card (UD5) | QA-critical (child-safety path); clip optional |
| UT5 | *"What do you know about Brett?"* | Recites approved facts only | `recall_person` returns approved `person:` facts | Pairs with UT3 |
| UT6 | Ask a genuinely hard question (3+ digit multiplication or multi-step planning) | Noticeable pause, then a correct 1–2 sentence answer | `think_hard` → llama-swap `qwen3.6:27b-think` call | "When Dotty *really* thinks" — the pause is the content |
| UT7 | *"What do you see?"* with something distinctive held up | She describes the scene/object | `take_photo` → behaviour `/api/voice/take_photo` (fresh or ≤30 s cache) | "I showed my robot a banana" — prop comedy |
| UT8 | *"Play the Macarena"* (or any catalogued song) | Song plays through her speaker | `play_song` → `/xiaozhi/admin/songs` resolve → `/play-asset` | Natural lead-in to the dance phase |

## Phase US — the six states & voice phrases

Every state entered by **voice** where a phrase exists, and by **dashboard**
at least once. Sticky states (`story_time`, `security`, `sleep`) must ignore
face events and survive chat-turn ends. The `wake up` / `come back` /
`are you there` phrases exit `story_time` and `security`; sleep is a privacy
state and exits only through a head-pet or dashboard state control.

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| US1 | From idle, walk into view (auto idle→talk), then out (talk→idle) | Pixel 0 dim cyan on face, off ~5 s after leaving | `face_detected`/`face_lost` state edges | Covered again in UP1 — one filming pass serves both |
| US2 | Voice: *"tell me a story"* **⚠ pending** | State flips (pixel 0 warm orange) + ack line; record whatever storytelling does/doesn't happen | `state_changed` → `story_time` | If a story actually comes out: gold. If not: WIP clip |
| US3 | While in story_time, let a chat turn end, then walk out of frame | State does **not** drop to idle (sticky) | no spurious `state_changed` | QA-only |
| US4 | From story_time or security, voice: *"wake up"* | Back to idle, pixel 0 off | `state_changed` → `idle` | QA-only |
| US5 | Voice: *"keep watch"* **⚠ pending** | Within ~3 s: yaw sweep −500→+500→0, angry face latched, pixel 0 flashing white 1 Hz | `state_changed` → `security`; `security capture loop started … interval=20s` | "Dotty guards the house" — the sweep is very filmable |
| US6 | Stay in security ≥40 s | Sweep continues | security NDJSON gains records with `photo_desc` (+20 s cadence); `audio_capture_pending` errors expected (#31) | QA-only; feeds UD10 |
| US7 | Voice: *"wake up"* (exit security) | Pan stops ≤4 s, head home, neutral face | `security capture loop cancelled`; NDJSON stops | tail of the US5 clip |
| US8 | Voice: *"goodnight Dotty"* | Smooth face-down travel (~3–4 s), pixel 0 very dim blue, 😴 + `Zzz…`, torque-release click ~1 s after settle | `state_changed` → `sleep` | "Putting my robot to bed" — reliably charming |
| US9 | While asleep: idle ~30 s, lights on | Gentle droop, **no** idle motion | no idle-motion servo commands | part of US8 clip |
| US10 | Wake path 1 — dashboard: set state to *idle* | Torque re-engages **first** (audible), wake-tilt to ~70 pitch, neutral face, idle | `state_changed` → `idle` | Guardian-controlled privacy exit |
| US11 | Sleep again; wake path 2 — **pet her head** | Same wake sequence, lands in **idle** (not talk) | `head_pet_started` | Dark-room friendly |
| US12 | While asleep, walk into camera view or say *"wake up"* | No wake: camera and microphone remain disabled by the privacy gate | no state change | QA-only privacy check |
| US13 | Awake: provoke a sleepy reply (ask her if she's tired → 😴) | Legacy hard-sleep path still works | 😴 emotion frame | QA-only |
| US14 | Voice/dashboard: trigger **dance** | Left ring rainbow sweep, choreography + song | `state_changed` → `dance`; `_handle_dance` | The flagship Short. Film generously |
| US15 | From dashboard, click the **current** state's button | `state_changed` still fires (idempotent re-set), dashboard cache refreshes | `state_changed` on idempotent set | QA-only |

## Phase UL — toggles & the LED contract

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UL1 | Dashboard: kid_mode ON, then OFF | Pixel 8 warm pink ≤1 s; dashboard dot matches; off→dark | bridge → `/xiaozhi/admin/set-toggle`; `_apply_kid_mode()` hot-reload | "Kid mode: one pink light" |
| UL2 | With kid_mode ON, ask a borderline question (e.g. about a scary movie) | Kind redirect, age-appropriate | content-filter sandwich; Safety card hit (UD7) | QA-critical; clip only if the redirect is charming |
| UL3 | With kid_mode ON: *"what do you see?"* **⚠ known gap** | Record actual behavior; no live PiVoice camera-denial policy exists yet | verify whether shortcut or `take_photo` ran; file a safety issue if camera access occurs | QA-critical; do not present as a shipped privacy guarantee |
| UL4 | Dashboard: smart_mode ON, then OFF **⚠ pending** | Pixel 9 orange; dot matches; **no behaviour change** (swap is v2) | `set-toggle smart_mode`; `model_swap_active=False` | QA-only |
| UL5 | During UL1+UL4, camera close on pixels 7 and 10 | Both stay dark throughout (reserved, locked off) | — | QA-only |
| UL6 | Voice: *"turn your LEDs blue"* | Only **left** ring goes blue; right-ring pips untouched | `set_led_color` tool call | "She won't let *anyone* touch her status lights" |
| UL7 | Ask her to set LED **6** red | Pixel 6 unchanged; record the verbal response | serial warn if firmware receives `set_led_multi`; PiVoice has no LED voice tool | tail of UL6 clip |
| UL8 | The combined-indicator stress test: kid ON + smart ON + face identified (green 6) + trigger dance + speak | Rainbow on the left; right ring holds all four pips (6 green in its 4 s window, 8 pink, 9 orange, 11 red); 7+10 dark; ≤200 ms flicker OK (5 Hz re-assert) | `state_changed` → `dance` | "Every light on at once" — satisfying finale |

**☕ Break point.** Leave capture + screen recording running (or stop and
re-run the sync mark on resume).

## Phase UP — perception & ambient behaviour

Live checks first, then the timer/env-gated consumers verified by **evidence**
(NDJSON + dashboard) rather than waiting out their timers on camera.

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UP1 | From idle, walk into view, face 30–60 cm, well lit | Pixel 6 yellow ≤1 s; possible "Hi!" greeting (hour-gated) | `phase0 det>0`; `face_detected`; FaceGreeter | "She notices when I walk in" |
| UP2 | Stay in frame for VLM identify | Pixel 6 → **green**; named greeting if roster-matched | FaceIdentifiedRefresher keeps it green past the 4 s firmware timeout; `set-face-identified` hits | "My robot knows my face" |
| UP3 | Start her talking, then walk out of frame mid-reply | TTS aborts after the grace window (audience gone) | `face_lost` → FaceLostAborter | "She stops talking when you leave. Rude? Efficient?" |
| UP4 | From idle, off-camera-side: snap fingers / clap left, then right | Head turns toward each sound | `sound_event` (direction/balance) → SoundTurner | "Sneaking up on my robot (impossible)" |
| UP5 | From across the room, say the wake word | Fast head-turn to the speaker | `wake_word_detected` → WakeWordTurner (if `WAKE_TURN_ENABLED`) | pairs with UP4 |
| UP6 | Pet her head while awake | Hearts + happy face + **purr** sound; no state change | `head_pet_started` → PurrPlayer | "Yes, the robot purrs." Instant clip |
| UP7 | Return to idle, leave the room 2+ min (camera keeps rolling) | Idle-motion cadence drops 4–8 s → 15–30 s; walk back in → cadence recovers in seconds | idle-motion timing in serial | Time-lapse: "robot gets bored" |
| UP8 | Evidence check (Claude, off-camera): idle photographer | — | `perception-*.ndjson` gained silent-photo records this session (if `IDLE_PHOTOGRAPHER_ENABLED`) | screen-capture B-roll |
| UP9 | Evidence check: sleep dreamer — after the US8 sleep window | — | `dreams-*.ndjson` gained dream narratives (if `DREAMER_ENABLED`) | "My robot dreams" — read one aloud on camera. Exceptional content |
| UP10 | Evidence check: dance reflector — after US14 | — | `dances-*.ndjson` gained an LLM reflection (if `DANCE_REFLECTOR_ENABLED`) | read her dance review aloud |
| UP11 | Evidence check: scene synthesis | — | `scene-synthesis-*.ndjson` + `scene_synthesised` events (if `SCENE_SYNTHESIS_ENABLED`); sentence visible on Perception card | dashboard B-roll |
| UP12 | Evidence check: calendar context | — | `GET /api/calendar/today` returns events (if `CALENDAR_IDS` set); N-A otherwise | QA-only |
| UP13 | Evidence check: proactive greeter (distinct from UP1's FaceGreeter) | — | ProactiveGreeter activity in behaviour logs this session (if `GREETER_ENABLED`); N-A otherwise | QA-only |

Consumers gated **off** in this deployment: record `N-A` with the env var
name in the note, don't force-enable mid-session.

## Phase UD — dashboard walkthrough (the screen capture is the star)

Robot stays idle-ish; narrate over the dashboard. Every card, every action.
These clips can be screen-recording crops (vertical crop of the mobile-width
dashboard works well for Shorts).

| ID | Brett does (on screen) | Expected | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UD1 | Header status strip: click each of the bridge/server/robot dots | Host modals open with live detail; robot modal shows latest photo | `/ui/host/{bridge,server,robot}` 200s | "Mission control for a desk robot" |
| UD2 | State card: click through all six states (return to idle after each) | Robot follows each state visibly; state-conditional bottom section swaps (emojis / say / story / dance+songs / banners) | six `set-state` round-trips | speed-run all six states — split-screen with the robot |
| UD3 | Idle → **Emojis** row: press several of the 9 | Robot's face changes per press | `/ui/actions/mood` | "A remote control for moods" |
| UD4 | Talk → **Say** box: type a line, send | Robot speaks it verbatim | `/ui/actions/say` (≤500 chars) | "Making my robot say things" — obvious fun |
| UD5 | Memory card: find UT4's pending kid fact → **approve** it; **redact** another | Pending → approved queue move; redacted fact gone; `recall_person` now sees the approved one | `/ui/actions/memory/{approve,redact}` | QA-critical (human-review loop); clip optional |
| UD6 | Tools-inventory card | If it lists 5 legacy entries, record presentation FAIL; separately verify Pi registers all 7 voice tools | Pi RPC/tool-startup evidence is authoritative | QA-only |
| UD7 | Safety card after UL2 | The kid-mode filter hit from UL2 listed (last 20) | `/ui/safety/recent` | QA-only |
| UD8 | Activity feed: cycle All / Turns / Events / Errors chips; open the errors modal | Live SSE turns + perception events streaming; errors modal renders | `/ui/events` SSE + `/api/perception/feed`; `/ui/alerts/detail` | B-roll: the feed scrolling during a chat |
| UD9 | Perception card + vision modal: open latest photo large, download | Latest VLM photo + description, audio caption, last voice line, scene sentence | `/ui/vision/large`, `/ui/vision/photo?download=1` | "What my robot sees" |
| UD10 | Security panel (after US5–US7): open security scene history | Capture records from the US6 window render | `/ui/security/recent/{device_id}` | pairs with the US5 clip |
| UD11 | LED-ring mirror page while toggling kid_mode on the robot | Mirror tracks the physical ring ≤2 s (HTMX poll) | `/ui/led-ring-mirror` | split-screen: physical ring vs mirror |
| UD12 | Songs list (dance section): play a track from the dashboard | Robot plays it | `/ui/songs` → `/ui/actions/play-song` | QA-only (UT8 covers the voice path) |

## Phase UR — resilience (last, on purpose)

| ID | Brett does (on camera) | Expected — eyes/video | Expected — logs (Claude) | Shorts framing |
|---|---|---|---|---|
| UR1 | With kid+smart ON: `docker restart dotty-bridge`, then speak one turn | First turn after reconnect re-syncs both pips from state files | bridge startup; pip re-assert. **Overlaps open bug #21** — a divergence here is repro detail for #21, not a new issue | QA-only |
| UR2 | `docker restart dotty-behaviour`, then walk into view | Perception consumers recover: face pip + greeting work again | behaviour startup; consumers re-registered | QA-only |

---

## Failure protocol (during the session)

Same as the bench runbook:

1. **Don't stop to debug.** Say the ID + "fail" on camera, note wall-clock
   time + expected vs seen in the CSV.
2. Claude captures surrounding log/serial context immediately (scrollback is
   lossy across reboots).
3. Move on. Exception: a failure that invalidates its phase's remaining
   checks — mark dependents `BLOCKED`.
4. **⚠ pending checks can't "fail" in the ordinary sense** — record what
   actually happened; the verdict is still PASS/FAIL against the *documented
   current* behaviour (e.g. US5's sweep shipping is PASS even though capture
   is pending).

## Post-session loop

1. **Stop captures:** `scripts/uat-capture.sh stop` (pulls the day's NDJSON
   files + endpoint snapshots into the session dir), stop screen + phone
   recording, copy videos into `uat-sessions/<date>/video/`.
2. **Slice** (~15 min): `scripts/uat-slice.py results.csv --video … --sync …`
   → PASS clips to `clips/shorts/`, FAIL/BLOCKED to `clips/issues/`.
3. **Triage** (same day, ~30 min): one GitHub issue per genuine FAIL, per
   `docs/agents/issue-tracker.md`: title `UAT <check-id>: <symptom>`, body
   with expected/actual, log excerpt, clip filename; labels `needs-triage` +
   matching `area:*`. Known-pending fails (US2, US6, UL4, UD6) **update their
   existing tracking issues** instead. UR1 divergence → comment on #21.
4. **Publish** to the live channel
   ([youtube.com/@dotty-stackchan](https://www.youtube.com/@dotty-stackchan)):
   work through `clips/shorts/` per the upload checklist in
   [`uat-social.md`](./uat-social.md); paste each video URL back into the
   results CSV `note` column.
5. **Wrap-up**: comment the pass/fail counts + results CSV location on the
   session tracking issue. Re-run sessions cover only FAIL/BLOCKED IDs plus
   whatever the fixes could plausibly regress.

Last verified: 2026-07-11.
