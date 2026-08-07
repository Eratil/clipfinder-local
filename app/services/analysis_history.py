from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


# Persist these values on every analysis run.  They deliberately describe the
# result contract instead of the application release, so a UI-only release does
# not make otherwise identical analyses look incompatible.
PIPELINE_VERSION = "3"
SCORING_VERSION = "3"
TAGGING_VERSION = "3"
MOMENT_MATCHER_VERSION = "2"


# Public thresholds make matching behaviour auditable and allow migrations to
# record exactly which matcher policy assigned a stable moment identifier.
MATCH_SCORE_HIGH = 0.76
MATCH_SCORE_MEDIUM = 0.64
MATCH_MIN_TEXT = 0.34
MATCH_STRONG_TEXT = 0.78
MATCH_MIN_IOU = 0.24
MATCH_MIN_BIDIRECTIONAL_COVERAGE = 0.42
MATCH_MAX_MIDPOINT_SECONDS = 8.0


@dataclass(frozen=True, slots=True)
class MomentMatchSignals:
    overlap_seconds: float
    iou: float
    previous_coverage: float
    new_coverage: float
    duration_ratio: float
    midpoint_distance: float
    midpoint_proximity: float
    sequence_similarity: float
    token_jaccard: float

    @property
    def text_similarity(self) -> float:
        return self.sequence_similarity * 0.65 + self.token_jaccard * 0.35

    @property
    def bidirectional_coverage(self) -> float:
        return min(self.previous_coverage, self.new_coverage)


@dataclass(frozen=True, slots=True)
class MomentMatch:
    previous_id: str
    new_id: str
    score: float
    confidence: str
    reason: str
    signals: MomentMatchSignals


@dataclass(frozen=True, slots=True)
class MomentMatchResult:
    matches: tuple[MomentMatch, ...]
    unmatched_previous_ids: tuple[str, ...]
    unmatched_new_ids: tuple[str, ...]

    def by_new_id(self) -> dict[str, MomentMatch]:
        return {match.new_id: match for match in self.matches}


def normalize_match_text(value: str | None) -> str:
    """Return a language-neutral comparison form without retaining metadata."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(
        "".join(character if character.isalnum() else " " for character in without_marks.casefold()).split()
    )


def token_jaccard(left: str | None, right: str | None) -> float:
    left_tokens = set(normalize_match_text(left).split())
    right_tokens = set(normalize_match_text(right).split())
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _first(record: Mapping[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return default


def _identity(record: Mapping[str, Any]) -> str:
    value = _first(record, ("id", "segment_id", "revision_id", "candidate_id"))
    if value is None:
        raise ValueError("A moment candidate must contain id or segment_id")
    return str(value)


def _interval(record: Mapping[str, Any]) -> tuple[float, float]:
    start = float(_first(record, ("start_seconds", "start"), 0.0))
    end = float(_first(record, ("end_seconds", "end"), start))
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("Moment boundaries must be finite numbers")
    if end < start:
        start, end = end, start
    return start, end


def _text(record: Mapping[str, Any]) -> str:
    return str(_first(record, ("transcript", "text", "caption_text"), "") or "")


def match_signals(previous: Mapping[str, Any], new: Mapping[str, Any]) -> MomentMatchSignals:
    previous_start, previous_end = _interval(previous)
    new_start, new_end = _interval(new)
    previous_duration = max(0.25, previous_end - previous_start)
    new_duration = max(0.25, new_end - new_start)
    overlap = max(0.0, min(previous_end, new_end) - max(previous_start, new_start))
    union = max(previous_end, new_end) - min(previous_start, new_start)
    iou = overlap / union if union > 0 else 1.0
    previous_coverage = min(1.0, overlap / previous_duration)
    new_coverage = min(1.0, overlap / new_duration)
    duration_ratio = min(previous_duration, new_duration) / max(previous_duration, new_duration)
    previous_midpoint = (previous_start + previous_end) / 2
    new_midpoint = (new_start + new_end) / 2
    midpoint_distance = abs(previous_midpoint - new_midpoint)
    # A relative window prevents long candidates from being penalised as if
    # they were short utterances, while the eight-second cap stops neighbours
    # from inheriting each other's human reviews.
    midpoint_window = min(MATCH_MAX_MIDPOINT_SECONDS, max(3.0, max(previous_duration, new_duration) * 0.45))
    midpoint_proximity = max(0.0, 1.0 - midpoint_distance / midpoint_window)
    previous_text = normalize_match_text(_text(previous))
    new_text = normalize_match_text(_text(new))
    if not previous_text and not new_text:
        sequence_similarity = 1.0
    elif not previous_text or not new_text:
        sequence_similarity = 0.0
    else:
        sequence_similarity = SequenceMatcher(None, previous_text, new_text, autojunk=False).ratio()
    return MomentMatchSignals(
        overlap_seconds=overlap,
        iou=iou,
        previous_coverage=previous_coverage,
        new_coverage=new_coverage,
        duration_ratio=duration_ratio,
        midpoint_distance=midpoint_distance,
        midpoint_proximity=midpoint_proximity,
        sequence_similarity=sequence_similarity,
        token_jaccard=token_jaccard(previous_text, new_text),
    )


def match_score(signals: MomentMatchSignals) -> float:
    """Score an old/new revision pair using symmetric time and text evidence."""
    value = (
        signals.iou * 0.20
        + signals.previous_coverage * 0.11
        + signals.new_coverage * 0.11
        + signals.duration_ratio * 0.10
        + signals.midpoint_proximity * 0.10
        + signals.sequence_similarity * 0.25
        + signals.token_jaccard * 0.13
    )
    return round(max(0.0, min(1.0, value)), 6)


def classify_match(signals: MomentMatchSignals, score: float | None = None) -> tuple[str | None, str]:
    """Return accepted confidence and a concise, persistable explanation."""
    score = match_score(signals) if score is None else score
    text = signals.text_similarity
    coverage = signals.bidirectional_coverage
    temporal = signals.iou >= MATCH_MIN_IOU or coverage >= MATCH_MIN_BIDIRECTIONAL_COVERAGE
    close_midpoint = signals.midpoint_distance <= MATCH_MAX_MIDPOINT_SECONDS

    if signals.overlap_seconds <= 0 and not (text >= 0.94 and close_midpoint):
        return None, "rejected: no temporal overlap"
    if not close_midpoint:
        return None, "rejected: midpoint is too far away"
    if text < MATCH_MIN_TEXT and not (
        coverage >= 0.88 and signals.duration_ratio >= 0.72 and signals.midpoint_distance <= 2.0
    ):
        return None, "rejected: transcript differs"
    if not temporal:
        return None, "rejected: insufficient temporal overlap"

    if score >= MATCH_SCORE_HIGH and (
        text >= MATCH_MIN_TEXT or (coverage >= 0.92 and signals.duration_ratio >= 0.82)
    ):
        if text >= MATCH_STRONG_TEXT:
            return "high", "high: strong temporal and transcript agreement"
        return "high", "high: near-identical temporal boundaries"
    if score >= MATCH_SCORE_MEDIUM and text >= MATCH_MIN_TEXT:
        return "medium", "medium: boundary drift with supporting transcript"
    if text >= 0.90 and coverage >= 0.30:
        return "medium", "medium: strong transcript with partial overlap"
    return None, "rejected: combined confidence below threshold"


def _candidate_match(previous: Mapping[str, Any], new: Mapping[str, Any]) -> MomentMatch | None:
    signals = match_signals(previous, new)
    score = match_score(signals)
    confidence, reason = classify_match(signals, score)
    if confidence is None:
        return None
    return MomentMatch(
        previous_id=_identity(previous),
        new_id=_identity(new),
        score=score,
        confidence=confidence,
        reason=reason,
        signals=signals,
    )


def match_moments(
    previous_segments: Iterable[Mapping[str, Any]],
    new_segments: Iterable[Mapping[str, Any]],
) -> MomentMatchResult:
    """Deterministically assign old revisions to new revisions one-to-one.

    Every existing segment participates, not only reviewed clips.  This is
    important because a moment may acquire human data after another reanalysis.
    Candidates are ranked by evidence, confidence and stable identifiers, so
    input/query ordering cannot alter the assignment.
    """
    previous = sorted((dict(item) for item in previous_segments), key=_identity)
    new = sorted((dict(item) for item in new_segments), key=_identity)
    candidates: list[MomentMatch] = []
    for old in previous:
        old_start, old_end = _interval(old)
        old_midpoint = (old_start + old_end) / 2
        for current in new:
            new_start, new_end = _interval(current)
            new_midpoint = (new_start + new_end) / 2
            # Cheap temporal pruning avoids quadratic string comparisons on
            # multi-hour streams without changing any accepted match.
            if abs(old_midpoint - new_midpoint) > MATCH_MAX_MIDPOINT_SECONDS:
                continue
            candidate = _candidate_match(old, current)
            if candidate is not None:
                candidates.append(candidate)

    confidence_rank = {"high": 2, "medium": 1}
    candidates.sort(
        key=lambda match: (
            -confidence_rank[match.confidence],
            -match.score,
            -match.signals.text_similarity,
            -match.signals.bidirectional_coverage,
            match.previous_id,
            match.new_id,
        )
    )
    claimed_previous: set[str] = set()
    claimed_new: set[str] = set()
    selected: list[MomentMatch] = []
    for candidate in candidates:
        if candidate.previous_id in claimed_previous or candidate.new_id in claimed_new:
            continue
        selected.append(candidate)
        claimed_previous.add(candidate.previous_id)
        claimed_new.add(candidate.new_id)

    selected.sort(key=lambda match: (match.new_id, match.previous_id))
    previous_ids = tuple(_identity(item) for item in previous)
    new_ids = tuple(_identity(item) for item in new)
    return MomentMatchResult(
        matches=tuple(selected),
        unmatched_previous_ids=tuple(item_id for item_id in previous_ids if item_id not in claimed_previous),
        unmatched_new_ids=tuple(item_id for item_id in new_ids if item_id not in claimed_new),
    )


def has_human_data(
    segment: Mapping[str, Any],
    *,
    tag_feedback: Iterable[Mapping[str, Any]] | None = None,
    collection_examples: Iterable[Mapping[str, Any]] | None = None,
    preference_feedback: Iterable[Mapping[str, Any]] | None = None,
) -> bool:
    """Tell whether a moment must be retained independently of machine output."""
    rating = str(segment.get("rating") or "unrated").strip().casefold()
    if rating not in {"", "unrated", "not reviewed", "none", "null"}:
        return True
    if str(segment.get("review_reason") or "").strip():
        return True
    if any(bool(segment.get(key)) for key in (
        "censor_profanity",
        "remove_pauses",
        "archive_audio_path",
        "caption_edited",
        "range_edited",
        "human_caption_text",
        "reviewed_at",
    )):
        return True
    if segment.get("review_id") or segment.get("moment_review_id"):
        return True
    return any(
        any(values or ())
        for values in (tag_feedback, collection_examples, preference_feedback)
    )
