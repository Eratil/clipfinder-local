from __future__ import annotations

from app.services.analysis_history import (
    PIPELINE_VERSION,
    SCORING_VERSION,
    TAGGING_VERSION,
    has_human_data,
    match_moments,
    match_signals,
    normalize_match_text,
    token_jaccard,
)


def old(segment_id: str, start: float, end: float, text: str, **extra) -> dict:
    return {
        "id": segment_id,
        "start_seconds": start,
        "end_seconds": end,
        "transcript": text,
        **extra,
    }


def new(segment_id: str, start: float, end: float, text: str) -> dict:
    return {"id": segment_id, "start": start, "end": end, "text": text}


def test_algorithm_versions_are_persistable_non_empty_strings():
    assert all(isinstance(value, str) and value for value in (PIPELINE_VERSION, SCORING_VERSION, TAGGING_VERSION))


def test_text_normalisation_and_token_jaccard_handle_polish_punctuation():
    assert normalize_match_text("  ŻÓŁĆ, naprawdę?! ") == "zołc naprawde"
    assert token_jaccard("To jest dobry klip", "dobry klip, to jest") == 1.0


def test_identical_revision_has_full_signals_and_high_confidence():
    previous = old("old", 10, 25, "To jest ten sam ciekawy fragment")
    current = new("new", 10, 25, "To jest ten sam ciekawy fragment")
    signals = match_signals(previous, current)
    assert signals.iou == 1.0
    assert signals.previous_coverage == 1.0
    assert signals.new_coverage == 1.0
    assert signals.duration_ratio == 1.0
    result = match_moments([previous], [current])
    assert [(item.previous_id, item.new_id, item.confidence) for item in result.matches] == [
        ("old", "new", "high")
    ]


def test_reasonable_boundary_drift_is_matched_but_container_candidate_is_not():
    previous = old("old", 100, 120, "Zaraz opowiem wam dlaczego ten boss jest ciekawy")
    drifted = new("drifted", 98, 123, "Opowiem wam dlaczego ten boss jest naprawdę ciekawy")
    container = new("container", 70, 150, "Opowiem wam dlaczego ten boss jest naprawdę ciekawy")
    result = match_moments([previous], [drifted, container])
    assert [item.new_id for item in result.matches] == ["drifted"]
    assert result.matches[0].confidence in {"high", "medium"}
    assert result.unmatched_new_ids == ("container",)


def test_same_window_with_different_speech_does_not_inherit_review():
    previous = old("old", 30, 45, "Moim zdaniem ta mechanika nie działa i zaraz powiem dlaczego")
    current = new("new", 30, 45, "Czytam instrukcję zadania numer trzy i przechodzę do następnego punktu")
    result = match_moments([previous], [current])
    assert not result.matches
    assert result.unmatched_previous_ids == ("old",)
    assert result.unmatched_new_ids == ("new",)


def test_match_is_deterministic_one_to_one_regardless_of_query_order():
    previous = [
        old("old-a", 0, 12, "ta sama wypowiedź testowa"),
        old("old-b", 0, 12, "ta sama wypowiedź testowa"),
    ]
    current = [
        new("new-a", 0, 12, "ta sama wypowiedź testowa"),
        new("new-b", 0, 12, "ta sama wypowiedź testowa"),
    ]
    first = match_moments(previous, current)
    second = match_moments(reversed(previous), reversed(current))
    first_pairs = [(item.previous_id, item.new_id) for item in first.matches]
    second_pairs = [(item.previous_id, item.new_id) for item in second.matches]
    assert first_pairs == second_pairs == [("old-a", "new-a"), ("old-b", "new-b")]
    assert len({item.previous_id for item in first.matches}) == len(first.matches)
    assert len({item.new_id for item in first.matches}) == len(first.matches)


def test_every_existing_segment_can_be_linked_not_only_reviewed_ones():
    previous = [
        old("unrated", 10, 20, "pierwszy neutralny fragment", rating="unrated"),
        old("reviewed", 40, 50, "drugi oceniony fragment", rating="approved"),
    ]
    current = [
        new("fresh-1", 10.2, 20.1, "pierwszy neutralny fragment"),
        new("fresh-2", 39.8, 50.0, "drugi oceniony fragment"),
    ]
    result = match_moments(previous, current)
    assert {(item.previous_id, item.new_id) for item in result.matches} == {
        ("unrated", "fresh-1"),
        ("reviewed", "fresh-2"),
    }


def test_human_data_detection_covers_reviews_feedback_and_manual_edits():
    assert not has_human_data({"rating": "unrated", "review_reason": ""})
    assert has_human_data({"rating": "approved"})
    assert has_human_data({"rating": "unrated", "review_reason": "za długi"})
    assert has_human_data({"rating": "unrated", "caption_edited": 1})
    assert has_human_data({"rating": "unrated"}, tag_feedback=[{"tag": "humor", "verdict": "correct"}])
    assert has_human_data({"rating": "unrated"}, collection_examples=[{"collection_id": "best"}])
    assert has_human_data({"rating": "unrated"}, preference_feedback=[{"rating": "rejected"}])

