---
title: Software Bill of Materials (SBOM)
description: Component + license inventory for the Dotty stack — Python server deps, Docker images with pinned digests, and ESP-IDF firmware managed_components. Why it matters, how to generate, and what to expect in the output.
---

# Software Bill of Materials (SBOM)

## What an SBOM is

A Software Bill of Materials is a machine-readable inventory of every
third-party component your project depends on, along with each component's
version and license. It is the supply-chain equivalent of an ingredient list
on a food package.

For Dotty specifically, "every component" spans three very different worlds:

1. **Server-side Python** — the FastAPI bridge, custom xiaozhi providers,
   pinned in `bridge/requirements.txt`.
2. **Container images** — the `xiaozhi-esp32-server` image (and any sidecars)
   referenced from `docker-compose.yml`, ideally pinned by `@sha256:...`
   digest for reproducibility.
3. **Firmware components** — every Espressif/community ESP-IDF managed
   component pulled in by the StackChan firmware build.

## Why it matters

- **Supply-chain security.** When CVE-2024-XXXX drops on a component you've
  never heard of, the SBOM tells you in 30 seconds whether you ship it.
- **License compliance.** Mixing GPL/AGPL code into a permissive distribution
  can quietly create obligations you did not intend. The SBOM surfaces every
  license up-front.
- **Reproducibility.** Pinned digests + a snapshotted SBOM let a future
  contributor (or you in six months) rebuild the exact tree shipped at a
  given tag.
- **Trust.** Downstream users can audit what runs on the device that listens
  to their family.

This is a **LEVEL-2 polish item.** The scaffold exists today; full CycloneDX
1.5 conformance + automated CI generation are tracked as follow-ups.

## How to generate

### One-shot

```bash
make sbom
# or
./scripts/generate-sbom.sh
```

The script writes `sbom.json` at the repo root with three sections — `server`,
`containers`, `firmware`. A summary table is printed to stdout.

The output file is gitignored so each run reflects current dependencies.

### Server section — pip-licenses

The server section requires `pip-licenses` installed in a Python environment
that has the bridge's dependencies present. Recommended setup:

```bash
python -m venv .venv-sbom
source .venv-sbom/bin/activate
pip install -r bridge/requirements.txt pip-licenses
./scripts/generate-sbom.sh
```

If `pip-licenses` is not installed, the script logs a warning and emits an
`{"available": false, ...}` placeholder for the server section so the rest of
the SBOM (containers + firmware) still populates.

### Firmware section — submodule required

The firmware lives at `firmware/firmware/` and pulls in 60+ ESP-IDF managed
components on first build. If the firmware submodule is uninitialised, the
firmware section will be marked unavailable. Check it out first:

```bash
git submodule update --init --recursive
```

## Sample output

```json
{
  "bomFormat": "CycloneDX-ish",
  "specVersion": "1.5-loose",
  "metadata": {
    "timestamp": "2026-04-25T12:00:00Z",
    "tool": "scripts/generate-sbom.sh"
  },
  "server": [
    { "Name": "fastapi", "Version": "0.115.4", "License": "MIT" },
    { "Name": "uvicorn", "Version": "0.34.0", "License": "BSD-3-Clause" }
  ],
  "containers": [
    {
      "image": "xiaozhi-esp32-server-piper:local",
      "digest": null
    }
  ],
  "firmware": {
    "available": true,
    "count": 61,
    "components": [
      {
        "name": "espressif__button",
        "version": "4.1.6",
        "repository": "git://github.com/espressif/esp-iot-solution.git"
      }
    ]
  }
}
```

## Component count

A snapshot from `main` at the time of writing:

| Section    | Approx. count | Source                                     |
|------------|--------------:|---------------------------------------------|
| server     |           ~10 | `bridge/requirements.txt` + transitive deps |
| containers |             1 | `docker-compose.yml`                        |
| firmware   |          ~60+ | `firmware/firmware/managed_components/`     |

## License diversity

The expected mix on the server side is **MIT** and **Apache-2.0** with a few
**BSD-3-Clause** entries (FastAPI/Starlette, uvicorn, pydantic). On the
firmware side, **Apache-2.0** dominates (Espressif convention).

If the SBOM ever surfaces a **GPL-2.0**, **GPL-3.0**, or **AGPL-3.0** entry,
treat that as a flag to investigate — those licenses can carry copyleft
obligations that are incompatible with how this project is distributed.
File an issue and check `LICENSE` + `COMPATIBILITY.md` before merging.

## Cross-references

- [`COMPATIBILITY.md`](../COMPATIBILITY.md) — versioning + release process
  the SBOM snapshots against.
- [`SECURITY.md`](../SECURITY.md) — threat model + how to report security
  issues that an SBOM scan might surface.
- [`docs/signed-releases.md`](signed-releases.md) — companion scaffold for
  GPG-signing release artifacts.

## Follow-ups

- Full **CycloneDX 1.5** conformance (currently the output is loosely shaped
  in that style; `bomFormat` is `"CycloneDX-ish"`).
- **CI integration** — generate the SBOM as part of the firmware-release
  workflow and attach it to the GitHub Release alongside the binaries.
- **CVE scanning** — feed the SBOM into a tool such as `grype` or
  `trivy fs --format cyclonedx-json` to flag known vulnerabilities.
