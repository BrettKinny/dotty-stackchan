"""Deterministic routes for explicit voice-tool requests (#165).

Under the production prompt qwen3.5:4b, pi's outer loop, answers explicit
tool requests itself: "remember that ..." got a spoken "I'll remember that"
with no `remember` call, and "think hard about ..." got the 4B's own guess.
These parsers recognise a short, literal list of phrasings and name the tool
to run, so PiVoiceLLM can run it without waiting for the model to decide to.

Narrow by design: every pattern is anchored to the start or end of the
utterance, there is no fuzzy matching, and anything not listed here goes to
the model, which still has every tool registered. Quoted, negated and
question-shaped variants ("remember when ...", "I can't remember that",
"don't think hard") deliberately fall through.

Song and camera requests are mostly caught earlier, in xiaozhi-server's
receiveAudioHandle.py (the dance/song and vision shortcuts), and never reach
this provider. The `take_photo` route here is the fallback for when that
shortcut could not produce a description: without it the 4B describes a room
it has not seen.

AI-assisted: drafted by Claude (Opus 5.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceIntent:
    """A voice tool to run directly, and the argument to give it."""

    tool: str
    argument: str = ""


# ---------------------------------------------------------------------------
# Input cleanup
# ---------------------------------------------------------------------------

_ROOM_VIEW_MARKER = "[ROOM_VIEW]\n"
_PERSON_ID_RE = re.compile(r"^[a-z0-9_-]*$")


def split_room_view(text: str) -> tuple[str, str]:
    """Separate xiaozhi's room-view prefix from what the person said.

    receiveAudioHandle._with_room_view_marker prepends
    ``[ROOM_VIEW]\\n<description>\\n<person_id or blank>\\n<text>`` to every
    voice turn while a face is cached. The retired ZeroClaw provider stripped
    it; PiVoiceLLM did not, so the marker reached pi as the start of the
    user's words, which broke every anchored route above and was logged to
    memory. Returns ``(description, spoken_text)``; text without the marker
    comes back unchanged with an empty description.
    """
    if not text.startswith(_ROOM_VIEW_MARKER):
        return "", text
    parts = text[len(_ROOM_VIEW_MARKER):].split("\n", 2)
    if len(parts) == 3 and _PERSON_ID_RE.match(parts[1].strip().lower()):
        return parts[0].strip(), parts[2]
    if len(parts) >= 2:
        # v1 shape (no person-id line).
        return parts[0].strip(), "\n".join(parts[1:])
    return "", text


# "Dotty, ..." / "Hey Dotty ..." / "..., Dotty." The look-ahead keeps
# "Dotty's favourite colour" intact.
_LEADING_ADDRESS_RE = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay|oi)[\s,]+)?dotty(?![\w'])[\s,.!:;-]*",
    re.IGNORECASE,
)
_TRAILING_ADDRESS_RE = re.compile(r"[\s,]+dotty(?![\w'])(?=[\s.!?]*$)", re.IGNORECASE)


def _clean(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    text = " ".join(text.split())
    text = _LEADING_ADDRESS_RE.sub("", text, count=1)
    text = _TRAILING_ADDRESS_RE.sub("", text, count=1)
    return text.strip()


# ---------------------------------------------------------------------------
# Phrase families
# ---------------------------------------------------------------------------

# Optional politeness / request framing in front of a command.
_ASK = (
    r"(?:(?:i (?:want|need) you to|(?:can|could|will|would) you)\s+)?"
    r"(?:please\s+)?"
)
_REMEMBER_VERB = r"(?:remember|memori[sz]e|don'?t forget|do not forget|never forget)"

_RECALL_RE = re.compile(
    r"^(?:please\s+)?"
    r"(?:do you (?:still )?(?:remember|recall)"
    r"|what did i (?:tell|say to) you about"
    r"|what do you remember about"
    r"|(?:can|could) you (?:remember|recall)(?=\s+(?:what|who|where|when|which|how)\b))"
    r"[\s,:]*(?:about\s+)?(?P<query>.+?)[\s?!.]*$",
    re.IGNORECASE,
)

# "What do you know about my friend Sam Baker?" names a person to look up.
# Needs the relationship word, so "what do you know about dinosaurs" stays
# general knowledge, and a short name (one to three words).
_PERSON_RE = re.compile(
    r"^(?:please\s+)?(?:what do you know|what can you tell me|tell me what you know)"
    r" about my (?:best |good |old )?"
    r"(?:friend|mate|buddy|pal|neighbou?r|teacher|cousin|classmate|aunt|aunty|auntie|uncle)"
    r"[\s,]+(?P<name>[a-z][\w'.-]*(?:\s+[a-z][\w'.-]*){0,2})[\s?!.]*$",
    re.IGNORECASE,
)

_REMEMBER_LEAD_RE = re.compile(
    rf"^{_ASK}{_REMEMBER_VERB}\b[\s,:;-]*(?P<that>(?:that|this)\b[\s,:;-]*)?(?P<fact>.+?)[\s.!?]*$",
    re.IGNORECASE,
)
# "My favourite colour is purple, please remember that." A bare-space join
# needs "please" / "can you" so "I can't remember that" stays conversation.
_REMEMBER_TAIL_RE = re.compile(
    r"^(?P<fact>.+?)"
    rf"(?:\s*[,.;:!?-]+\s*(?:so\s+)?{_ASK}"
    r"|\s+(?:so\s+)?(?:please\s+|(?:can|could|will|would) you\s+(?:please\s+)?))"
    rf"{_REMEMBER_VERB}\s+(?:that|this|it)(?:\s+(?:please|for me|ok|okay))?[\s.!?]*$",
    re.IGNORECASE,
)
# A "fact" opening with one of these is a question, an instruction for later
# or a pronoun ("remember when ...", "remember to ...", "remember me?").
_NOT_A_FACT_RE = re.compile(
    r"^(?:to|when|what|what's|who|who's|whom|whose|where|why|how|which|if|whether"
    r"|me|it|that|this|anything|everything|something)\b",
    re.IGNORECASE,
)
_NEGATED_TAIL_RE = re.compile(
    r"\b(?:can't|cannot|can not|don't|do not|won't|didn't|never|not|i)$", re.IGNORECASE,
)

_THINK_VERB = r"(?:think (?:really |very |extra |super )?hard|have a (?:really )?hard think)"
_THINK_LEAD_RE = re.compile(
    rf"^{_ASK}{_THINK_VERB}"
    r"(?:\s+(?:about|on)\s+(?:this|that|it)\b)?[\s,]*(?:(?:about|on)\b)?[\s,:;.-]*"
    r"(?P<question>.+?)[\s?!.]*$",
    re.IGNORECASE,
)
_THINK_TAIL_RE = re.compile(
    rf"^(?P<question>.+?)\s*[,.;:!?-]+\s*(?:and\s+|so\s+)?{_ASK}{_THINK_VERB}"
    r"(?:\s+(?:about|on)\s+(?:this|that|it))?[\s.!?]*$",
    re.IGNORECASE,
)
_PRONOUN_ONLY = {"it", "this", "that", "something", "stuff", "things"}

_PHOTO_RE = re.compile(
    rf"^{_ASK}"
    r"(?:take (?:a |my |our |another )?(?:photo|picture|pic|snapshot)\b"
    r"|look (?:at (?:this|that|me|us|it)|around)\b"
    r"|what (?:do|can) you see\b"
    r"|what(?:'s| is) in front of you\b"
    r"|(?:can|do) you see (?:this|that|me|us|anything)\b"
    # "Please use your camera and tell me about the room": left to the 4B
    # model it described a room it never photographed (robot, 2026-10-20).
    r"|use (?:your|the) camera\b"
    r"|(?:describe|tell me (?:about|what(?:'s| is) in)) (?:the|this) room"
    r"(?:[\s,.!?]*$|\s+in (?:one|two|a|a few) ))",
    re.IGNORECASE,
)

# Question scaffolding around a recall query. memory_lookup is an FTS5
# phrase match, so "what my favourite colour is" would never match the
# stored "my favourite colour is purple".
_QUERY_LEAD_RE = re.compile(r"^(?:that|what|who|where|when|which|how)\s+", re.IGNORECASE)
_QUERY_TAIL_RE = re.compile(r"\s+(?:is|are|was|were)$", re.IGNORECASE)


def _has_words(text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", text))


def _recall_query(raw: str) -> str:
    query = raw.strip()
    stripped = _QUERY_TAIL_RE.sub("", _QUERY_LEAD_RE.sub("", query, count=1), count=1)
    return stripped if _has_words(stripped) else query


def _remember_fact(match: re.Match | None, *, min_words: int = 1) -> str | None:
    if match is None:
        return None
    fact = match.group("fact").strip(" ,;:-").lstrip(".\u2026 ")
    if not _has_words(fact) or _NOT_A_FACT_RE.match(fact):
        return None
    if len(fact.split()) < min_words or _NEGATED_TAIL_RE.search(fact):
        return None
    return fact


def _think_question(match: re.Match | None) -> str | None:
    if match is None:
        return None
    question = match.group("question").strip(" ,;:-")
    if not _has_words(question) or question.lower() in _PRONOUN_ONLY:
        return None
    return question


def match_voice_intent(text: str) -> VoiceIntent | None:
    """The tool an explicit request names, or None to let the model decide.

    ``text`` is what the person said (room-view prefix already removed).
    Bracketed prompts (``[STATE_CHANGE:...]``, ``[CAMERA_DISABLED]``, the
    vision shortcut's ``[You just used your camera ...]``) are written by
    xiaozhi-server, not spoken, and are never routed.
    """
    cleaned = _clean(text or "")
    if not cleaned or cleaned.startswith("["):
        return None

    person = _PERSON_RE.match(cleaned)
    if person:
        return VoiceIntent("recall_person", person.group("name"))

    recall = _RECALL_RE.match(cleaned)
    if recall and _has_words(recall.group("query")):
        return VoiceIntent("memory_lookup", _recall_query(recall.group("query")))

    lead = _REMEMBER_LEAD_RE.match(cleaned)
    # "Can you remember my birthday?" asks; "... remember that my birthday
    # is in May?" tells. Only the second is a fact to store.
    if lead and cleaned.endswith("?") and not lead.group("that"):
        lead = None
    fact = _remember_fact(lead)
    if fact is None:
        fact = _remember_fact(_REMEMBER_TAIL_RE.match(cleaned), min_words=2)
    if fact is not None:
        return VoiceIntent("remember", fact)

    question = _think_question(_THINK_LEAD_RE.match(cleaned))
    if question is None:
        question = _think_question(_THINK_TAIL_RE.match(cleaned))
    if question is not None:
        return VoiceIntent("think_hard", question)

    if _PHOTO_RE.match(cleaned):
        return VoiceIntent("take_photo", cleaned)

    return None
