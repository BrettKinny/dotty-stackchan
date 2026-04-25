# Contributing to Dotty

Thanks for your interest in contributing! This project is a hackable starting
point for self-hosting a voice stack on the M5Stack StackChan. Contributions
that improve the wiring, fix bugs, add provider alternatives, or improve
documentation are welcome.

## How to propose changes

1. **Fork** the repository on GitHub.
2. **Create a branch** from `main` for your work.
3. **Make your changes** (see guidelines below).
4. **Open a pull request** against `main` with a clear description of what
   changed and why.

Small fixes (typos, broken links, clarifications) can go straight to a PR.
For larger changes — new providers, architectural shifts, safety-related
modifications — please open an issue first to discuss the approach.

## Before submitting

- **Validate Docker Compose config:**
  ```bash
  docker compose -f docker-compose.yml config --quiet
  ```
  This catches YAML syntax errors and invalid keys before they hit a live
  deployment.

- **Check for leaked placeholders or real values:**
  - Files in this repo must use placeholders (`<UNRAID_IP>`, `<RPI_IP>`,
    `<RPI_USER>`, `<ROBOT_NAME>`, etc.) everywhere a real IP, hostname,
    username, or filesystem path would appear. See the "Configuring for your
    environment" table in `README.md` for the full list.
  - **Never commit real IPs, hostnames, usernames, API keys, or filesystem
    paths.** If your diff introduces a literal IP address or path that isn't
    a well-known default (like `127.0.0.1` or a standard port number), it
    probably needs to be a placeholder.

- **Test if possible.** If you have a StackChan and a running deployment,
  verify the change works end-to-end. If you don't have the hardware, note
  that in the PR description — someone else can test it.

## What lives where

Changes tend to fall into one of these areas:

| Area | Files | Notes |
|---|---|---|
| **Voice pipeline (xiaozhi-server)** | `docker-compose.yml`, `.config.yaml`, custom providers (`zeroclaw.py`, `edge_stream.py`, `fun_local.py`, `piper_local.py`) | These run inside the xiaozhi-server Docker container on the Unraid/Docker host. |
| **Bridge** | `bridge.py`, `zeroclaw-bridge.service`, `bridge/` | Runs on the RPi. The `bridge/` directory contains the Dockerfile, compose file, and deployment docs. |
| **Documentation** | `README.md`, `SETUP.md`, `docs/`, `session-prompt.md` | Docs under `docs/` follow conventions listed in `docs/README.md` (TL;DR at top, tables over prose, freshness footer). |
| **CI** | `.github/workflows/` | Currently just the bridge Docker image build. |

## Code style

- **Python:** Standard Python style. No specific formatter is enforced yet.
  Keep it readable, use type hints where they help, and match the style of
  the surrounding code.
- **YAML:** Two-space indentation. Use comments to explain non-obvious values.
- **Markdown:** Follow the conventions in `docs/README.md` — TL;DR at the
  top, tables for dense facts, relative links only.

## Placeholder discipline

This is the most important contribution guideline. The repo is designed to be
forked and configured per-deployment. Every value that varies between
deployments must use a placeholder:

- `<UNRAID_IP>`, `<UNRAID_USER>`, `<UNRAID_HOST>`, `<UNRAID_XIAOZHI_PATH>`
- `<RPI_IP>`, `<RPI_USER>`, `<RPI_HOME>`, `<RPI_BRIDGE_PATH>`
- `<RPI_ZEROCLAW_BIN>`, `<RPI_ZEROCLAW_CFG>`
- `<YOUR_NAME>`, `<ROBOT_NAME>`

Port numbers (`8000`, `8003`, `8080`, `18789`, `42617`) are product-generic
and do not need placeholders.

## Safety-related changes

The child-safety enforcement layer (prompt sandwich in `bridge.py`, audience
framing in `.config.yaml`) is load-bearing. If your change touches the system
prompt, turn suffix, or emoji enforcement logic, please describe your
red-team testing in the PR description. See the commit history for examples
of the red-team battery format.

## Where to ask questions

Open a [GitHub Issue](../../issues). There is no chat channel or mailing list
at this time.

## License

By contributing, you agree that your contributions will be licensed under the
same [MIT License](./LICENSE) that covers the project.
