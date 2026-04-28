---
title: Reproducible Firmware Builds
description: How Dotty's firmware build produces byte-identical binaries from the same source commit, and how to verify a published release against the source.
---

# Reproducible Firmware Builds

Dotty's firmware build is designed to be reproducible: the same source commit
should always produce byte-identical binaries. This page explains the mechanism
and how to verify a published release.

## What "reproducible" means here

Two engineers, starting from the same git commit, running `make verify-firmware`
on different machines, should get the same `stack-chan.bin` SHA256 checksum.
The GitHub Actions release workflow records that checksum in `SHA256SUMS.txt`
and attaches it to every `fw-v*` release.

## Toolchain pinning

| Layer | Pinned to | How |
|-------|-----------|-----|
| IDF version | `espressif/idf:v5.5.4` | `container.image` in `firmware-release.yml` |
| Managed components | `idf_component.yml` + `dependencies.lock` | Committed in firmware submodule |
| Upstream firmware | `v2.2.4` tag + `patches/xiaozhi-esp32.patch` | `fetch_repos.py` in firmware build |

> **Adding a SHA256 digest to the IDF image** (optional, maximum trust):
> Run `docker pull espressif/idf:v5.5.4 && docker inspect espressif/idf:v5.5.4 --format '{{index .RepoDigests 0}}'`
> on a trusted machine and update `firmware-release.yml` `image:` to
> `espressif/idf:v5.5.4@sha256:<digest>`. This prevents a tag-rewrite attack
> on DockerHub from silently changing your toolchain.

## Verifying a release locally

```bash
# 1. Initialise the firmware submodule (once)
git submodule update --init --recursive

# 2. Build and checksum locally
make verify-firmware

# 3. Download the published SHA256SUMS.txt for the release you're verifying
RELEASE=fw-v0.1.0   # or whichever tag
curl -L "https://github.com/BrettKinny/dotty-stackchan/releases/download/${RELEASE}/SHA256SUMS.txt" \
     -o firmware/firmware/build/SHA256SUMS.published

# 4. Re-run to compare
make verify-firmware
# Expect: PASS  Build is reproducible.
```

## Pinning managed components

IDF managed components are fetched at build time unless locked. To generate a
lock file:

```bash
cd firmware/firmware
docker run --rm -v "$PWD:/project" -w /project \
  espressif/idf:v5.5.4 \
  bash -lc 'git config --global --add safe.directory "*" && python fetch_repos.py && idf.py reconfigure'
# Commit dependencies.lock alongside idf_component.yml
git add main/idf_component.yml dependencies.lock
```

The lock file pins exact component versions; without it, a new component
release could silently change the binary.

## CI workflow

The `firmware-release.yml` workflow fires on `fw-v*` tags and:

1. Checks out with `submodules: recursive`
2. Fetches upstream dependencies via `fetch_repos.py`
3. Builds with `idf.py build` inside `espressif/idf:v5.5.4`
4. Generates `SHA256SUMS.txt` over the three release binaries
5. Attaches binaries + checksums to the GitHub Release

GPG signing of release artifacts is scaffolded (see `docs/signed-releases.md`)
and enabled once `GPG_PRIVATE_KEY` / `GPG_PASSPHRASE` repo secrets are set.

## Known non-determinism risks

| Risk | Status |
|------|--------|
| Timestamp embedded in binary | Mitigated — ESP-IDF uses `SOURCE_DATE_EPOCH` when set |
| Managed component version drift | Mitigated once `dependencies.lock` is committed |
| IDF tag re-point on DockerHub | Low risk; pin digest for maximum confidence |
| `fetch_repos.py` fetching HEAD | Fixed — script pins to `v2.2.4` tag |
