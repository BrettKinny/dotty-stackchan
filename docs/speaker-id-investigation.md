---
title: Speaker-ID Investigation Log (Phase 1)
description: Timeboxed probe (2026-04-25) into whether xiaozhi-esp32-server exposes a speaker-ID hint the bridge could consume as Signal E in the SpeakerResolver.
---

# Speaker-ID — investigation log (Phase 1, 2026-04-25)

This is the timeboxed half-day probe that came out of the SpeakerResolver
work. The question: **does xiaozhi-esp32-server already expose a
speaker-ID hint our bridge can consume as Signal E in the resolver,
without us having to ship Layer 4 face-recognition firmware?**

## Answer

**Available upstream — not wired in our deployment today.** Adding it
is a separate, well-bounded task (~1–2 weeks). Until then the resolver
runs on Signals A/B/C/D (self-ID, calendar, time-of-day, perception)
which is enough to deliver the family-companion feature.

## What's there upstream

`docs/latent-capabilities.md` flags **Voiceprint speaker ID** as a
*Voice-pipeline – unused* feature (line 45):

> Distinguish family members; apply per-user persona/context — Medium
> priority — cross-refs child-safety (different guardrails for kids vs
> adults).

The underlying support comes from xiaozhi-esp32-server's optional
voiceprint module; SenseVoice's emotion + AED outputs are already
similar latent capabilities the deployment doesn't expose.

## What our patches expose today

`custom-providers/xiaozhi-patches/websocket_server.py` (the patched
fork) wires:

- `self._asr` (ASR module instance, line 60)
- `self._llm` (LLM provider — currently the ZeroClaw bridge)
- `self._memory` (initialised but disabled — `Memory: nomem` in
  `.config.yaml`)

There is **no voiceprint hook**. Searching for `speaker`, `voiceprint`,
`voice_id`, `speaker_id` across `xiaozhi-patches/` returns zero
matches (other than the unrelated "small speaker" persona text in the
config).

`custom-providers/zeroclaw/zeroclaw.py:_payload()` builds a metadata
dict: `{provider: "zeroclaw", smart_mode: ...}`. There is no slot for
a speaker hint coming from xiaozhi.

## What it would take to wire it

To turn "voiceprint exists upstream" into "Signal E in the resolver":

1. **Server-side enable** — add a `Voiceprint:` block to `.config.yaml`
   pointing at a voiceprint provider (the xiaozhi-server fork ships
   one; needs config + likely a model download).
2. **Enrollment ritual** — capture a few seconds of speech per
   household member, run them through the voiceprint module, persist
   the resulting embeddings in the server's voiceprint store. This
   needs a portal flow (admin-only) and parental-PIN gating to match
   the rest of our identity-data posture.
3. **WS metadata surface** — patch `websocket_server.py` to attach the
   recognised speaker id (and confidence) to the LLM call metadata so
   the bridge can read it. This is one extra field on the dict already
   passed to the `LLMProvider`.
4. **Provider passthrough** — `custom-providers/zeroclaw/zeroclaw.py`
   forwards xiaozhi metadata into the bridge's `MessageIn.metadata`.
   Add a passthrough for `speaker_id` / `speaker_confidence`.
5. **Resolver Signal E** — extend `bridge/speaker.py:_signal_perception`
   (or a new `_signal_voiceprint`) to read `payload.metadata` and
   produce a vote with the same shape as the existing perception
   signal. Weight likely 0.6–0.8 — between `SIG_PERCEPTION` (face-rec)
   and `SIG_STICKY` once we trust enrollments.

## Why we're not doing it now

- The resolver already works without it. Self-ID (Signal A) is the
  canonical correction handle, and calendar + time-of-day cover the
  routine 80% of weekday traffic.
- Voiceprint enrollment overlaps with face-rec enrollment ergonomically
  — once Layer 4 ships, we'd want a single "enroll a family member"
  ritual that captures both modalities together. Doing voiceprint first
  means redoing the enrollment UX twice.
- The 1–2 week time would be better spent on Phase 2 (memory
  persistence) which compounds the value of the identity work we just
  shipped.

## Outcome

Resolver ships with Signals A/B/C/D + a clean extension point for
Signal E. The `_signal_perception` method in `bridge/speaker.py:418`
already pattern-matches on `name == "face_recognized"`; adding a
`name == "voiceprint_match"` branch when the time comes is a five-line
change. No bridge-side schema change required.

## See also

- `docs/latent-capabilities.md:45` — original capability flag
- `bridge/speaker.py` — the resolver this would plug into
- `tasks.md` — Layer 4 face-rec roadmap (parallel identity track)
