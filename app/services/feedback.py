"""Canonical human feedback and revision-bound training snapshots.

``segment_reviews`` is the source of truth for a user's current review.  The
legacy fields on ``segments`` are maintained as a query-compatible mirror,
while ``preference_feedback`` is a profile-specific snapshot of the exact
machine revision that was reviewed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any

from app import database as db
from app.services.analysis_history import TAGGING_VERSION

try:
    from app.services.tag_taxonomy import canonicalize_tags
except ImportError:  # The taxonomy module is introduced by the adjacent step.
    def canonicalize_tags(tags: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(
            " ".join(str(tag).split()) for tag in tags if " ".join(str(tag).split())
        ))


VALID_RATINGS = frozenset({"accepted", "rejected", "unrated"})
VALID_TAG_VERDICTS = frozenset({"correct", "incorrect", "unmarked"})


def _normalized_choice(value: str, allowed: frozenset[str], field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in allowed:
        raise ValueError(f"Invalid {field}: {value!r}")
    return normalized


def _normalized_profile(profile: str) -> str:
    normalized = " ".join(str(profile or "").split()).casefold()
    if not normalized:
        raise ValueError("Profile cannot be empty.")
    return normalized


def _normalized_reason(reason: str, rating: str) -> str:
    return " ".join(str(reason or "").split()) if rating == "rejected" else ""


def _current_segment_revision(con, segment_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    segment_row = con.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
    if not segment_row:
        raise ValueError("Segment not found.")
    segment = dict(segment_row)
    revision_row = con.execute(
        """SELECT * FROM segment_revisions
           WHERE segment_id=? AND revision_number=? AND is_current=1""",
        (segment_id, int(segment.get("revision_number") or 1)),
    ).fetchone()
    if not revision_row:
        raise RuntimeError("The segment does not have a matching current revision.")
    return segment, dict(revision_row)


def _revision_snapshot(revision: dict[str, Any]) -> tuple[str | None, str]:
    try:
        payload = json.loads(revision.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("The current segment revision has an invalid snapshot.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("The current segment revision has an invalid snapshot.")

    # Imported lazily to keep this low-level service independent from discovery
    # model initialization and to avoid a module import cycle.
    from app.services.discovery import preference_features

    embedding = revision.get("embedding") or payload.get("embedding")
    features = json.dumps(
        preference_features(payload), ensure_ascii=False, separators=(",", ":"),
    )
    return str(embedding) if embedding is not None else None, features


def set_review(segment_id: str, rating: str, reason: str = "", profile: str = "general") -> dict[str, Any]:
    """Set a canonical review and replace this profile's training snapshot.

    The entire dual-write is one SQLite transaction.  An ``unrated`` decision
    removes only the supplied profile's training example; feedback intentionally
    collected for another discovery profile remains independent.
    """
    normalized_rating = _normalized_choice(rating, VALID_RATINGS, "rating")
    normalized_profile = _normalized_profile(profile)
    normalized_reason = _normalized_reason(reason, normalized_rating)
    timestamp = db.now()

    with db.connection() as con:
        segment, revision = _current_segment_revision(con, segment_id)
        revision_id = str(revision["id"])
        revision_number = int(revision["revision_number"])

        con.execute(
            "UPDATE segments SET rating=?, review_reason=? WHERE id=?",
            (normalized_rating, normalized_reason, segment_id),
        )
        con.execute(
            """INSERT INTO segment_reviews
               (segment_id, reviewed_revision_id, rating, review_reason,
                censor_profanity, remove_pauses, archive_audio_path,
                archive_audio_track, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(segment_id) DO UPDATE SET
                 reviewed_revision_id=excluded.reviewed_revision_id,
                 rating=excluded.rating,
                 review_reason=excluded.review_reason,
                 updated_at=excluded.updated_at""",
            (
                segment_id, revision_id, normalized_rating, normalized_reason,
                int(bool(segment.get("censor_profanity"))),
                int(bool(segment.get("remove_pauses"))),
                str(segment.get("archive_audio_path") or ""),
                int(segment.get("archive_audio_track") or 1),
                str(segment.get("created_at") or timestamp), timestamp,
            ),
        )

        if normalized_reason:
            con.execute(
                "INSERT OR IGNORE INTO rejection_reasons (reason, created_at) VALUES (?, ?)",
                (normalized_reason, timestamp),
            )

        if normalized_rating == "unrated":
            con.execute(
                "DELETE FROM preference_feedback WHERE segment_id=? AND profile=?",
                (segment_id, normalized_profile),
            )
        else:
            embedding, features = _revision_snapshot(revision)
            if embedding is None:
                # A previous snapshot must not survive when the newly reviewed
                # revision cannot produce a valid training example.
                con.execute(
                    "DELETE FROM preference_feedback WHERE segment_id=? AND profile=?",
                    (segment_id, normalized_profile),
                )
            else:
                con.execute(
                    """INSERT INTO preference_feedback
                       (id, segment_id, profile, decision, review_reason,
                        embedding, features, reviewed_revision_number,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(segment_id, profile) DO UPDATE SET
                         decision=excluded.decision,
                         review_reason=excluded.review_reason,
                         embedding=excluded.embedding,
                         features=excluded.features,
                         reviewed_revision_number=excluded.reviewed_revision_number,
                         updated_at=excluded.updated_at""",
                    (
                        str(uuid.uuid4()), segment_id, normalized_profile,
                        normalized_rating, normalized_reason, embedding, features,
                        revision_number, timestamp, timestamp,
                    ),
                )

    # Candidate lists can now reuse the decoded/vectorized feedback matrix.
    # Invalidate it only after the transaction committed successfully.
    from app.services.discovery import invalidate_profile_feedback_cache
    invalidate_profile_feedback_cache(normalized_profile)

    return {
        "segment_id": segment_id,
        "rating": normalized_rating,
        "review_reason": normalized_reason,
        "profile": normalized_profile,
        "reviewed_revision_number": revision_number,
    }


def refresh_training_snapshot_if_current(segment_id: str) -> int:
    """Refresh training features only for decisions on the current revision.

    Reanalysis deliberately leaves ``segment_reviews.reviewed_revision_id`` on
    the content that the user actually saw.  If that review is stale, no
    profile snapshot is touched, preventing a decision from revision N from
    being paired with features or an embedding from revision N+1.
    """
    timestamp = db.now()
    with db.connection() as con:
        _segment, revision = _current_segment_revision(con, segment_id)
        review = con.execute(
            "SELECT reviewed_revision_id, rating FROM segment_reviews WHERE segment_id=?",
            (segment_id,),
        ).fetchone()
        if (
            not review
            or str(review["rating"] or "unrated") not in {"accepted", "rejected"}
            or str(review["reviewed_revision_id"] or "") != str(revision["id"])
        ):
            return 0

        embedding, features = _revision_snapshot(revision)
        if embedding is None:
            return 0
        cursor = con.execute(
            """UPDATE preference_feedback
               SET embedding=?, features=?, updated_at=?
               WHERE segment_id=? AND reviewed_revision_number=?""",
            (
                embedding, features, timestamp, segment_id,
                int(revision["revision_number"]),
            ),
        )
        updated = max(0, int(cursor.rowcount))
    if updated:
        from app.services.discovery import invalidate_profile_feedback_cache
        invalidate_profile_feedback_cache()
    return updated


def _canonical_tag(tag: str) -> str:
    normalized = " ".join(str(tag or "").split())
    canonical = canonicalize_tags([normalized]) if normalized else []
    values = [" ".join(str(item).split()) for item in canonical if " ".join(str(item).split())]
    if not values:
        raise ValueError("Tag cannot be empty.")
    return values[0]


def set_tag_verdict(segment_id: str, tag: str, verdict: str) -> dict[str, str]:
    """Set or clear a revision-bound verdict and its legacy mirror."""
    normalized_verdict = _normalized_choice(verdict, VALID_TAG_VERDICTS, "tag verdict")
    canonical_tag = _canonical_tag(tag)
    timestamp = db.now()

    with db.connection() as con:
        segment = con.execute(
            """SELECT s.tags, sr.id AS reviewed_revision_id,
                      COALESCE(ar.tagging_version, ?) AS tagging_version
               FROM segments s
               JOIN segment_revisions sr
                 ON sr.segment_id=s.id AND sr.revision_number=s.revision_number
                AND sr.is_current=1
               LEFT JOIN analysis_runs ar ON ar.id=sr.analysis_run_id
               WHERE s.id=?""",
            (TAGGING_VERSION, segment_id),
        ).fetchone()
        if not segment:
            raise ValueError("Segment not found.")
        try:
            assigned_raw = json.loads(segment["tags"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("The segment has an invalid tag list.") from exc
        if not isinstance(assigned_raw, list):
            raise RuntimeError("The segment has an invalid tag list.")
        assigned = {
            item.casefold(): item
            for item in canonicalize_tags([str(item) for item in assigned_raw])
        }
        if canonical_tag.casefold() not in assigned:
            raise ValueError("This tag is no longer assigned to the clip.")
        canonical_tag = assigned[canonical_tag.casefold()]

        reviewed_revision_id = str(segment["reviewed_revision_id"])
        legacy_rows = con.execute(
            "SELECT tag FROM tag_feedback WHERE segment_id=?",
            (segment_id,),
        ).fetchall()
        equivalent_legacy_tags = [
            str(item["tag"]) for item in legacy_rows
            if canonical_tag.casefold() in {
                value.casefold() for value in canonicalize_tags([item["tag"]])
            }
        ]

        if normalized_verdict == "unmarked":
            con.execute(
                """DELETE FROM segment_tag_reviews
                   WHERE segment_id=? AND reviewed_revision_id=? AND canonical_tag=?""",
                (segment_id, reviewed_revision_id, canonical_tag),
            )
        else:
            con.execute(
                """INSERT INTO segment_tag_reviews
                   (id, segment_id, reviewed_revision_id, canonical_tag, verdict,
                    tagging_version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(segment_id, reviewed_revision_id, canonical_tag)
                   DO UPDATE SET verdict=excluded.verdict,
                                 tagging_version=excluded.tagging_version,
                                 updated_at=excluded.updated_at""",
                (
                    str(uuid.uuid4()), segment_id, reviewed_revision_id,
                    canonical_tag, normalized_verdict,
                    str(segment["tagging_version"] or TAGGING_VERSION),
                    timestamp, timestamp,
                ),
            )

        # ``tag_feedback`` remains a compatibility projection for older code.
        # Remove aliases of the same canonical tag before writing the mirror.
        for legacy_tag in equivalent_legacy_tags:
            con.execute(
                "DELETE FROM tag_feedback WHERE segment_id=? AND tag=?",
                (segment_id, legacy_tag),
            )
        if normalized_verdict != "unmarked":
            con.execute(
                """INSERT INTO tag_feedback (segment_id, tag, verdict, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(segment_id, tag) DO UPDATE SET
                     verdict=excluded.verdict, updated_at=excluded.updated_at""",
                (segment_id, canonical_tag, normalized_verdict, timestamp),
            )

    return {
        "segment_id": segment_id,
        "tag": canonical_tag,
        "verdict": normalized_verdict,
        "reviewed_revision_id": reviewed_revision_id,
    }
