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

## Capture correction established on this bench

Direct FFmpeg ALSA input reported a 36-second AAC duration but decoded only
2.142 seconds of actual samples. Increasing input queues did not repair it.
Microphone-only `arecord` worked. Capturing the same C920 via its PipeWire/Pulse
source produced 35.98 seconds of PCM in a 36-second recording. The harness now
defaults to this route; `DOTTY_AV_AUDIO_BACKEND=alsa` remains an explicit fallback.

Verification asserts decoded sample coverage and maximum timestamp gaps, not
just container duration. ALSA xruns must not be dismissed on duration alone.
The volume in this particular calibrated session is 100% software with Brett's
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
