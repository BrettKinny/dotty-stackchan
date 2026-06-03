# AI transparency

**Yes — Dotty is built with AI assistance, and it says so out loud.**

This is a human-focused project: made by humans, for humans. AI coding agents
are used as tools throughout the work — writing and refactoring code, drafting
documentation, triaging issues, and reviewing diffs. That is a deliberate,
out-in-the-open choice, not something tucked away. This page is the standing
promise about *how* that assistance is used and *how you can tell* when it was.

## The rule we hold ourselves to

> **Anything an AI agent authors is acknowledged as such.**

If a person can't easily tell whether a change came from a human or a tool,
we've failed the rule. Concretely, in this repo:

- **Commits** that an AI agent helped write carry a `Co-Authored-By:` trailer
  naming the model — e.g. `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
  This is real, current practice: the bulk of the commit history already
  carries it. Run `git log` and you'll see exactly which model touched what.
- **Pull requests** opened with agent help carry a generated-with note in the
  body (e.g. *🤖 Generated with Claude Code*).
- **Substantial AI-drafted documents** say so, in-line or in their footer,
  rather than passing themselves off as hand-written.
- **Agent comments and actions** — anything an AI posts, edits, or runs on the
  project's behalf — are attributable back to the agent, not laundered through
  a human name to look like unaided work.

## A human is always accountable

The AI proposes; a person decides. Every change that lands has a human in the
loop who reviewed it and is answerable for it. Acknowledgement is not a way to
offload responsibility onto a tool — it's the opposite. The maintainer's name
on the merge means a human read it, understood it, and stands behind it. The
co-author trailer just records *which tool helped get there*.

This matters most where the stakes are highest. Dotty ships **Kid Mode** on by
default, and the child-safety enforcement layer is load-bearing. Safety-relevant
changes get human red-team review regardless of who drafted them — see the
"Safety-related changes" section of [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## Why we use AI this way

We aim to use AI **responsibly: as a tool for good, in service of people.** The
guardrails we put on ourselves:

- **A human in the loop.** No unattended agent merges its own work to `main`.
- **A name on every change.** Attribution over anonymity — for tools and people
  alike.
- **Honesty about what works.** AI-drafted claims about behaviour get verified
  the same as any other; the README's "this is buggy, frequently broken"
  honesty applies to AI-written code too.
- **People first.** The point of the project is a friendly robot for a family
  and a hackable stack for the community. AI is how some of it gets built — not
  what it's for.

## For contributors

Using an AI assistant on your contribution is welcome and normal here. We just
ask you to keep the same rule: **acknowledge it.** Keep the `Co-Authored-By:`
trailers your tool adds (don't strip them), note agent help in your PR
description, and review the output yourself before you put your name on it. See
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for the mechanics.

## For AI agents working in this repo

If you are an AI agent operating on this project, this policy is binding on you,
not just descriptive. The operating instructions in [`CLAUDE.md`](./CLAUDE.md)
require you to acknowledge your authorship on every artifact you produce. Don't
remove existing attribution, don't present agent work as unaided human work, and
leave the human-accountability chain intact.

---

*This document was itself drafted with AI assistance and reviewed by a human —
which is exactly the point.*
