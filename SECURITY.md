# Security Policy

## Threat model

This project is infrastructure for an **always-on voice assistant** (StackChan).
The device sits on a desk, listens via a hot mic, and speaks aloud. It ships
with Kid Mode enabled by default (an optional child-safe personality mode), but
the base system is a general-purpose assistant. The always-on microphone and
unauthenticated LAN endpoints make the threat model more sensitive than a
typical hobby project; when Kid Mode is active the threat model escalates
further because children become the audience:

- **Voice pipeline exposure:** ASR transcripts, LLM prompts, and TTS audio
  traverse the LAN between the device, the Docker host, and the RPi. An
  attacker on the LAN could intercept or inject traffic.
- **Kid Mode safety:** When Kid Mode is active, children are the intended
  audience. Prompt injection or jailbreaks that bypass the content-safety
  enforcement layer could expose a child to harmful content.
- **Always-on microphone:** The device captures ambient audio. Compromise of
  the voice pipeline could leak private conversations.
- **No authentication on internal endpoints:** The bridge (`/api/message`)
  and xiaozhi-server endpoints are unauthenticated by design (LAN-only
  deployment assumption). Exposing them to the internet without additional
  controls is a vulnerability.

## What is in scope

- The FastAPI bridge (`bridge.py`) and its ACP session handling
- Custom xiaozhi-server providers (`zeroclaw.py`, `edge_stream.py`,
  `fun_local.py`, `piper_local.py`)
- Docker Compose configuration and container security
- Content-safety prompt enforcement (prompt sandwich, emoji prefix enforcement,
  Kid Mode filtering)
- The bridge Docker image and its CI pipeline
- Documentation that could lead to insecure deployments if followed as-is

## What is out of scope

- Upstream xiaozhi-esp32-server vulnerabilities (report to
  [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server))
- Upstream ZeroClaw vulnerabilities (report to
  [zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw))
- Upstream M5Stack StackChan firmware vulnerabilities (report to
  [m5stack/StackChan](https://github.com/m5stack/StackChan))
- LLM model behavior that is not caused by this project's prompts or code

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, use one of these channels:

1. **GitHub private vulnerability reporting:**
   Use the "Report a vulnerability" button on the
   [Security tab](../../security/advisories/new) of this repository. This
   creates a private advisory visible only to maintainers.

2. **Email:**
   Send details to **brett@squarewavesystems.com.au**. If you would like to
   encrypt your report, ask for a PGP key in a preliminary email.

### What to include

- Description of the vulnerability
- Steps to reproduce (or a proof of concept)
- Affected component(s) and file(s)
- Potential impact, especially if it relates to Kid Mode safety or audio privacy

### What to expect

- **Acknowledgment** within 72 hours of your report.
- **Initial assessment** within 7 days — whether it is accepted, needs more
  information, or is out of scope.
- **Fix or mitigation** timeline communicated once the issue is confirmed.
  For Kid Mode safety issues, expect an accelerated response.
- **Credit** in the fix commit and changelog (unless you prefer to remain
  anonymous).

## Supported versions

This project does not have formal releases yet. Security fixes will be applied
to the `main` branch. There are no backport branches.

## Disclosure policy

We follow coordinated disclosure. Once a fix is merged and deployed, the
vulnerability details will be made public (via GitHub advisory or changelog
entry). We ask reporters to allow up to 90 days before public disclosure,
though we aim to resolve issues much faster than that.
