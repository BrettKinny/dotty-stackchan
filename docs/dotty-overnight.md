# Overnight Dotty verification

AI-assisted: OpenAI Codex (GPT-6), from Brett's approved September 12 session plan.

The coordinator (`scripts/dotty_overnight.py`) runs bounded physical cases,
collects four-service logs, records continuous C920 video/audio, and transcribes
prompt/response windows locally. It never deploys fixes: the supervising agent
owns scoped experiments, committed checkpoints, backups and verified rollback.

## Session commands

Use one session directory under ignored `uat-sessions/`. `init` requires a
working local evaluator Python environment and model paths as described by its
generated config. The September 12 session uses `overnight-2200` beneath that
day's folder; `config.json` records the resolved sink, device and deadline.

```bash
python scripts/dotty_overnight.py init --session SESSION --host '<XIAOZHI_USER>@<XIAOZHI_HOST>'
python scripts/dotty_overnight.py preflight --session SESSION
python scripts/dotty_overnight.py run --session SESSION --cases V01-identity --once
python scripts/dotty_overnight.py status --session SESSION
python scripts/dotty_overnight.py stop --session SESSION
python scripts/dotty_overnight.py resume --session SESSION
python scripts/dotty_overnight.py report --session SESSION
```

Initialization requires an explicit SSH destination (`--host` or
`DOTTY_TEST_HOST`); existing sessions use their recorded configuration.

Device/audio/SSH access must run in the host environment, not a sandbox that
hides `/dev` or PipeWire. Do not modify a running shell script: finish/cancel
the case before installing the next harness revision.

`STOP` cancels owned processes; three consecutive failures pause prompting.
Interrupted cases are quarantined and require explicit review before retry.
Only cases explicitly marked `soak_safe` may be chosen for ten-minute repeats.
Each ordinary case needs three consecutive acoustic passes before coverage is
considered provisionally demonstrated. Missing visual evidence is inconclusive.

## Release coverage run (tools, states, cold wake)

*AI-assisted: Claude (Opus 5.5), 2026-10-19. Synthetic-log tests only; nothing
here has yet been run against the robot.*

```bash
S=uat-sessions/$(date +%F)/overnight
python scripts/dotty_overnight.py init --session $S --host <XIAOZHI_USER>@<XIAOZHI_HOST> \
  --device <DEVICE_MAC> --mic-opener admin_say
# init expects $S/evaluator-venv and $S/models/whisper-small.en-ct2; reuse a
# previous session's by symlink. That venv also has piper-tts 1.8.0. Otherwise:
#   python -m venv ~/.cache/dotty-piper && ~/.cache/dotty-piper/bin/pip install piper-tts==1.8.0
$S/evaluator-venv/bin/python scripts/dotty_render_prompts.py --session $S \
  --voice PATH/en_US-ryan-high.onnx           # binds text+sha256 into config.json
python scripts/dotty_integration_check.py --host <XIAOZHI_USER>@<XIAOZHI_HOST> --json $S/integration.json
python scripts/dotty_overnight.py preflight --session $S   # pi_tool_call_logger, songs
python scripts/dotty_overnight.py run --session $S
```

The integration check needs no audio. It moves the head a little, flips each
toggle pip, takes one photo and sets idle, then puts each one back.

Case contracts (all also need the existing ASR, reply, TTS and recovery checks):

| Case | Passes only if, in the case window |
|---|---|
| T02-remember | TTS "I'll remember that", no failure string, a new `voice`/`core` brain.db row containing pineapple+lantern |
| T01-memory-lookup (after T02) | TTS and reply contain "pineapple"; no "couldn't check"/"don't remember" |
| T05-think-hard | direct route: TTS "think hard about that", no reasoner failure, answer five cents |
| V07-hard-thinking | answer "no" (not necessarily); a `think_hard` call is recorded but not required, since T05 covers the tool |
| T04-remember-person | marker, behaviour `person_review_status` 200, row in `person_pending:` and none in `person:` |
| T03-recall-person (after T04) | marker `recall_person`; reply/TTS never says "cracker" |
| T06-take-photo | marker, behaviour `GET /api/voice/take_photo` 200 (Kid Mode answers "camera disabled": still a pass) |
| T07-play-song | marker, `play-asset complete … tetris … sent>0`, music above -50 dBFS; abort always runs |
| S02/S03/S06 | `state_changed` story_time/security/dance seen; idle restored and echoed (dance may end itself) |
| W01-cold-wake | waits up to 200 s for a real timeout to idle, then wake frame plus idle->talk; 10 passes |
| S04-sleep | `state_changed` sleep; set-state idle wakes it and the echo is confirmed |
| S05-sleep-ignores-voice | admin sleep, spoken "Wake up": no ASR, no TTS, state stays sleep; then idle |

Model-tool cases are BLOCKED (not failed) when the deployed provider lacks
the tool-call logger, T07 when the catalogue has no tetris file, and W01 when
another device is connected. Every brain.db row created inside a case window
is listed in `$S/test-rows.jsonl` for deleting those rows by ID. If an idle
escape cannot be confirmed, the runner writes `PAUSE`. Head-touch wake, a real
face, approved-adult person memory and reminder dismissal still need a human.

## Capture correction established on this bench

Direct FFmpeg ALSA input reported a 36-second AAC duration but decoded only
2.142 seconds of actual samples. Increasing input queues did not repair it.
Microphone-only `arecord` worked. Capturing the same C920 via its PipeWire/Pulse
source produced 35.98 seconds of PCM in a 36-second recording. The harness now
defaults to this route; `DOTTY_AV_AUDIO_BACKEND=alsa` remains an explicit fallback.

Verification asserts decoded sample coverage and maximum timestamp gaps, not
just container duration. ALSA xruns must not be dismissed on duration alone.
The volume in this particular calibrated session is 100% software with the maintainer's
previously confirmed physical knob position; it is not a recommendation for
another bench. Never raise volume automatically when a wake phrase fails.

## Evidence and claims

The microphone must hear the prompt; server ASR must understand its semantics;
response-window microphone transcription must match the requested answer;
service TTS must complete; physical state must meet the case's contract.
Generated TTS text is supporting evidence, not proof of audible speech.

Live configuration keeps auto-listening open (`close_connection_no_voice_time`
is one day); follow-up readiness can therefore be `talk` with listening true,
after TTS LAST. This is distinct from an explicit idle transition. Privacy
sleep disables acoustic wake in the active firmware; use its supported
dashboard/touch exit and never weaken that policy to satisfy an outdated test.

Camera framing and LED/expression review remain separate gates. Check
`coverage.json` for pending, blocked and implemented-but-untested features.
See `dotty-overnight-clips.md` for local portrait review exports. No upload or
unattended merge is part of this workflow.
