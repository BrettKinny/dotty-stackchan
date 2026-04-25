---
title: Documentation Versioning
description: How the Dotty docs site is versioned, what /latest/ vs /dev/ vs /vX.Y/ mean, and how maintainers cut a versioned doc release.
---

# Documentation Versioning

## TL;DR

| URL | What it shows | When to read it |
|---|---|---|
| `/latest/` | Docs for the most recent tagged release | You are running a stable, tagged build |
| `/vX.Y/` | Docs frozen at a specific minor version (e.g. `/v0.1/`) | You are pinned to that firmware/server version |
| `/dev/` | Docs from the tip of `main` | You build from source or follow `main` |

A version dropdown sits next to the site title and switches between any
of the published versions. The default landing page is `/latest/`.

## Why versioned docs?

The firmware and the server can lag each other. A user on `fw-v0.1.0` should
not be reading instructions written against `fw-v1.0.0` -- the MCP tool surface
or the WebSocket frame shape may have shifted. See
[COMPATIBILITY.md](../COMPATIBILITY.md) for the full breaking-change policy.

## Version policy

Versions follow SemVer. Tag namespaces are split between server and firmware
(see [COMPATIBILITY.md](../COMPATIBILITY.md#tag-namespaces)):

- **Server** -- `server-vX.Y.Z` (bridge, custom providers, docker compose).
- **Firmware** -- `fw-vX.Y.Z` (ESP32-S3 StackChan firmware).

The docs site publishes one version label per **MAJOR.MINOR** (e.g. `0.1`,
`1.0`, `1.1`). Patch releases overwrite the minor's published version --
so `server-v0.1.0`, `server-v0.1.1`, and `server-v0.1.2` all live at
`/v0.1/`, with the most recent patch winning.

`latest` always points to the most recently tagged version.

## Reading the version dropdown

The dropdown next to the Dotty title shows every published version. Pick one
and the whole site reloads in that version's tree. The version label appears
at the top of every page so it is obvious which version you are reading.

If a page only exists in newer versions, the older version's site shows a 404
for that page. That is expected -- the older version's docs reflect what was
true at that release.

## Maintainer notes

The docs site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
and versioned with [`mike`](https://github.com/jimporter/mike). Versions live
on the `gh-pages` branch under per-version folders.

### Cutting a versioned doc release

Versioned docs deploy automatically on tag push -- the
`docs-deploy.yml` workflow extracts MAJOR.MINOR from the tag, runs
`mike deploy --push --update-aliases <version> latest`, and updates the
`latest` alias.

Pushes to `main` deploy as the `dev` alias, leaving `latest` untouched.

### Manual deploy from a workstation

If you need to rebuild a version locally (e.g. a doc-only fix on an older
release):

```bash
# From the repo root
pip install -r docs/requirements.txt

# Fetch the gh-pages branch so mike has somewhere to commit.
git fetch origin gh-pages

# Deploy version 0.1 and update the 'latest' alias to point at it.
mike deploy --push --update-aliases 0.1 latest

# Or deploy a 'dev' build without touching 'latest'.
mike deploy --push dev

# Set the default version (the one users land on at the site root).
mike set-default --push latest
```

### Listing and removing versions

```bash
# List published versions
mike list

# Delete a stale version
mike delete --push 0.0
```

### Bootstrapping the site for the first time

`mike` expects to commit onto an existing `gh-pages` branch. If the repo has
never deployed docs, the first `mike deploy --push` call creates the branch.
If the repo previously used `mkdocs gh-deploy --force` (no versioning), the
first `mike deploy` overwrites the existing flat layout with a versioned one
-- existing URLs at the site root will break, and visitors should be
redirected to `/latest/`.

## See also

- [COMPATIBILITY.md](../COMPATIBILITY.md) -- the breaking-change policy and
  release process.
- [CONTRIBUTING.md](../CONTRIBUTING.md) -- how to contribute documentation
  alongside code.
- [`mike` upstream](https://github.com/jimporter/mike) -- the underlying tool.
