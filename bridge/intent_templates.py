"""Per-intent template fallbacks for the engagement-decision sublayer.

These short canned utterances are used by
``bridge.engagement_decider.EngagementDecider`` whenever the LLM-driven
generation path is unavailable (timeout, exception, empty completion)
or when an intent type explicitly opts out of LLM generation. The
templates are deliberately small, English-only, kid-safe by default,
and parameterised by ``{name}``, ``{event_summary}``, and
``{when_human}`` placeholders where relevant.

The engagement decider treats template output as the absolute floor —
even if every external dependency (LLM, calendar, etc.) is dead, a
template line will still ship so behaviour stays predictable.

Adding a new template
---------------------
1. Add an entry to ``TEMPLATES`` with the intent-type key.
2. If the intent is time-of-day-aware, use a ``dict`` keyed by
   ``"morning" | "afternoon" | "evening" | "night"``.
3. If the intent picks randomly from a pool, use a ``list[str]``.
4. Otherwise a single string is fine.
"""
from __future__ import annotations

import random
from typing import Iterable, Mapping, Optional, Union

# Each value can be:
#   - a single str (used unconditionally)
#   - a list[str]  (random pick)
#   - a dict[str, str | list[str]] keyed by time-window name
TemplateValue = Union[str, list, dict]

TEMPLATES: dict[str, TemplateValue] = {
    "casual_greeting": {
        "morning": "Good morning, {name}!",
        "afternoon": "Hey {name}!",
        "evening": "Hi {name}, how was your day?",
        "night": "Hey {name}.",
    },
    "calendar_reminder": (
        "Hey {name}, don't forget {event_summary} at {when_human}."
    ),
    "time_marker": {
        "morning": "Good morning everyone!",
        "afternoon": "Hope your afternoon's going well.",
        "evening": "Winding down now.",
        "night": "Getting late.",
    },
    "curiosity": [
        "I wonder what's outside.",
        "Did you notice the light's different today?",
        "I had a thought.",
        "Quiet in here.",
        "I'm just thinking.",
        "Hmm.",
    ],
    "unknown_face": "Hello! I don't think we've met.",
}


def _safe_format(text: str, params: Mapping[str, str]) -> str:
    """Format ``text`` with ``params``, leaving unknown fields untouched.

    ``str.format`` raises ``KeyError`` for missing fields, which would
    bubble up into the engagement loop. We swallow that here so a
    template authored before its caller is wired up still produces
    *something* sensible.
    """
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def render_template(
    intent: str,
    *,
    window: Optional[str] = None,
    params: Optional[Mapping[str, str]] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """Return a rendered template line for ``intent``.

    Parameters
    ----------
    intent :
        Intent-type key (e.g. ``casual_greeting``).
    window :
        Time-of-day bucket name. Used to disambiguate dict-valued
        templates. Falls back to ``afternoon`` for unknown values so
        we never KeyError on a freshly-added window name.
    params :
        Mapping of placeholders to substitute, e.g.
        ``{"name": "Hudson", "event_summary": "library", "when_human": "9am"}``.
    rng :
        Optional ``random.Random`` for deterministic test output. When
        omitted, ``random.choice`` is used.

    Returns empty string if the intent is unknown — the caller is
    expected to treat empty output as "skip" and bump nothing.
    """
    params = params or {}
    spec = TEMPLATES.get(intent)
    if spec is None:
        return ""

    if isinstance(spec, str):
        return _safe_format(spec, params)

    if isinstance(spec, list):
        if not spec:
            return ""
        choice = rng.choice(spec) if rng is not None else random.choice(spec)
        return _safe_format(choice, params)

    if isinstance(spec, dict):
        # Time-of-day-aware. Default to afternoon if the requested
        # window doesn't have a bespoke line.
        chosen = spec.get(window or "afternoon")
        if chosen is None:
            chosen = spec.get("afternoon") or next(iter(spec.values()), "")
        if isinstance(chosen, list):
            if not chosen:
                return ""
            choice = rng.choice(chosen) if rng is not None else random.choice(chosen)
            return _safe_format(choice, params)
        return _safe_format(str(chosen), params)

    return ""


def list_intents() -> Iterable[str]:
    """Return the set of intent keys this module knows about."""
    return TEMPLATES.keys()


__all__ = ["TEMPLATES", "render_template", "list_intents"]
