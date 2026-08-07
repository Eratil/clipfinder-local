from __future__ import annotations

import json
from contextlib import nullcontext

from app.services import chat


class _Cursor:
    def fetchone(self):
        return None


class _Connection:
    def execute(self, *_args, **_kwargs):
        return _Cursor()


def test_chat_rescore_uses_full_segment_and_revision_guard(monkeypatch, timed_words):
    transcript = "To byĹ‚ kompletny i bardzo ciekawy moment tej walki!"
    segment = {
        "id": "moment-1",
        "video_id": "video-1",
        "revision_number": 4,
        "start_seconds": 10.0,
        "end_seconds": 18.0,
        "transcript": transcript,
        "word_timestamps": json.dumps(timed_words(transcript, 10.0, 18.0)),
        "embedding": json.dumps([0.1, 0.2]),
        "tags": json.dumps(["humor"]),
        "quality_score": 72,
        "quality_signals": json.dumps(["previous signal"]),
        "short_potential_score": 70,
        "short_potential_signals": "[]",
        "text_reading_likelihood": 0.02,
        "visual_reading_likelihood": 0.0,
        "extended_reading_likelihood": 0.0,
        "reading_likelihood": 0.02,
        "audio_event_score": 8,
        "game_reaction_score": 8,
        "voice_expression_score": 7,
        "vision_score": 0,
        "logical_sense_score": 75,
        "context_score": 74,
        "context_signals": json.dumps(["complete thought in context"]),
        "self_contained_score": 80,
        "context_before": "Walka dobiega koĹ„ca.",
        "context_after": "UdaĹ‚o siÄ™ wygraÄ‡.",
        "boundary_signals": json.dumps(["end aligned to sentence"]),
        "extended_hook_score": -1,
        "extended_ending_score": -1,
        "extended_story_signals": "[]",
        "extended_completeness_score": -1,
        "chat_reaction_score": 0,
        "chat_joy_score": 0,
        "chat_message_count": 0,
        "chat_unique_authors": 0,
        "chat_surge": 0.0,
        "chat_messages": "[]",
        "chat_question_match_score": 0,
        "chat_question_text": "",
        "analysis_mode": "default",
    }
    messages = [
        {"seconds": 16.0, "author": "a", "message": "HAHA!"},
        {"seconds": 17.0, "author": "b", "message": "ale dobre xD"},
        {"seconds": 18.0, "author": "c", "message": "LOL"},
    ]

    def fake_row(query, _parameters=()):
        if "FROM chat_settings" in query:
            return {"delay_seconds": 6}
        if "FROM videos" in query:
            return {"analysis_mode": "default"}
        raise AssertionError(query)

    segment_queries = []

    def fake_rows(query, _parameters=()):
        if "FROM chat_messages" in query:
            return messages
        if "FROM segments" in query:
            segment_queries.append((query, _parameters))
            return [segment]
        raise AssertionError(query)

    captured = []

    def fake_update(segment_id, values, *, con, expected_revision_number=None):
        captured.append((segment_id, values, expected_revision_number, con))
        return "revision-4"

    monkeypatch.setattr(chat.db, "row", fake_row)
    monkeypatch.setattr(chat.db, "rows", fake_rows)
    connection = _Connection()
    monkeypatch.setattr(chat.db, "connection", lambda: nullcontext(connection))
    monkeypatch.setattr(chat, "update_current_segment_and_revision", fake_update)
    refreshed: list[str] = []
    monkeypatch.setattr(
        chat, "refresh_training_snapshot_if_current", lambda segment_id: refreshed.append(segment_id),
    )

    assert chat.apply_chat_reactions("video-1", ["moment-1", "moment-1"]) == 1
    assert len(captured) == 1
    segment_id, values, revision_number, used_connection = captured[0]
    assert segment_id == "moment-1"
    assert revision_number == 4
    assert used_connection is connection
    assert values["quality_score"] >= 1
    assert "end aligned to sentence" in json.loads(values["quality_signals"])
    assert values["chat_message_count"] == 3
    assert refreshed == ["moment-1"]
    assert "s.id IN (?)" in segment_queries[0][0]
    assert segment_queries[0][1] == ("video-1", "moment-1")


def test_empty_chat_rescore_scope_performs_no_segment_query(monkeypatch):
    monkeypatch.setattr(chat.db, "row", lambda *_args, **_kwargs: {"delay_seconds": 6})
    queries = []

    def fake_rows(query, _parameters=()):
        queries.append(query)
        return [{"seconds": 1.0, "author": "viewer", "message": "hej"}]

    monkeypatch.setattr(chat.db, "rows", fake_rows)

    assert chat.apply_chat_reactions("video-1", []) == 0
    assert not any("FROM segments" in query for query in queries)
