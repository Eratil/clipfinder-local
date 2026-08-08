from __future__ import annotations

import pytest

from app.services.feature_graph import (
    FEATURE_GRAPH,
    FEATURE_SCHEMA_VERSION,
    SOURCE_MODE,
    SOURCE_TRANSCRIPT,
    FeatureGraph,
    FeatureNode,
    recompute_segment_features,
)
from app.services.tagging import CHAT_QUESTION_ANSWER_TAG, CHAT_QUESTION_TAG, GAME_REACTION_TAG


@pytest.fixture
def segment(timed_words):
    transcript = "Moim zdaniem ta walka była świetna, bo zaryzykowałem wszystko i wygrałem!"
    return {
        "transcript": transcript,
        "start_seconds": 10.0,
        "end_seconds": 26.0,
        "word_timestamps": timed_words(transcript, 10.0, 26.0),
        "tags": ["forma: opinia"],
        "context_before": "Właśnie rozpoczęła się ostatnia faza walki.",
        "context_after": "Za chwilę idziemy dalej.",
        "extended_completeness_score": 80,
        "game_reaction_score": 12,
        "voice_expression_score": 9,
        "vision_score": 7,
        "chat_reaction_score": 9,
        "chat_joy_score": 7,
        "chat_question_match_score": 0,
        "analysis_mode": "extended",
        "boundary_signals": ["start aligned to sentence", "end aligned to sentence"],
    }


def test_default_graph_has_dependency_safe_order():
    assert FEATURE_SCHEMA_VERSION == "3"
    order = FEATURE_GRAPH.topological_order
    positions = {name: index for index, name in enumerate(order)}
    for name, dependencies in FEATURE_GRAPH.dependencies.items():
        for dependency in dependencies:
            if dependency in positions:
                assert positions[dependency] < positions[name]


def test_full_recomputation_returns_public_updates_without_mutating_input(segment):
    original = dict(segment)
    result = recompute_segment_features(segment)

    assert result.recomputed_nodes == FEATURE_GRAPH.topological_order
    assert not result.skipped_nodes
    assert segment == original
    assert 1 <= result.updates["quality_score"] <= 99
    assert 1 <= result.updates["logical_sense_score"] <= 99
    assert 1 <= result.updates["context_score"] <= 99
    assert 1 <= result.updates["self_contained_score"] <= 99
    assert 1 <= result.updates["short_potential_score"] <= 99
    assert 0 <= result.updates["text_reading_likelihood"] <= 1
    assert 0 <= result.updates["extended_reading_likelihood"] <= 1
    assert 0 <= result.updates["reading_likelihood"] <= 1
    assert result.updates["extended_completeness_score"] >= 1
    assert "start aligned to sentence" in result.updates["quality_signals"]
    assert result.updates["moment_reaction_stage"] == "game -> voice -> chat"
    assert all(not key.startswith("_") for key in result.updates)


def test_chat_change_only_recomputes_chat_dependants(segment):
    result = recompute_segment_features(segment, {"chat_reaction_score", "chat_joy_score"})

    assert result.recomputed_nodes == (
        "moment_reaction", "tag_enrichment", "quality", "short_potential",
    )
    assert "logical_sense_score" not in result.updates
    assert "context_score" not in result.updates
    assert "reading_likelihood" not in result.updates
    assert result.updates["moment_reaction_score"] > segment.get("moment_reaction_score", 0)


def test_context_change_recomputes_context_and_downstream_only(segment):
    result = recompute_segment_features(segment, {"context_before"})

    assert result.recomputed_nodes == (
        "extended_analysis", "effective_reading", "logical_sense", "context",
        "tag_enrichment", "quality", "short_potential",
    )
    assert "moment_reaction_score" not in result.updates
    assert "logical_sense_score" in result.updates


def test_transcript_change_recomputes_complete_feature_chain(segment):
    result = recompute_segment_features(segment, {"transcript"})

    assert result.recomputed_nodes == (
        "speech_quality", "moment_reaction", "extended_analysis",
        "effective_reading", "logical_sense", "context",
        "tag_enrichment", "quality", "short_potential",
    )
    assert result.updates["chat_question_match_score"] == 0
    assert result.updates["chat_question_text"] == ""
    assert result.updates["boundary_signals"] == []


def test_missing_neighbour_context_preserves_existing_context_scores(segment):
    segment.pop("context_before")
    segment.pop("context_after")
    segment["context_score"] = 77
    segment["self_contained_score"] = 81

    result = recompute_segment_features(segment, {"transcript"})

    assert result.skipped_nodes == ("context",)
    assert "context" not in result.recomputed_nodes
    assert "context_score" not in result.updates
    assert "self_contained_score" not in result.updates
    assert "quality" in result.recomputed_nodes


def test_json_encoded_lists_are_accepted(segment):
    segment["tags"] = '["forma: opinia"]'
    segment["word_timestamps"] = "[]"
    result = recompute_segment_features(segment, {"tags"})
    assert "forma: opinia" in result.updates["tags"]


def test_unknown_patch_field_is_a_noop(segment):
    result = recompute_segment_features(segment, {"caption_position"})
    assert result.updates == {}
    assert result.recomputed_nodes == ()


def test_invalid_dependency_and_cycle_are_rejected():
    noop = lambda _state: {}
    with pytest.raises(ValueError, match="Unknown dependencies"):
        FeatureGraph((FeatureNode("one", ("source:typo",), (), noop),))

    with pytest.raises(ValueError, match="cycle"):
        FeatureGraph((
            FeatureNode("one", ("two",), (), noop),
            FeatureNode("two", ("one",), (), noop),
        ))

    with pytest.raises(ValueError, match="Duplicate feature output.*shared"):
        FeatureGraph((
            FeatureNode("one", (), ("shared",), noop),
            FeatureNode("two", (), ("shared",), noop),
        ))


def test_graph_does_not_call_embedding_models(monkeypatch, segment):
    from app.services import tagging

    monkeypatch.setattr(tagging, "embed_texts", lambda _texts: pytest.fail("model must not load"))
    result = recompute_segment_features(segment, {SOURCE_TRANSCRIPT})
    assert result.updates["tags"]


def test_timing_change_invalidates_old_range_evidence_unless_fresh(segment):
    segment.update({
        "tags": ["forma: opinia", GAME_REACTION_TAG, CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG],
        "audio_event_score": 14,
        "visual_reading_likelihood": 0.8,
        "chat_question_match_score": 87,
        "chat_question_text": "Co sądzisz o tej walce?",
    })
    stale = recompute_segment_features(segment, {"start_seconds", "end_seconds"})
    for field in (
        "audio_event_score", "game_reaction_score", "voice_expression_score",
        "vision_score", "chat_reaction_score", "chat_joy_score",
        "chat_question_match_score",
    ):
        assert stale.updates[field] == 0
    assert stale.updates["context_before"] == ""
    assert stale.updates["context_after"] == ""
    assert stale.updates["boundary_signals"] == []
    assert stale.updates["chat_question_text"] == ""
    assert not set(stale.updates["tags"]).intersection({
        GAME_REACTION_TAG, CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG,
    })

    fresh_state = {
        **segment,
        "game_reaction_score": 11,
        "context_before": "Świeży kontekst przed zakresem.",
        "context_after": "Świeży kontekst po zakresie.",
    }
    fresh = recompute_segment_features(
        fresh_state,
        {"start_seconds", "end_seconds", "game_reaction_score", "context_before", "context_after"},
    )
    assert "game_reaction_score" not in fresh.updates
    assert "context_before" not in fresh.updates
    assert GAME_REACTION_TAG in fresh.updates["tags"]


def test_transcript_change_preserves_explicitly_fresh_question_evidence(segment):
    segment.update({
        "chat_question_match_score": 82,
        "chat_question_text": "Jak oceniasz tę walkę?",
    })
    result = recompute_segment_features(
        segment,
        {"transcript", "chat_question_match_score", "chat_question_text"},
    )
    assert "chat_question_match_score" not in result.updates
    assert CHAT_QUESTION_TAG in result.updates["tags"]
    assert CHAT_QUESTION_ANSWER_TAG in result.updates["tags"]


def test_final_extended_reading_is_single_effective_value_and_caps_quality(segment):
    document = (
        "Dyrektywa numer siedem. Zabrania się przechowywania dokumentów. "
        "Wszystkie plakaty mają zostać zniszczone. Zachowaj ostrożność."
    )
    segment.update({
        "analysis_mode": "extended",
        "transcript": document,
        "word_timestamps": [],
        "tags": [],
        "context_before": "Otwieram znalezioną notatkę.",
        "context_after": "Dobra, idziemy dalej.",
        "vision_score": 0,
        "visual_reading_likelihood": 0.0,
    })
    result = recompute_segment_features(segment)
    assert result.updates["extended_reading_likelihood"] >= result.updates["text_reading_likelihood"]
    assert result.updates["reading_likelihood"] >= 0.48
    assert result.updates["quality_score"] <= 22
    assert FEATURE_GRAPH.output_producers["reading_likelihood"] == "effective_reading"


def test_reading_screen_score_is_persisted_as_visual_provenance(segment):
    segment.update({
        "analysis_mode": "default",
        "visual_reading_likelihood": 0.0,
        "reading_screen_score": 8,
    })
    result = recompute_segment_features(segment, {"reading_screen_score"})
    assert result.updates["visual_reading_likelihood"] >= 0.52
    assert result.updates["reading_likelihood"] == result.updates["visual_reading_likelihood"]
    assert FEATURE_GRAPH.output_producers["visual_reading_likelihood"] == "effective_reading"


def test_dynamic_tags_are_rebuilt_from_current_evidence(segment):
    segment.update({
        "tags": ["forma: opinia", GAME_REACTION_TAG, CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG],
        "game_reaction_score": 0,
        "chat_question_match_score": 0,
    })
    cleared = recompute_segment_features(segment, {"game_reaction_score", "chat_question_match_score"})
    assert "forma: opinia" in cleared.updates["tags"]
    assert not set(cleared.updates["tags"]).intersection({
        GAME_REACTION_TAG, CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG,
    })

    # A weak correlation is not enough to label the clip as a game reaction.
    segment.update({"game_reaction_score": 10, "chat_question_match_score": 80})
    below_threshold = recompute_segment_features(segment, {"game_reaction_score", "chat_question_match_score"})
    assert GAME_REACTION_TAG not in below_threshold.updates["tags"]

    segment.update({"game_reaction_score": 12, "chat_question_match_score": 80})
    rebuilt = recompute_segment_features(segment, {"game_reaction_score", "chat_question_match_score"})
    assert {GAME_REACTION_TAG, CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG}.issubset(rebuilt.updates["tags"])


def test_mode_source_deterministically_enables_or_clears_extended_outputs(segment):
    default_state = {**segment, "analysis_mode": "default", "extended_completeness_score": 91}
    default = recompute_segment_features(default_state, {SOURCE_MODE})
    assert default.recomputed_nodes[0] == "extended_analysis"
    assert default.updates["extended_reading_likelihood"] == 0.0
    assert default.updates["extended_hook_score"] == -1
    assert default.updates["extended_ending_score"] == -1
    assert default.updates["extended_story_signals"] == []
    assert default.updates["extended_completeness_score"] == -1

    extended = recompute_segment_features(segment, {SOURCE_MODE})
    assert extended.updates["extended_hook_score"] >= 1
    assert extended.updates["extended_ending_score"] >= 1
    assert extended.updates["extended_completeness_score"] >= 1
