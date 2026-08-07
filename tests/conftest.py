from __future__ import annotations

import json
import os
from collections.abc import Callable

import numpy as np
import pytest


os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


@pytest.fixture
def timed_words() -> Callable[[str, float, float], list[dict]]:
    def build(text: str, start: float = 0.0, end: float = 16.0) -> list[dict]:
        tokens = text.split()
        if not tokens:
            return []
        step = (end - start) / len(tokens)
        return [
            {"word": token, "start": start + index * step, "end": start + (index + 1) * step}
            for index, token in enumerate(tokens)
        ]

    return build


@pytest.fixture
def candidate_factory() -> Callable[..., dict]:
    def build(**values) -> dict:
        candidate = {
            "id": values.pop("id", "candidate"),
            "video_id": values.pop("video_id", "video"),
            "start_seconds": values.pop("start_seconds", 0.0),
            "end_seconds": values.pop("end_seconds", 18.0),
            "transcript": values.pop("transcript", "To jest kompletna i zrozumiała wypowiedź, która ma wyraźne zakończenie."),
            "embedding": values.pop("embedding", json.dumps([1.0, 0.0, 0.0, 0.0])),
            "tags": values.pop("tags", "[]"),
            "quality_score": values.pop("quality_score", 72),
            "quality_signals": values.pop("quality_signals", json.dumps(["good clip length", "natural speaking pace"])),
            "short_potential_score": values.pop("short_potential_score", 70),
            "reading_likelihood": values.pop("reading_likelihood", 0.0),
            "audio_event_score": values.pop("audio_event_score", 0),
            "game_reaction_score": values.pop("game_reaction_score", 0),
            "voice_expression_score": values.pop("voice_expression_score", 0),
            "moment_reaction_score": values.pop("moment_reaction_score", 0),
            "moment_reaction_stage": values.pop("moment_reaction_stage", ""),
            "vision_score": values.pop("vision_score", 0),
            "chat_reaction_score": values.pop("chat_reaction_score", 0),
            "chat_joy_score": values.pop("chat_joy_score", 0),
            "chat_message_count": values.pop("chat_message_count", 0),
            "chat_unique_authors": values.pop("chat_unique_authors", 0),
            "logical_sense_score": values.pop("logical_sense_score", 75),
            "context_score": values.pop("context_score", 72),
            "self_contained_score": values.pop("self_contained_score", 78),
            "extended_completeness_score": values.pop("extended_completeness_score", 76),
            "chat_question_match_score": values.pop("chat_question_match_score", 0),
            "review_reason": values.pop("review_reason", ""),
            "rating": values.pop("rating", "unrated"),
            "duplicate_group": values.pop("duplicate_group", ""),
            "word_timestamps": values.pop("word_timestamps", "[]"),
            "created_at": values.pop("created_at", "2026-01-01T00:00:00+00:00"),
        }
        candidate.update(values)
        return candidate

    return build


@pytest.fixture
def empty_discovery_feedback(monkeypatch):
    from app.services import discovery

    empty = np.empty((0, 0), dtype=np.float32)
    monkeypatch.setattr(discovery, "_profile_feedback", lambda _profile: (empty, empty, {}, [], []))
    monkeypatch.setattr(discovery, "active_pattern_set", lambda _profile=None: None)
    return discovery
