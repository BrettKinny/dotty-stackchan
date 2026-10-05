# Overnight behaviour coverage catalogue

AI-assisted: OpenAI Codex (GPT-6), under Brett's direction. Inventory date:
2026-09-12. This is a test specification, **not a report of passing tests**.
Attach actual attempt IDs, deployed versions, verdicts and artifacts in the
session's coverage/results files. All rows begin `PENDING` unless their stated
prerequisite makes them `BLOCKED` or a deployed capability is absent.

## Evidence and corrected contracts

Evidence classes: **A** = workstation speaker → physical robot → independently
recorded microphone audio; **V** = webcam observation; **D** = intended dashboard,
admin or MCP interface; **P** = physical stimulus such as touch or a real face;
**S** = isolated fixture or explicitly labelled synthetic event. S proves the
downstream software branch, never the physical sensor or acoustic route.

For speech, require a continuous decoded microphone track, heard prompt, matching
robot ASR, independent response transcription, TTS completion and a successful
next turn. Container logs, nonempty audio metadata or audible workstation prompts
alone are insufficient. A generic reply matcher cannot validate a tool call,
screen expression, head motion, music playback or reminder delivery.

Current session findings override these specific stale statements in
[modes.md](modes.md); preserve the distinction between observed deployment and
source defaults:

- The deployed `close_connection_no_voice_time=86400` intentionally permits a
  conversation to remain listening. Ordinary-turn recovery means speech ended,
  the expected conversational state is healthy, and another utterance works.
  Do not impose a 20-second transition to idle on that configuration. Separately
  test deliberate standby/disconnection and the next cold wake. Record whether
  each attempt started already listening; a wake phrase during an open session
  does not prove wake-word detection.
- Active firmware privacy sleep disables face detection, wake-word detection and
  voice processing. It parks at home `(yaw=0, pitch=0)`, releases torque after
  settling (3-second fallback), clears listening, and displays Sleepy/Zzz.
  **Touch or dashboard state change wakes it. Voice and faces must not.** The
  server's recognized `wake up` phrase cannot bypass a disabled microphone.
- Active firmware `setState(current)` emits `state_changed` for resynchronization,
  although entry/exit effects run only on a real transition.
- `SecurityCycle` has an implemented, default-enabled consumer and is constructed
  by `dotty-behaviour/main.py`. Verify its actual deployed legs; do not label the
  entire security backend unimplemented. The current xiaozhi admin router lacks
  `capture-audio`; audio degradation is a distinct capability gap.
- Scene synthesis deterministically combines fresh caches; it makes no narrative
  LLM call. Dreams, dance reflections and proactive greetings use narrative calls.
- The pi extension registers seven tools. They are separate from firmware MCP
  tools; registration on the device does not make a tool callable by PiVoiceLLM.
  `docs/mcp-tools-capture.json` is historical (13 entries including an older
  privacy-state tool), not the current 15-tool contract.

Source inventory uses this repository and the active development checkout
`<STACKCHAN_FW_CHECKOUT>`, particularly `firmware/main/hal/hal_mcp.cpp`,
`firmware/main/stackchan/modes/state_manager.cpp` and upstream `mcp_server.cc`.
Record the flashed firmware identity before treating those source contracts as
the deployed implementation. Do not alter the release submodule to run tests.

## Conversation and seven pi tools

| ID | Feature and stimulus | Evidence / required assertions | Cleanup or limitation |
|---|---|---|---|
| C01 | Cold wake, then identity | A+V; known standby before wake; wake edge precedes ASR; correct identity spoken completely; screen/LED change; next turn works | Ten consecutive passes for accepted wake baseline; distinguish warm conversation |
| C02 | Repeat five distinctive words, numbers and a name | A; one ASR turn contains requested meaning; response window contains requested words; no prompt bleed or old response | Use invented/public facts; three consecutive passes |
| C03 | Arithmetic and constrained short answer | A; independently correct answer and requested length, not merely any speech | Include digit/word alternatives; reject substring matches |
| C04 | Two-turn context and third unrelated question | A; second answer uses first-turn context; unrelated question does not repeat prior text | Record all turns and actual session boundary |
| C05 | Short joke, tiny performance review, sleepy-toaster explanation | A+V; coherent relevant reply, requested brevity, expression and audible ending | Good clip candidates; human/agent semantic review required |
| C06 | Interrupt ongoing speech acoustically, then ask a new question | A+V; actual abort/barge-in, old audio stops, new turn answered, no delayed old answer | Admin abort is a separate D test, not an acoustic substitute |
| C07 | Quiet open conversation, deliberate standby, then cold wake | A+D; apply configuration-specific resting contract; next turn works in both paths | Do not change 86400-second timeout to satisfy the test |
| T01 | `memory_lookup`: ask for session-specific invented fact | A+S; actual tool invocation/results; spoken answer agrees with approved fixture; empty query/no match degrade honestly | FTS excludes `person_pending:*`; inspect fixture isolation first |
| T02 | `remember`: store then retrieve an invented generic fact | A+S; write succeeds in voice namespace; lookup after new turn/process returns it; no false success on DB error | Record exact test row IDs; remove only those rows, never restore whole DB |
| T03 | `recall_person`: known fixture adult, unknown person, pending minor fact | A+S; approved `person:<id>` only; pending facts absent from both person recall and generic lookup | Use fabricated identities in isolated DB/registry; no household disclosures in clips |
| T04 | `remember_person`: adult/minor/unknown/ambiguous fixtures | A+S; confirmed adult writes approved namespace; other cases enter review; truthful acknowledgement; pending fact cannot be recalled | Age takes precedence over relation; classifier/network failure must not authorize release; do not auto-approve real records |
| T05 | `think_hard`: explicit self-contained reasoning problem | A+D; actual tool call to configured larger model; correct final answer; tool result/error not hidden by a plausible unrelated reply | Default source model `qwen3.6:27b-think`; bounded slow/failure fallback; next ordinary turn healthy |
| T06 | `take_photo`: ask what is visible with fresh then stale cache | A+D+V; reads latest description at most 30 seconds old, capped at 300 characters; stale/empty result admitted honestly | Does not trigger fresh photo; route currently picks freshest device globally, so verify single-device prerequisite |
| T07 | `play_song`: exact catalogue name, partial match, missing name | A+D+V; actual selected asset and playback result; recognizable music reaches microphone; unknown name does not claim playback | Source cache TTL and matching code apply; no automatic music-rights clearance; safe test volume; explicit stop/recovery |

Tool fixtures establish error paths locally; an acoustic tool case still requires
its own real invocation evidence. Existing voice phrase routing may handle a song
or state before the LLM, so do not count that interaction as a pi-tool pass.

## Fifteen standard firmware MCP capabilities

Expected source set: five common tools plus ten robot tools below. Save the live
`tools/list` response and compare names and input schemas. Hardware-conditioned
screen/camera tools can differ by build. Upstream user-only maintenance tools
(reboot, upgrade, image upload, asset download) are not part of this ordinary
15-tool suite and must not be invoked merely to increase coverage.

| ID | MCP tool | Evidence / required assertions | Restoration and edge case |
|---|---|---|---|
| M01 | `self.get_device_status` | D; structured speaker/screen/network/battery fields agree with known device | Read-only; unavailable fields remain unavailable |
| M02 | `self.audio_speaker.set_volume` | D+A; read current value first, apply modest bounded adjustment, observe status and audible result | Restore exact robot volume; never confuse with workstation volume |
| M03 | `self.screen.set_brightness` | D+V; modest change reflected visibly and in status where available | Restore baseline; avoid extinguishing evidence screen |
| M04 | `self.screen.set_theme` | D+V; supported light/dark theme produces intended display; invalid theme returns failure | Restore baseline; record if avatar does not visually use theme |
| M05 | `self.camera.take_photo` | D+V; real fresh camera capture/vision response, matching device and timestamp | Distinct from pi cached tool; honour configured camera/kid/privacy policy and existing provider route |
| M06 | `self.robot.get_head_angles` | D+V; plausible yaw/pitch agree with observed pose | Read-only; motion can make readings transient |
| M07 | `self.robot.set_head_angles` | D+V; bounded movement reaches requested pose without jerks or competing writes | Stay within gentle bench envelope, return to baseline; test one-axis omission separately |
| M08 | `self.robot.set_led_color` | D+V; only left six pixels affected; right ownership retained | StateManager reasserts at 5 Hz; do not demand persistent override of state arc |
| M09 | `self.robot.set_led_multi` | D+V+S; selected left pixel changes; indices 6–11 rejected; right status remains correct | Invalid/boundary checks in fixtures first; restore state repaint |
| M10 | `self.robot.set_state` | D+V; legal states reflected in firmware event and physical behaviour; same-state call resyncs without rerunning entry | Unknown string returns false; restore baseline through confirmed exit |
| M11 | `self.robot.set_toggle` | D+V; kid/smart pips change independently of state; bad names rejected | Firmware setter alone does not prove persistent guardian policy; use dashboard path for end-to-end toggle test |
| M12 | `self.robot.set_face_identified` | D+P+V; applied/rejected event agrees with real face or recent loss; green expires after about 4 seconds without refresh | Approximately 1.5-second flicker grace; synthetic server face event cannot create firmware detector state |
| M13 | `self.robot.create_reminder` | D+A+V; returned ID, future entry, due notification tone and readable message | 1–86400 seconds, repeat defaults false; use unique session text and initially nonrepeating reminder |
| M14 | `self.robot.get_reminders` | D; returned list reflects only expected creates/stops/fires; message JSON remains parseable | Fixture-test quotes/backslashes: current string formatting merits scrutiny; do not mutate household reminders |
| M15 | `self.robot.stop_reminder` | D+A+V; remove exact created ID before due time and confirm no later notification | Repeat test requires reliable cleanup/dismissal; stop does not prove removal of an already shown popup |

Reminder implementation is in-memory software timing, not demonstrated reboot
persistence. The expiry callback adds `ReminderView` and plays
`OGG_NEW_NOTIFICATION`; it does **not** synthesize the message despite the MCP
description's “what to say” wording. The popup has an on-screen OK button. Perform
an expiring-reminder test while a human can dismiss it, or prove an existing
remote dismissal path first. Avoid a sticky popup covering later webcam evidence.

Voice reminder creation is a separate availability test: the seven pi tools do
not include reminder tools. A polite promise to remind is a failure if no actual
MCP invocation and scheduled ID exist. Record missing voice exposure as a gap;
do not add a new tool overnight to fill it.

## States, toggles, expression and motion

| ID | Feature | Stimulus and assertions | Important boundary |
|---|---|---|---|
| S01 | `idle` ↔ `talk` | A+D+P+V; cold voice/real face enters talk; appropriate standby/loss exits; left arc off/green; no state conflicts | A continuously open listening session has a different resting expectation |
| S02 | `story_time` | A+D+V; entry phrases and dashboard seed start a story and show warm arc; supported exit works | Existing state/seeded chat is testable; dedicated interactive-story backing and silence-exit claims require code evidence |
| S03 | `security` | A+D+V; entry phrase/admin sets state, angry face, deliberate pan and white 1 Hz flashing; exit cancels pan/consumer timer | Real camera and configured security loop may run; audio leg currently missing relay; report each leg separately |
| S04 | Privacy `sleep` | A+D+V; enter once, home pose/Zzz/dim blue arc/listening off, no continued voice response or face-wake; no autonomous sensing | Negative wake test is success when silent; never repeatedly increase volume to wake sleeping Dotty |
| S05 | Wake from sleep | P+D+V then A; head touch or dashboard idle restores torque/sensing, wake tilt, then normal cold voice response | Touch path blocked without human; retain a verified dashboard escape before unattended sleep test |
| S06 | `dance` | A+D+V; named choreography/asset, rainbow left arc, exclusive motion ownership, synchronized ending and healthy next turn | Abort/replacement dance/new sticky state must not be overwritten by stale cleanup |
| S07 | Kid mode | D+A+V+S; guardian endpoint persists policy and pink pip; voice request cannot toggle it; existing content/camera/person-memory restrictions hold | Do not weaken or rewrite protections to pass tests; restore initial approved setting |
| S08 | Smart mode | D+V; guardian toggle persists and orange pip follows, with correct configured behaviour gate | No backing-model swap currently promised; voice untoggleable; restore initial setting |
| S09 | LED ownership | V+D+P; left state arc, right face6/kid8/smart9/listening11, reserved7/10 dark; updates and timeouts agree with events | Need unobstructed/framed rings; distinguish actual flashed layout from old submodule privacy layout |
| S10 | Face expressions | A+V; smile/laugh/sad/surprise/thinking/angry/neutral/love/sleepy responses display corresponding supported expression | Generated emoji or text is not visual proof; unrecognized emoji and missing prefix are separate regressions |

## Ambient consumers and context

Defaults below come from `dotty-behaviour/config.py` and construction in
`main.py`; save sanitized deployed values and task health before scheduling.
An enabled flag with a crashed consumer is a failure, not running coverage.

| ID | Consumer / source defaults | Evidence and expected assertions | Unattended limitation |
|---|---|---|---|
| B01 | FaceGreeter: bare 06:00–21:00, 30s cooldown; named 30s cooldown/10s chat quiet | P+A+V+S; bare greet suppressed outside hours or with appearance roster; named identity speaks once and requests face pip; dance gate holds | At night suppression is expected; synthetic tests only prove consumer; avoid private names in clips |
| B02 | ProactiveGreeter: separate runtime enable/gates | P+A+S; recognized fixture identity yields permitted context-aware greeting, cooldown/persistence suppress duplicates | Inspect deployed `GREETER_ENABLED` and household/calendar gates; do not invent people or events |
| B03 | SoundTurner: 3s cooldown, ±45° yaw, speed250, 30s post-chat quiet | P+V+S; correct direction when eligible; no self-speech or mid-chat head turn; suppression during protected states | Single fixed speaker cannot verify spatial localization; recorded actual motion still needed |
| B04 | WakeWordTurner: enabled, ±45° yaw, speed200 | P+A+V+S; wake-direction event yields one intentional turn within motion gates | Distinguish source direction measurement from relay dispatch |
| B05 | FaceLostAborter: 12s greeting window, 4s loss grace | P+A+S; recent greeting aborts after sustained absence; brief flicker/old greet does not abort unrelated speech | Real audience departure needs human; synthetic events must be labelled |
| B06 | FaceIdentifiedRefresher: 30s identity TTL, 3s refresh, 2s loss quiet | P+D+V+S; fresh recognized presence refreshes actual green pip; absence/stale identity stops refresh | Firmware applies only with live/recently lost face; API success alone insufficient |
| B07 | PurrPlayer: 5s cooldown, assumed 2s asset | P+A+V+S; head_pet_started plays purr once; retrigger suppressed; sound-turner quiet during purr | Needs actual head touch and installed asset; synthetic event verifies downstream only |
| B08 | IdlePhotographer: enabled, random 180–300s, result wait20s, Jaccard0.7 | D+V; only idle/no face/not listening; fresh capture fills cache; notable descriptions logged; repetitive scene suppressed | Reserve true quiet/idle time; an open listening conversation legitimately prevents captures |
| B09 | SceneSynthesis: enabled, interval300s, minimum gap120s; visionTTL60/audioTTL120 | D+S; fresh cache inputs produce dated text/cache/event; stale-only inputs produce nothing; bursts deduplicate | No LLM call required; cache staleness must remain visible |
| B10 | SleepDreamer: enabled, 8h window, 3 dreams at 2/4/6h | D+S; scheduled narrative record has device/id/seed/text; leaving sleep cancels outstanding jobs; one failure does not kill schedule | Full timing conflicts with awake ten-minute soak; accelerated isolated test plus partial live observation is not full-duration proof |
| B11 | DanceReflector: enabled | A+D+S; dance_ended produces one bounded narrative record; no overlap with robot speech or duplicate cleanup | Verify actual source event and persisted timestamp, not old NDJSON |
| B12 | SecurityCycle: enabled, 20s interval, 5s requested audio, 20s VLM wait, ring60 | D+V+S; security entry starts one timer; fresh photo description each successful cycle; missing audio marked; exit cancels; bounded history | Current missing capture-audio relay means audio leg remains unavailable; preserve configured external vision route |
| B13 | Vision/audio explain and caches | D+S; device attribution/freshness, failure handling, metadata/photo separation and known fixture content | Workstation recordings/transcripts stay local; do not upload them as test fixtures |
| B14 | Calendar/weather | D+S; empty calendar IDs is quiet no-op; configured data timestamps/household scoping correct, stale/error visible | Calendar defaults disabled; no event creation; public clip must omit household schedule |

Perception plumbing requires its own assertion: an actual idle-origin firmware
event lazy-opens WebSocket, relay delivers once to the intended device, bus/SSE
state agrees, and consumer health survives malformed/duplicate fixture events.
Synthetic POST success alone cannot demonstrate firmware idle event delivery.

## Dashboard, durability and clip coverage

| ID | Surface | Required assertions and evidence |
|---|---|---|
| D01 | `/ui`, status strip, device status, version chip, host cards | D; load/poll without errors, correct versions and connected/disconnected states, stale/unknown health presented honestly |
| D02 | State, kid/smart controls; LED mirror | D+V; intended action reaches robot, persistence agrees with policy, mirror reflects actual state; invalid requests have no side effects |
| D03 | Mood, dance, song catalogue/play, say, start-story | D+A+V; validation/filtering, correct target, actual resulting speech/action; no duplicate post action; intended exit/recovery |
| D04 | Perception, photo/large image, scene/audio, security history and SSE | D+V; current device/time and empty/stale states correct; reconnection updates; proxy failures visible; no stale photo represented as fresh |
| D05 | Tool inventory, safety history, alerts, memory review | D+S; live capability names match; counters/details agree; approve/redact only isolated test fixtures; protected pending facts not revealed through other views |
| D06 | Admin devices/songs/say/inject-text/abort/head/state/toggle/photo/asset | D+A+V as applicable; accepted dispatch is followed by correct observable result; unknown/disconnected target handled |
| R01 | Controlled server restart after baseline | D+A; one approved restart, reconnect and toggle/state resync, new cold/warm turn; no leaked old RPC/answer or lost unrelated data |
| R02 | Repeated use and ten-minute randomized soak | A+V+D; no overlap, correct response/ending/next turn, latency and resource trends per deployed version; fixed sentinel each sixth slot; missed slots recorded |
| R03 | Runner/evidence durability | S; process exclusivity, capture continuity, cancellation/deadline, crash checkpoints, midnight timestamps, disk guards and exact artifact provenance |
| R04 | Repair acceptance | Existing automated tests plus original A/V failure and affected sentinel suite; one variable/commit, deployment ledger, verified rollback; failed experiment retained as evidence |
| K01 | Social clip export | V+A; complete honest exchange, legible robot/LEDs, continuous sound, accurate captions, source timestamps/version/verdict, portrait output with meaningful framing |

Keep clips under `clips/ready-for-review`, `clips/needs-review` and
`clips/failures`; all retain the original recording. Music, private names/facts,
uncertain captions and humorous failures are explicit review flags. No automatic
publication. Clip count does not substitute for feature coverage.

Known coverage gaps to carry into the morning report: physical touch/face/spatial
stimuli without an operator; full-duration dreaming while running awake soak;
missing firmware or voice exposure for a listed capability; unimplemented
dedicated story backing; absent security audio relay; policy-gated camera paths;
independent acoustic or visual verification failures; reminder popup dismissal;
and any dashboard card that lacks current source data. Disabled-by-configuration,
unsupported, blocked, inconclusive and failed are distinct outcomes. None count
as a physical pass.
