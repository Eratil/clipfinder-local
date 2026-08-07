"""Canonical semantic-tag vocabulary and legacy compatibility helpers.

Tags are persisted in recordings and user feedback, so changing a label in
one producer can otherwise split one concept into several statistics.  This
module is the single normalization boundary shared by tag inference,
deduplication and evidence enrichment.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


GAME_REACTION_TAG = "reakcja na grę"
CHAT_QUESTION_TAG = "pytanie"
CHAT_QUESTION_ANSWER_TAG = "odpowiedź na pytanie widza"
READING_TAG = "format: czytanie"

# Diagnostic tags are recalculated from scores and therefore kept separate
# from semantic content categories.
CONTEXT_TAG_PREFIXES = (
    "reakcja: ", "kontekst: ", "struktura: ", "format: ", "moment: ",
    "wypowiedź: ",
)

_EMOTION_TAGS = frozenset({
    "radość", "złość", "gniew", "smutek", "zaskoczenie",
    "emocja: śmiech", "emocja: frustracja", "emocja: zachwyt",
    "emocja: rozczarowanie", "emocja: szok",
})
_HUMOUR_TAGS = frozenset({"humor"})
_SPEECH_FORM_TAGS = frozenset({
    "wyrażanie opinii", "rekomendacja", "forma: opinia", "forma: rada",
    "forma: krytyka", "forma: porównanie", "forma: decyzja",
    "forma: przewidywanie", "forma: historia", "forma: puenta",
})
_DIAGNOSTIC_TAGS = frozenset({
    READING_TAG,
    "struktura: urwana wypowiedź", "struktura: wymaga kontekstu",
    "struktura: zależny od kontekstu", "struktura: samowystarczalny",
    "struktura: samodzielna myśl", "wypowiedź: ekspresyjna",
    "wypowiedź: jednostajna",
})
_CANONICAL_TAGS = (
    _EMOTION_TAGS | _HUMOUR_TAGS | _SPEECH_FORM_TAGS | _DIAGNOSTIC_TAGS
    | {GAME_REACTION_TAG, CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG}
)


def _key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


_CANONICAL_BY_KEY = {_key(tag): tag for tag in _CANONICAL_TAGS}
_REMOVED_LEGACY_TAGS = frozenset({
    "forma: pytanie",
})
_ALIASES = {
    # Reading used to exist under two unscoped English/Polish labels.
    "reading": READING_TAG,
    "czytanie": READING_TAG,
    "format: reading": READING_TAG,
    # Viewer-question evidence is not the same as the streamer's speech form.
    "pytanie widza": CHAT_QUESTION_TAG,
    "pytanie czatu": CHAT_QUESTION_TAG,
    "viewer question": CHAT_QUESTION_TAG,
    "forma: odpowiedź na pytanie czatu": CHAT_QUESTION_ANSWER_TAG,
    "forma: odpowiedź na pytanie widza": CHAT_QUESTION_ANSWER_TAG,
    "forma: odpowiedź na pytanie": CHAT_QUESTION_ANSWER_TAG,
    "odpowiedź na pytanie": CHAT_QUESTION_ANSWER_TAG,
    "odpowiedź na pytanie czatu": CHAT_QUESTION_ANSWER_TAG,
    "answer to viewer question": CHAT_QUESTION_ANSWER_TAG,
    # An older evidence label described the same game-reaction concept.
    "reakcja: gra": GAME_REACTION_TAG,
}


def canonical_tag(value: Any) -> str | None:
    """Return the canonical persisted label, or ``None`` for an invalid tag.

    Unknown non-empty labels are intentionally preserved.  Saved user tags
    and tags introduced by a future release must not disappear merely because
    an older runtime does not know their category yet.
    """
    key = _key(value)
    if not key or key in _REMOVED_LEGACY_TAGS:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    if key in _CANONICAL_BY_KEY:
        return _CANONICAL_BY_KEY[key]
    return " ".join(str(value).strip().split())


def canonicalize_tags(values: Iterable[Any] | None) -> list[str]:
    """Normalize and stably deduplicate tag values in first-seen order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        tag = canonical_tag(value)
        if tag is None:
            continue
        identity = tag.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        result.append(tag)
    return result


def tag_category(value: Any) -> str:
    """Return a stable category used for presentation and deduplication."""
    tag = canonical_tag(value)
    if tag is None:
        return "invalid"
    if tag == CHAT_QUESTION_TAG:
        return "viewer_question"
    if tag == CHAT_QUESTION_ANSWER_TAG:
        return "viewer_question_answer"
    if tag == GAME_REACTION_TAG:
        return "game_reaction"
    if tag in _EMOTION_TAGS:
        return "emotion"
    if tag in _HUMOUR_TAGS:
        return "humour"
    if tag in _SPEECH_FORM_TAGS:
        return "speech_form"
    lowered = tag.casefold()
    for prefix in CONTEXT_TAG_PREFIXES:
        if lowered.startswith(prefix):
            return f"diagnostic:{prefix.rstrip(': ')}"
    return f"tag:{lowered}"

