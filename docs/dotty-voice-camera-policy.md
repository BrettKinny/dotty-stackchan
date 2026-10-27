# Voice-camera Kid Mode policy — review candidate

TL;DR: this local repair guards **voice-requested** physical capture and cached
camera descriptions separately. It is **pending human safety/red-team review**;
software tests do not establish live deployment or a global camera-off guarantee.

## Enforcement boundaries

| Boundary | Adult mode | Kid Mode or unreadable policy |
| --- | --- | --- |
| Spoken vision shortcut in `receiveAudioHandle.py` | Existing camera MCP and image-result path | Denied before capture; no successful-photo context is constructed |
| Behaviour `/api/voice/take_photo` | Existing fresh <=30-second cached description, capped at 300 characters | Fixed structured denial before inspecting the cache |

Both read the bridge-owned `DOTTY_KID_MODE_STATE` file for every access. Only an
explicit `false`, `0`, or `no` enables adult camera access. Missing, unreadable,
invalid UTF-8 or malformed state fails closed, even if a startup environment
flag says adult mode. This is intentionally stricter than the legacy toggle
reader; ordinary content-filter and LED toggle behavior is unchanged.

The behaviour Compose candidate adds a read-only directory mount of the existing
bridge state at `/var/lib/dotty-bridge/state`. Mounting the directory follows file
replacement; it does not create a second toggle store. Deployments must map this
to the same guardian-controlled state directory used by bridge and xiaozhi.
Do not use behaviour's startup-only `app.state.kid_mode` flag as the authority.

The registered `take_photo` tool is a **cached-description read**, not a new
photo. The spoken vision shortcut can physically capture before Pi is called,
so guarding only the Pi tool does not protect that route.

## Deliberate scope and review requirements

This does not disable ambient face detection, security/idle capture, guardian
dashboard/admin capture, or an external webcam. It does not claim those paths
share this voice-access policy. Existing safety/content filters remain intact.

Before deployment, a human must review the child-policy change and the shared
read-only mount. Follow CONTRIBUTING's safety-change discussion and red-team
documentation requirements; obtain separate deployment approval. Do not deploy
only the physical shortcut guard while leaving cached voice access unguarded.

Required review checks:

- A fresh synthetic cache is denied in Kid Mode; a stale cache alone cannot
  establish denial. Verify no camera MCP or cached content exposure.
- Toggle adult -> kid -> adult without restarting either service and verify
  allow -> deny -> allow at both boundaries.
- Missing, unreadable and malformed shared state must deny camera access.
- Valid adult policy preserves physical capture and cached freshness behavior.
- Spoken denial must not be turned into “the photo shows ...” or a claim that a
  photo was taken. Confirm the microphone reply and execution evidence separately.
- Re-run existing content-filter, emoji and per-person-memory review tests.
  No household memory write is required for these checks.

Regression tests use synthetic state and mocked dispatch; no private camera data
or live services are required. New tests first failed against the unguarded code.

AI-assisted implementation/documentation: OpenAI Codex (GPT-6). Human review and
accountability are required. Last reviewed by agent: 2026-09-12; not deployed.
