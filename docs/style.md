---
title: Documentation Style Guide
description: Conventions for writing and maintaining Dotty's docs.
---

# Documentation Style Guide

Rules of the road for docs in this repo. Follow these so the docs stay
consistent and easy to maintain.

## Tone

Write like you're explaining something to a friend at a whiteboard.
Be direct, be practical, skip the corporate fluff. Use "you" freely.
Say what something *does*, not what it "enables" or "facilitates".

Match the existing docs — friendly, concise, opinionated where it helps.

## Frontmatter

Every Markdown file under `docs/` must start with YAML frontmatter:

```yaml
---
title: Page Title Here
description: One-line summary of what this page covers.
---
```

MkDocs Material uses these for the HTML `<title>` and search index.

## Headings

- **H1 (`#`)** — page title, once per file, matches the frontmatter `title`.
- **H2 (`##`)** — top-level sections.
- **H3+ (`###`)** — subsections as needed.
- Make headers grep-friendly: `## MCP tool handshake` beats `## Overview`.

## Adding a how-to doc

1. Create the file in `docs/cookbook/` (e.g. `docs/cookbook/do-the-thing.md`).
2. Add frontmatter (`title` + `description`).
3. Add a nav entry in `mkdocs.yml` under the `Cookbook:` section.
4. Keep it short — one task, one page. Link to reference docs for background.

## Decay annotations

Use `Last verified: YYYY-MM-DD` as the last line of any page that contains
facts which go stale — hardware specs, version-specific details, API
responses, model pricing. Pure how-to pages that only reference local config
can skip it.

The decay half-lives in [README.md](./README.md) help readers gauge how
much to trust a page based on its age.

## Code blocks

Always use fenced blocks with a language tag:

````markdown
```yaml
TTS:
  EdgeTTS:
    voice: en-AU-WilliamNeural
```
````

Common tags in this repo: `yaml`, `bash`, `python`, `json`.

## Links

- Use **relative paths**: `[protocols.md](../protocols.md)`, not absolute URLs.
- Link to other docs pages, not to GitHub blob URLs.
- External links (upstream repos, specs) are fine as full URLs.

## Tables over prose

When you have a list of specs, options, or mappings, use a Markdown table
instead of bullet points. Tables scan faster and diff cleaner.

## Placeholders

Use `<UNRAID_IP>`, `<RPI_IP>`, `<RPI_USER>`, etc. for any value that varies
per deployment. See the full list in `CONTRIBUTING.md`. Never commit real
IPs, hostnames, or API keys.

## Keep it short

If a page is getting long, split it. One concept per page, one task per
cookbook entry. The nav is cheap — use it.
