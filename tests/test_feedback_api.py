from __future__ import annotations

from app import main
from app.models import RatingUpdate, TagFeedbackUpdate


def test_rating_endpoint_delegates_to_canonical_feedback_service(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(main, "active_profile", lambda: "horror")

    def fake_set_review(segment_id, rating, reason, profile):
        calls.append((segment_id, rating, reason, profile))
        return {
            "segment_id": segment_id,
            "rating": rating,
            "review_reason": "za długi",
            "profile": profile,
            "reviewed_revision_number": 3,
        }

    monkeypatch.setattr(main, "set_review", fake_set_review)
    result = main.rate_segment(
        "segment-1",
        RatingUpdate(rating="rejected", review_reason="  za długi  "),
    )

    assert calls == [("segment-1", "rejected", "  za długi  ", "horror")]
    assert result["ok"] is True
    assert result["reviewed_revision_number"] == 3


def test_tag_endpoint_delegates_to_revision_bound_feedback_service(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        main,
        "set_tag_verdict",
        lambda segment_id, tag, verdict: calls.append((segment_id, tag, verdict)),
    )
    monkeypatch.setattr(main.db, "row", lambda *_args, **_kwargs: {"id": "segment-1"})
    monkeypatch.setattr(
        main.db,
        "serialize_segment",
        lambda item: {**item, "tag_feedback": {"humor": "correct"}},
    )

    result = main.update_segment_tag_feedback(
        "segment-1",
        TagFeedbackUpdate(tag="humor", verdict="correct"),
    )

    assert calls == [("segment-1", "humor", "correct")]
    assert result["tag_feedback"] == {"humor": "correct"}
