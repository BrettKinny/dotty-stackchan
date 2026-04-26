"""Household registry — the source of truth for "who lives here."

Loaded from a single YAML file (default `~/.zeroclaw/household.yaml`,
overridable via `HOUSEHOLD_YAML_PATH`). The registry powers identity-
aware behaviour across the bridge:

  - the speaker resolver (self-ID phrases, time-of-day priors, calendar
    prefix mapping)
  - the proactive greeter (display name, personality, birthday)
  - the `[Speaking with]` block injected into voice-channel prompts

Schema is small and human-edited. Free-text fields (`personality`,
`appearance`, `voice`, `family_context`, `notes`) flow through to the
LLM verbatim. Structured fields (`birthdate`, `calendar_prefix`,
`self_id_phrases`, `usual_times`) are consumed by code.

Reload semantics: the registry stat-checks the YAML file on every
public access. If mtime has moved, it re-parses. Cheap; lets you edit
household.yaml on the running RPi without a bridge restart.

If PyYAML is missing or the file is unreadable/malformed, the registry
starts empty rather than crashing the bridge. An empty registry is a
valid state — it means everyone resolves to `_household`, exactly like
today.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("zeroclaw-bridge.household")

try:
    import yaml  # type: ignore
    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover — install-time gate
    yaml = None  # type: ignore
    _YAML_AVAILABLE = False
    log.warning(
        "PyYAML not installed — household registry will start empty. "
        "Add `PyYAML>=6.0,<7` to bridge/requirements.txt and reinstall."
    )

DEFAULT_HOUSEHOLD_PATH = "~/.zeroclaw/household.yaml"
DEFAULT_PERSON_FALLBACK = "_household"

_LEADING_PUNCT_RE = re.compile(r"^[\s\W_]+")


@dataclass(frozen=True)
class Person:
    """One household member. All fields optional except `id` and
    `display_name` — a YAML entry with just those two is valid."""

    id: str
    display_name: str
    relation: Optional[str] = None
    pronouns: Optional[str] = None
    age: Optional[int] = None
    birthdate: Optional[date] = None
    appearance: Optional[str] = None
    voice: Optional[str] = None
    personality: Optional[str] = None
    interests: tuple[str, ...] = ()
    family_context: Optional[str] = None
    notes: Optional[str] = None
    do_not: tuple[str, ...] = ()
    self_id_phrases: tuple[str, ...] = ()
    usual_times: dict[str, tuple[str, ...]] = field(default_factory=dict)
    calendar_prefix: Optional[str] = None
    voice_print_id: Optional[str] = None

    def days_until_birthday(self, *, today: Optional[date] = None) -> Optional[int]:
        if self.birthdate is None:
            return None
        ref = today or date.today()
        try:
            this_year = self.birthdate.replace(year=ref.year)
        except ValueError:
            this_year = date(ref.year, self.birthdate.month, 28)
        if this_year >= ref:
            return (this_year - ref).days
        try:
            next_year = self.birthdate.replace(year=ref.year + 1)
        except ValueError:
            next_year = date(ref.year + 1, self.birthdate.month, 28)
        return (next_year - ref).days

    def compact_description(self, *, max_chars: int = 200) -> str:
        """A single-line summary suitable for a `[Speaking with]` block.
        Combines display name, age (if known), and the most identifying
        free-text bits. Never includes raw birthdate (PII funnel)."""
        parts: list[str] = [self.display_name]
        meta: list[str] = []
        if self.age is not None:
            meta.append(f"{self.age}yo")
        elif self.relation:
            meta.append(self.relation)
        if self.personality:
            meta.append(self.personality)
        if self.interests:
            meta.append("loves " + ", ".join(self.interests[:3]))
        if meta:
            parts.append(" — " + "; ".join(meta))
        out = "".join(parts).strip()
        if len(out) > max_chars:
            out = out[: max_chars - 1].rstrip() + "…"
        return out


class HouseholdRegistry:
    """YAML-backed registry of household members with hot-reload."""

    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        clock: Any = None,
    ) -> None:
        raw = (
            os.fspath(path) if path is not None
            else os.environ.get("HOUSEHOLD_YAML_PATH", DEFAULT_HOUSEHOLD_PATH)
        )
        self._path = Path(raw).expanduser()
        self._clock = clock
        self._mtime: float = 0.0
        self._people: dict[str, Person] = {}
        self._default_person: str = DEFAULT_PERSON_FALLBACK
        self._by_prefix: dict[str, str] = {}
        self._self_id: list[tuple[str, str]] = []
        self._reload()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def default_person(self) -> str:
        self._reload_if_changed()
        return self._default_person

    def get(self, person_id: str) -> Optional[Person]:
        self._reload_if_changed()
        if not person_id:
            return None
        return self._people.get(person_id.lower())

    def iter(self) -> Iterable[Person]:  # noqa: A003
        self._reload_if_changed()
        return tuple(self._people.values())

    def render_roster_for_vlm(self, *, max_line_chars: int = 80) -> str:
        """Render members with a non-empty `appearance:` as one line per
        person, suitable for inlining into a VLM identification prompt.
        Lines are sorted by display_name for stable diffing across
        reloads. Empty appearance is treated as "exclude from roster" —
        a member with no visual description cannot be identified by
        photo, so injecting their name into the prompt would only
        invite the VLM to false-positive on them."""
        self._reload_if_changed()
        lines: list[str] = []
        for p in sorted(self._people.values(), key=lambda x: x.display_name.lower()):
            appearance = (p.appearance or "").strip()
            if not appearance:
                continue
            if len(appearance) > max_line_chars:
                appearance = appearance[: max_line_chars - 3].rstrip() + "..."
            lines.append(f"  {p.display_name}: {appearance}")
        return "\n".join(lines)

    def roster_ids_with_appearance(self) -> set[str]:
        """Set of canonical person_ids that have a non-empty appearance.
        Used by the VLM-response parser to validate that the name the
        VLM returned is one of the members it was asked to choose from."""
        self._reload_if_changed()
        return {
            p.id for p in self._people.values()
            if (p.appearance or "").strip()
        }

    def get_by_calendar_prefix(self, prefix: str) -> Optional[Person]:
        """Look up a person by their `[Name]` calendar prefix.
        Case-insensitive; brackets optional."""
        self._reload_if_changed()
        if not prefix:
            return None
        key = prefix.strip().lower()
        if not key.startswith("["):
            key = f"[{key}]"
        person_id = self._by_prefix.get(key)
        return self._people.get(person_id) if person_id else None

    def match_self_id(self, text: str) -> Optional[Person]:
        """Match an utterance against every person's `self_id_phrases`.
        Phrases are matched at the leading position only, after stripping
        leading punctuation/whitespace. Case-insensitive. Returns the
        first matching Person, or None."""
        self._reload_if_changed()
        if not text or not self._self_id:
            return None
        normalised = _LEADING_PUNCT_RE.sub("", text).lower()
        for phrase, person_id in self._self_id:
            if normalised.startswith(phrase):
                tail_pos = len(phrase)
                if tail_pos >= len(normalised):
                    return self._people.get(person_id)
                tail = normalised[tail_pos]
                if not tail.isalnum() and tail != "_":
                    return self._people.get(person_id)
        return None

    def reload(self) -> None:
        """Force reload regardless of mtime. Mostly for tests."""
        self._mtime = 0.0
        self._reload()

    def _reload_if_changed(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            return
        if stat.st_mtime != self._mtime:
            self._reload()

    def _reload(self) -> None:
        people: dict[str, Person] = {}
        by_prefix: dict[str, str] = {}
        self_id: list[tuple[str, str]] = []
        default_person = DEFAULT_PERSON_FALLBACK

        if not _YAML_AVAILABLE or yaml is None:
            self._commit(people, by_prefix, self_id, default_person, mtime=0.0)
            return
        try:
            stat = self._path.stat()
        except OSError:
            log.info(
                "household: %s not found — registry empty (resolves to %s)",
                self._path, default_person,
            )
            self._commit(people, by_prefix, self_id, default_person, mtime=0.0)
            return

        try:
            raw = self._path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
        except (OSError, yaml.YAMLError):
            log.warning(
                "household: %s unreadable/malformed — registry empty",
                self._path, exc_info=True,
            )
            self._commit(
                people, by_prefix, self_id, default_person, mtime=stat.st_mtime,
            )
            return

        if not isinstance(data, dict):
            log.warning("household: top-level YAML is not a mapping; ignored")
            self._commit(
                people, by_prefix, self_id, default_person, mtime=stat.st_mtime,
            )
            return

        default_person = str(
            data.get("default_person") or DEFAULT_PERSON_FALLBACK
        )
        people_block = data.get("people") or {}
        if not isinstance(people_block, dict):
            log.warning("household: `people:` is not a mapping; ignored")
            people_block = {}

        for raw_id, entry in people_block.items():
            try:
                person = self._parse_person(str(raw_id), entry)
            except Exception:
                log.warning(
                    "household: skipped malformed entry %r", raw_id,
                    exc_info=True,
                )
                continue
            if person is None:
                continue
            people[person.id] = person
            if person.calendar_prefix:
                by_prefix[person.calendar_prefix.strip().lower()] = person.id
            for phrase in person.self_id_phrases:
                norm = phrase.strip().lower()
                if norm:
                    self_id.append((norm, person.id))
        self_id.sort(key=lambda pair: len(pair[0]), reverse=True)

        self._commit(
            people, by_prefix, self_id, default_person, mtime=stat.st_mtime,
        )
        log.info(
            "household: loaded %d people from %s (default=%s)",
            len(people), self._path, default_person,
        )

    def _commit(
        self,
        people: dict[str, Person],
        by_prefix: dict[str, str],
        self_id: list[tuple[str, str]],
        default_person: str,
        *,
        mtime: float,
    ) -> None:
        self._people = people
        self._by_prefix = by_prefix
        self._self_id = self_id
        self._default_person = default_person
        self._mtime = mtime

    @staticmethod
    def _parse_person(raw_id: str, entry: Any) -> Optional[Person]:
        if not isinstance(entry, dict):
            return None
        person_id = raw_id.strip().lower()
        if not person_id or person_id == DEFAULT_PERSON_FALLBACK:
            log.warning("household: skipping reserved id %r", raw_id)
            return None
        display_name = str(
            entry.get("display_name") or raw_id
        ).strip() or raw_id

        birthdate_raw = entry.get("birthdate")
        birthdate: Optional[date] = None
        if birthdate_raw is not None:
            if isinstance(birthdate_raw, date):
                birthdate = birthdate_raw
            elif isinstance(birthdate_raw, datetime):
                birthdate = birthdate_raw.date()
            else:
                try:
                    birthdate = date.fromisoformat(str(birthdate_raw))
                except ValueError:
                    log.warning(
                        "household: %s has unparseable birthdate %r",
                        person_id, birthdate_raw,
                    )

        usual_times_raw = entry.get("usual_times") or {}
        usual_times: dict[str, tuple[str, ...]] = {}
        if isinstance(usual_times_raw, dict):
            for key, val in usual_times_raw.items():
                if isinstance(val, (list, tuple)):
                    usual_times[str(key)] = tuple(str(v) for v in val)
                elif isinstance(val, str):
                    usual_times[str(key)] = (val,)

        return Person(
            id=person_id,
            display_name=display_name,
            relation=_opt_str(entry.get("relation")),
            pronouns=_opt_str(entry.get("pronouns")),
            age=_opt_int(entry.get("age")),
            birthdate=birthdate,
            appearance=_opt_str(entry.get("appearance")),
            voice=_opt_str(entry.get("voice")),
            personality=_opt_str(entry.get("personality")),
            interests=_to_str_tuple(entry.get("interests")),
            family_context=_opt_str(entry.get("family_context")),
            notes=_opt_str(entry.get("notes")),
            do_not=_to_str_tuple(entry.get("do_not")),
            self_id_phrases=_to_str_tuple(entry.get("self_id_phrases")),
            usual_times=usual_times,
            calendar_prefix=_opt_str(entry.get("calendar_prefix")),
            voice_print_id=_opt_str(entry.get("voice_print_id")),
        )


def _opt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _opt_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_str_tuple(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return tuple(s.strip() for s in v.split(",") if s.strip())
    if isinstance(v, (list, tuple)):
        return tuple(str(x).strip() for x in v if str(x).strip())
    return ()


__all__ = ["HouseholdRegistry", "Person", "DEFAULT_PERSON_FALLBACK"]
