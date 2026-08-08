from __future__ import annotations

import json

import numpy as np
import pytest

from app.services import discovery as discovery_module
from app.services.discovery import (
    RANKING_COMPONENT_LIMITS,
    best_of_stream,
    filter_profanity,
    preference_features,
    score_candidates,
    suppress_duplicate_groups,
    is_disallowed_reading,
)


def _score(discovery, candidates):
    return score_candidates(candidates, reference=[], profile="general")


def test_probable_reading_is_capped_at_18(candidate_factory, empty_discovery_feedback):
    candidate = candidate_factory(id="reading", quality_score=99, reading_likelihood=0.8)
    ranked = _score(empty_discovery_feedback, [candidate])
    assert ranked[0]["ranking_score"] <= 18
    assert ranked[0]["excluded_from_discovery"] is True


def test_reading_exception_requires_verified_viewer_answer(candidate_factory):
    unrelated_chat = candidate_factory(
        reading_likelihood=0.8, end_seconds=18,
        chat_reaction_score=12, chat_joy_score=8, chat_question_match_score=0,
    )
    verified_answer = candidate_factory(
        reading_likelihood=0.8, end_seconds=18,
        chat_reaction_score=12, chat_joy_score=8, chat_question_match_score=55,
    )
    assert is_disallowed_reading(unrelated_chat) is True
    assert is_disallowed_reading(verified_answer) is False


def test_concise_complete_candidate_beats_long_contextless_candidate(candidate_factory, empty_discovery_feedback):
    concise = candidate_factory(id="concise", end_seconds=20, logical_sense_score=82, context_score=78, self_contained_score=86)
    long = candidate_factory(id="long", end_seconds=64, logical_sense_score=35, context_score=28, self_contained_score=25)
    ranked = _score(empty_discovery_feedback, [concise, long])
    scores = {item["id"]: item["ranking_score"] for item in ranked}
    assert scores["concise"] > scores["long"]


def test_expressive_delivery_beats_monotone_delivery(candidate_factory, empty_discovery_feedback):
    expressive = candidate_factory(id="expressive", voice_expression_score=10)
    monotone = candidate_factory(id="monotone", voice_expression_score=-10)
    ranked = _score(empty_discovery_feedback, [expressive, monotone])
    scores = {item["id"]: item["ranking_score"] for item in ranked}
    assert scores["expressive"] > scores["monotone"]


def test_specialized_profiles_require_their_own_evidence(candidate_factory, empty_discovery_feedback):
    game_reaction = candidate_factory(
        id="game", tags=json.dumps(["reakcja na grę", "zaskoczenie"]),
        game_reaction_score=14, moment_reaction_score=18,
        moment_reaction_stage="game -> voice", end_seconds=18,
    )
    no_sequence = candidate_factory(
        id="no-sequence", tags=json.dumps(["reakcja na grę", "zaskoczenie"]),
        game_reaction_score=0, moment_reaction_score=0, end_seconds=18,
    )
    ranked = score_candidates([game_reaction, no_sequence], reference=[], profile="game_reactions")
    scores = {item["id"]: item["ranking_score"] for item in ranked}
    assert scores["game"] > scores["no-sequence"]

    punchline = candidate_factory(id="punchline", tags=json.dumps(["humor"]), extended_punchline_score=78)
    no_punchline = candidate_factory(id="no-punchline", tags=json.dumps(["humor"]), extended_punchline_score=32)
    ranked = score_candidates([punchline, no_punchline], reference=[], profile="funny_moments")
    scores = {item["id"]: item["ranking_score"] for item in ranked}
    assert scores["punchline"] > scores["no-punchline"]


def test_profanity_filters_allow_one_or_none(candidate_factory):
    clean = candidate_factory(id="clean", transcript="To jest całkowicie spokojna wypowiedź.")
    one = candidate_factory(id="one", transcript="To jest kurwa trudne.")
    two = candidate_factory(id="two", transcript="Kurwa, ale to jest cholernie pojebane.")
    assert {item["id"] for item in filter_profanity([clean, one, two], "allow")} == {"clean", "one", "two"}
    assert {item["id"] for item in filter_profanity([clean, one, two], "one")} == {"clean", "one"}
    assert {item["id"] for item in filter_profanity([clean, one, two], "none")} == {"clean"}


def test_duplicate_suppression_keeps_highest_ranked_variant(candidate_factory):
    stronger = candidate_factory(id="stronger", duplicate_group="moment", ranking_score=80)
    weaker = candidate_factory(id="weaker", duplicate_group="moment", ranking_score=60)
    other = candidate_factory(id="other", duplicate_group="other", ranking_score=55)
    result = suppress_duplicate_groups([weaker, other, stronger])
    assert [item["id"] for item in result] == ["stronger", "other"]


def test_best_of_stream_uses_different_moments(candidate_factory):
    candidates = [
        candidate_factory(id="a", start_seconds=0, end_seconds=15, ranking_score=90, tags=json.dumps(["humor"])),
        candidate_factory(id="b", start_seconds=5, end_seconds=20, ranking_score=89, tags=json.dumps(["humor"])),
        candidate_factory(id="c", start_seconds=300, end_seconds=315, ranking_score=80, tags=json.dumps(["zaskoczenie"])),
    ]
    selected = best_of_stream(candidates, limit=2)
    assert {item["id"] for item in selected} == {"a", "c"}


def test_real_zero_feature_is_not_replaced_with_neutral_value(candidate_factory):
    candidate = candidate_factory(
        logical_sense_score=0,
        context_score=0,
        self_contained_score=0,
        extended_completeness_score=0,
        chat_question_match_score=0,
    )
    features = preference_features(candidate)["values"]
    assert features["logical_sense"] == 0
    assert features["context"] == 0
    assert features["self_contained"] == 0
    assert features["extended_completeness"] == 0
    assert features["chat_question_match"] == 0


def test_real_zero_scores_lower_than_unknown_neutral(candidate_factory, empty_discovery_feedback):
    zero = candidate_factory(
        id="zero",
        logical_sense_score=0,
        context_score=0,
        self_contained_score=0,
        extended_completeness_score=0,
    )
    unknown = candidate_factory(
        id="unknown",
        logical_sense_score=-1,
        context_score=-1,
        self_contained_score=-1,
        extended_completeness_score=-1,
    )
    ranked = _score(empty_discovery_feedback, [zero, unknown])
    by_id = {item["id"]: item for item in ranked}
    assert by_id["zero"]["ranking_components"]["coherence"] < by_id["unknown"]["ranking_components"]["coherence"]
    assert by_id["zero"]["ranking_score"] < by_id["unknown"]["ranking_score"]


def test_one_feedback_stays_below_every_learning_threshold(
    monkeypatch, candidate_factory, empty_discovery_feedback,
):
    candidate = candidate_factory()
    vector = np.asarray([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
    row = {
        "decision": "accepted",
        "review_reason": "",
        "embedding": candidate["embedding"],
        "features": json.dumps(preference_features(candidate), ensure_ascii=False),
    }
    empty = np.empty((0, 0), dtype=np.float32)
    monkeypatch.setattr(
        discovery_module,
        "_profile_feedback",
        lambda _profile: (vector, empty, {}, [row], []),
    )

    ranked = score_candidates([candidate], reference=[], profile="general")[0]
    # One decision is useful data, but is below every learning threshold and
    # therefore cannot be amplified as embedding + features + tags.
    assert ranked["profile_feedback_score"] == 0
    assert ranked["ranking_components"]["preference"] == 0


def test_ranking_components_sum_to_raw_score_and_respect_limits(
    candidate_factory, empty_discovery_feedback,
):
    candidate = candidate_factory(
        game_reaction_score=13,
        moment_reaction_score=16,
        chat_reaction_score=10,
        chat_joy_score=7,
        voice_expression_score=8,
        tags=json.dumps(["humor", "reakcja na grę"]),
    )
    ranked = _score(empty_discovery_feedback, [candidate])[0]
    components = ranked["ranking_components"]
    assert set(components) == set(RANKING_COMPONENT_LIMITS)
    assert ranked["ranking_raw_score"] == pytest.approx(sum(components.values()))
    for name, value in components.items():
        lower, upper = RANKING_COMPONENT_LIMITS[name]
        assert lower <= value <= upper


def test_99_requires_exceptional_complete_evidence(candidate_factory, empty_discovery_feedback):
    common = {
        "quality_score": 99,
        "logical_sense_score": 95,
        "context_score": 92,
        "self_contained_score": 96,
        "game_reaction_score": 20,
        "moment_reaction_score": 30,
        "moment_reaction_stage": "game -> voice -> chat",
        "chat_reaction_score": 20,
        "chat_joy_score": 14,
        "chat_question_match_score": 99,
        "voice_expression_score": 20,
        "tags": json.dumps(["humor", "reakcja na grę"]),
    }
    incomplete = candidate_factory(id="incomplete", extended_completeness_score=70, **common)
    exceptional = candidate_factory(id="exceptional", extended_completeness_score=92, **common)
    ranked = score_candidates(
        [incomplete, exceptional],
        reference=[[1.0, 0.0, 0.0, 0.0]],
        profile="general",
    )
    by_id = {item["id"]: item for item in ranked}
    assert by_id["incomplete"]["ranking_raw_score"] >= 99
    assert by_id["incomplete"]["ranking_score"] <= 98
    assert by_id["incomplete"]["ranking_exceptional"] is False
    assert by_id["exceptional"]["ranking_score"] == 99
    assert by_id["exceptional"]["ranking_exceptional"] is True
