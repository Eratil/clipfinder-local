from __future__ import annotations

from app.services.tag_taxonomy import (
    CHAT_QUESTION_ANSWER_TAG,
    CHAT_QUESTION_TAG,
    READING_TAG,
    canonical_tag,
    canonicalize_tags,
    tag_category,
)
from app.services.tagging import deduplicate_content_tags, enrich_tags


def test_legacy_aliases_normalize_without_duplicate_values():
    assert canonical_tag("reading") == READING_TAG
    assert canonical_tag(" czytanie ") == READING_TAG
    assert canonicalize_tags(["reading", "czytanie", "FORMAT: CZYTANIE"]) == [READING_TAG]
    assert canonical_tag("forma: pytanie") is None


def test_viewer_question_and_answer_are_independent_from_speech_form():
    tags = deduplicate_content_tags([
        "forma: opinia",
        "pytanie widza",
        "forma: odpowiedź na pytanie czatu",
    ])
    assert tags == ["forma: opinia", CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG]
    assert tag_category(CHAT_QUESTION_TAG) == "viewer_question"
    assert tag_category(CHAT_QUESTION_ANSWER_TAG) == "viewer_question_answer"
    assert tag_category("forma: opinia") == "speech_form"


def test_canonical_deduplication_is_stable_and_removes_false_question_tag():
    source = [" HUMOR ", "humor", "forma: pytanie", "reading", "czytanie"]
    assert canonicalize_tags(source) == ["humor", READING_TAG]
    assert deduplicate_content_tags(source) == ["humor", READING_TAG]
    assert deduplicate_content_tags(source) == deduplicate_content_tags(list(source))


def test_enrichment_replaces_stale_diagnostics_with_canonical_reading_tag():
    tags = enrich_tags(
        ["reading", "struktura: urwana wypowiedź", "forma: opinia"],
        reading_likelihood=0.7,
        logical_sense_score=80,
        self_contained_score=80,
    )
    assert READING_TAG in tags
    assert "reading" not in tags
    assert "struktura: urwana wypowiedź" not in tags
    assert "struktura: samowystarczalny" in tags
    assert "forma: opinia" in tags
