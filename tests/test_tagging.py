from __future__ import annotations

from app.services.tagging import (
    CHAT_QUESTION_ANSWER_TAG,
    CHAT_QUESTION_TAG,
    assess_clip_quality,
    assess_extended_reading_likelihood,
    assess_self_containment,
    assess_short_potential,
    calibrate_quality_score,
    deduplicate_content_tags,
    score_moment_reaction,
)


def test_formal_game_note_is_more_likely_reading_than_personal_opinion():
    note = "Dyrektywa numer siedem. Zabrania się przechowywania plakatów. Wszystkie dokumenty mają zostać zniszczone."
    opinion = "Moim zdaniem społeczeństwo powinno o tym rozmawiać, bo dla mnie ten przepis jest po prostu absurdalny."
    assert assess_extended_reading_likelihood(note) >= 0.48
    assert assess_extended_reading_likelihood(opinion) < assess_extended_reading_likelihood(note)


def test_reading_signals_reduce_quality(timed_words):
    natural = "Ta walka była świetna, bo na końcu zaryzykowałem i naprawdę udało mi się wygrać!"
    reading = "Zabrania się przechowywania dokumentów. Wszystkie plakaty mają zostać zniszczone. Zachowaj ostrożność."
    natural_score, _signals, natural_reading = assess_clip_quality(natural, timed_words(natural), 0, 16, ["wyrażanie opinii"])
    reading_score, _signals, reading_likelihood = assess_clip_quality(reading, timed_words(reading), 0, 16, [])
    extended = assess_extended_reading_likelihood(reading, base_likelihood=reading_likelihood)
    assert extended > natural_reading
    calibrated_reading, _ = calibrate_quality_score(
        reading_score,
        duration=16,
        tags=[],
        quality_signals=[],
        reading_likelihood=extended,
        logical_sense_score=70,
        context_score=70,
        self_contained_score=75,
        extended_completeness_score=75,
        game_reaction_score=0,
        voice_expression_score=0,
        moment_reaction_score=0,
    )
    assert calibrated_reading <= 22
    assert natural_score > calibrated_reading


def test_complete_sentence_is_more_self_contained_than_cut_thought():
    complete = "Nie kupiłbym tej gry ponownie, ponieważ zakończenie zupełnie mnie rozczarowało."
    cut = "Ale ja tego nie kupię, bo"
    assert assess_self_containment(complete) > assess_self_containment(cut, before="Rozmawialiśmy wcześniej o cenie.", after="to nie ma żadnego sensu.")


def test_ordinary_quality_cannot_reach_99_without_exceptional_evidence():
    score, exceptional = calibrate_quality_score(
        99,
        duration=18,
        tags=["forma: opinia"],
        quality_signals=["natural speaking pace"],
        reading_likelihood=0.0,
        logical_sense_score=75,
        context_score=68,
        self_contained_score=77,
        extended_completeness_score=72,
        game_reaction_score=0,
        voice_expression_score=0,
        moment_reaction_score=0,
    )
    assert score <= 90
    assert exceptional is None


def test_short_potential_rewards_complete_attention_trigger():
    exceptional, _exceptional_signals = assess_short_potential(
        "To był najlepszy moment tej walki, bo zaryzykowałem wszystko i wygrałem!",
        0,
        18,
        ["humor", "reakcja na grę"],
        quality_score=86,
        reading_likelihood=0,
        logical_sense_score=88,
        context_score=80,
        self_contained_score=90,
        extended_completeness_score=88,
        game_reaction_score=12,
        voice_expression_score=11,
        moment_reaction_score=14,
    )
    ordinary, _ = assess_short_potential(
        "No i chyba coś tam później jeszcze zrobiłem, ale",
        0,
        18,
        [],
        quality_score=65,
        logical_sense_score=35,
        context_score=30,
        self_contained_score=28,
        extended_completeness_score=25,
    )
    assert exceptional > ordinary
    assert exceptional >= 95


def test_game_voice_chat_sequence_scores_higher_than_game_voice_only():
    voice_only, voice_stage = score_moment_reaction(12)
    with_chat, chat_stage = score_moment_reaction(12, chat_reaction_score=10, chat_joy_score=8)
    assert with_chat > voice_only
    assert voice_stage == "game -> voice"
    assert chat_stage == "game -> voice -> chat"


def test_question_answer_tag_survives_other_form_tag():
    result = deduplicate_content_tags(["forma: opinia", CHAT_QUESTION_ANSWER_TAG, "pytanie"])
    assert "forma: opinia" in result
    assert CHAT_QUESTION_TAG in result
    assert CHAT_QUESTION_ANSWER_TAG in result
