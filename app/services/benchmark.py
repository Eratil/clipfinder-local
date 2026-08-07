"""Pure helpers for evaluating ClipFinder ranking and review quality.

The production database contains private transcripts, paths and chat content.
This module intentionally accepts only a small feature-only record format so
that benchmark reports can be generated without loading media or ML models.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1

FORBIDDEN_PUBLIC_FIELDS = {
    "archive_audio_path",
    "chat_messages",
    "chat_question_text",
    "context_after",
    "context_before",
    "embedding",
    "original_name",
    "path",
    "review_reason",
    "review_reason_text",
    "source_url",
    "start_seconds",
    "end_seconds",
    "transcript",
    "video_id",
    "segment_id",
    "word_timestamps",
}

REJECTION_CODES = {
    "reading_game_text",
    "incomplete_cut",
    "needs_context",
    "duplicate",
    "incoherent",
    "not_interesting",
    "monotone_delivery",
    "too_long",
    "false_visual_event",
    "greeting_housekeeping",
    "song_copyright",
    "profanity",
    "technical",
    "other",
    "",
}

NUMERIC_FEATURE_RANGES: dict[str, tuple[float, float]] = {
    "quality": (0, 99),
    "short_potential": (-1, 99),
    "logical_sense": (-1, 99),
    "context": (-1, 99),
    "self_contained": (-1, 99),
    "extended_completeness": (-1, 99),
    "reading_likelihood": (0, 1),
    "audio_event": (0, 99),
    "game_reaction": (0, 99),
    "voice_expression": (-99, 99),
    "moment_reaction": (0, 99),
    "vision": (0, 99),
    "chat_reaction": (0, 99),
    "chat_joy": (0, 99),
    "chat_question_match": (0, 99),
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "precision_at_20_min": 0.70,
    "average_precision_min": 0.65,
    "pairwise_accuracy_min": 0.70,
    "reading_recall_min": 0.80,
    "reading_specificity_min": 0.80,
    "duplicate_exposure_at_20_max": 0.10,
    "score_99_rate_max": 0.05,
}


class BenchmarkValidationError(ValueError):
    """Raised when a benchmark file is malformed or leaks private fields."""


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def validate_feature_record(record: dict[str, Any]) -> None:
    """Validate one shareable, feature-only benchmark record.

    Validation is deliberately strict.  Embeddings are treated as semantic
    user data and are forbidden alongside transcripts, paths and chat text.
    """
    if not isinstance(record, dict):
        raise BenchmarkValidationError("Benchmark record must be an object.")
    leaked = sorted(FORBIDDEN_PUBLIC_FIELDS.intersection(_walk_keys(record)))
    if leaked:
        raise BenchmarkValidationError(f"Private fields are not allowed in a feature-only benchmark: {', '.join(leaked)}")
    if int(record.get("schema_version") or 0) != SCHEMA_VERSION:
        raise BenchmarkValidationError(f"Unsupported benchmark schema version: {record.get('schema_version')!r}")
    for key in ("sample_id", "group_id", "decision", "features", "predicted_score"):
        if key not in record:
            raise BenchmarkValidationError(f"Missing required benchmark field: {key}")
    if not isinstance(record["sample_id"], str) or not record["sample_id"].strip():
        raise BenchmarkValidationError("sample_id must be a non-empty string.")
    if not isinstance(record["group_id"], str) or not record["group_id"].strip():
        raise BenchmarkValidationError("group_id must be a non-empty string.")
    if record["decision"] not in {"accepted", "rejected"}:
        raise BenchmarkValidationError("decision must be accepted or rejected.")
    rejection_code = str(record.get("rejection_code") or "")
    if rejection_code not in REJECTION_CODES:
        raise BenchmarkValidationError(f"Unsupported rejection_code: {rejection_code}")
    predicted_score = record["predicted_score"]
    if isinstance(predicted_score, bool) or not isinstance(predicted_score, (int, float)) or not 1 <= float(predicted_score) <= 99:
        raise BenchmarkValidationError("predicted_score must be between 1 and 99.")
    features = record["features"]
    if not isinstance(features, dict):
        raise BenchmarkValidationError("features must be an object.")
    for name, value in features.items():
        if name not in NUMERIC_FEATURE_RANGES:
            raise BenchmarkValidationError(f"Unknown numeric feature: {name}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise BenchmarkValidationError(f"Feature {name} must be a finite number.")
        low, high = NUMERIC_FEATURE_RANGES[name]
        if not low <= float(value) <= high:
            raise BenchmarkValidationError(f"Feature {name} must be between {low} and {high}.")
    tags = record.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise BenchmarkValidationError("tags must be a list of strings.")
    feedback = record.get("tag_feedback", {})
    if not isinstance(feedback, dict) or any(value not in {"correct", "incorrect"} for value in feedback.values()):
        raise BenchmarkValidationError("tag_feedback values must be correct or incorrect.")
    expected_reading = record.get("expected_reading")
    if expected_reading is not None and not isinstance(expected_reading, bool):
        raise BenchmarkValidationError("expected_reading must be true, false or null.")


def load_feature_benchmark(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a JSONL benchmark and reject duplicate IDs or private fields."""
    metadata: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    sample_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise BenchmarkValidationError(f"Invalid JSON on line {line_number}: {exc}") from exc
            if item.get("type") == "metadata":
                if metadata:
                    raise BenchmarkValidationError("A benchmark may contain only one metadata line.")
                metadata = item
                continue
            validate_feature_record(item)
            if item["sample_id"] in sample_ids:
                raise BenchmarkValidationError(f"Duplicate sample_id: {item['sample_id']}")
            sample_ids.add(item["sample_id"])
            records.append(item)
    if not records:
        raise BenchmarkValidationError("Benchmark contains no records.")
    return metadata, records


def _precision_at(ordered: list[dict[str, Any]], limit: int) -> float:
    selected = ordered[: min(limit, len(ordered))]
    return sum(item["decision"] == "accepted" for item in selected) / len(selected) if selected else 0.0


def _recall_at(ordered: list[dict[str, Any]], limit: int) -> float:
    positives = sum(item["decision"] == "accepted" for item in ordered)
    if not positives:
        return 0.0
    return sum(item["decision"] == "accepted" for item in ordered[:limit]) / positives


def _average_precision(ordered: list[dict[str, Any]]) -> float:
    positives = sum(item["decision"] == "accepted" for item in ordered)
    if not positives:
        return 0.0
    found = 0
    total = 0.0
    for index, item in enumerate(ordered, 1):
        if item["decision"] == "accepted":
            found += 1
            total += found / index
    return total / positives


def _ndcg_at(ordered: list[dict[str, Any]], limit: int) -> float:
    gains = [2 if item["decision"] == "accepted" else 0 for item in ordered[:limit]]
    ideal = sorted((2 if item["decision"] == "accepted" else 0 for item in ordered), reverse=True)[:limit]

    def dcg(values: Iterable[int]) -> float:
        return sum((2**gain - 1) / math.log2(index + 2) for index, gain in enumerate(values))

    best = dcg(ideal)
    return dcg(gains) / best if best else 0.0


def _pairwise_accuracy(records: list[dict[str, Any]]) -> float:
    accepted = [float(item["predicted_score"]) for item in records if item["decision"] == "accepted"]
    rejected = [float(item["predicted_score"]) for item in records if item["decision"] == "rejected"]
    pairs = len(accepted) * len(rejected)
    if not pairs:
        return 0.0
    correct = sum(1.0 if left > right else 0.5 if left == right else 0.0 for left in accepted for right in rejected)
    return correct / pairs


def _calibration(records: list[dict[str, Any]], bins: int = 10) -> tuple[float, float]:
    probabilities = [max(0.0, min(1.0, float(item["predicted_score"]) / 99.0)) for item in records]
    labels = [1.0 if item["decision"] == "accepted" else 0.0 for item in records]
    brier = statistics.mean((probability - label) ** 2 for probability, label in zip(probabilities, labels))
    expected_error = 0.0
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        members = [position for position, value in enumerate(probabilities) if low <= value < high or (index == bins - 1 and value == 1.0)]
        if not members:
            continue
        confidence = statistics.mean(probabilities[position] for position in members)
        accuracy = statistics.mean(labels[position] for position in members)
        expected_error += (len(members) / len(records)) * abs(confidence - accuracy)
    return brier, expected_error


def _reading_metrics(records: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    labelled = [item for item in records if item.get("expected_reading") is not None]
    if not labelled:
        return None, None
    positives = [item for item in labelled if item["expected_reading"]]
    negatives = [item for item in labelled if not item["expected_reading"]]
    predicted = lambda item: float(item.get("features", {}).get("reading_likelihood", 0)) >= 0.48
    recall = sum(predicted(item) for item in positives) / len(positives) if positives else None
    specificity = sum(not predicted(item) for item in negatives) / len(negatives) if negatives else None
    return recall, specificity


def _duplicate_exposure(ordered: list[dict[str, Any]], limit: int) -> float:
    selected = ordered[: min(limit, len(ordered))]
    groups = [str(item.get("moment_group_id") or "") for item in selected]
    groups = [group for group in groups if group]
    repeated = sum(count - 1 for count in Counter(groups).values() if count > 1)
    return repeated / len(selected) if selected else 0.0


def evaluate_feature_benchmark(
    records: list[dict[str, Any]],
    *,
    top_ks: tuple[int, ...] = (5, 10, 20),
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Calculate deterministic ranking, calibration, reading and tag metrics."""
    if not records:
        raise BenchmarkValidationError("Cannot evaluate an empty benchmark.")
    for record in records:
        validate_feature_record(record)
    ordered = sorted(records, key=lambda item: (-float(item["predicted_score"]), item["sample_id"]))
    accepted_scores = [float(item["predicted_score"]) for item in records if item["decision"] == "accepted"]
    rejected_scores = [float(item["predicted_score"]) for item in records if item["decision"] == "rejected"]
    brier, expected_calibration_error = _calibration(records)
    reading_recall, reading_specificity = _reading_metrics(records)
    tag_verdicts = [verdict for item in records for verdict in item.get("tag_feedback", {}).values()]
    groups = {item["group_id"] for item in records}

    metrics: dict[str, Any] = {
        "reviewed": len(records),
        "accepted": len(accepted_scores),
        "rejected": len(rejected_scores),
        "source_groups": len(groups),
        "approval_rate": len(accepted_scores) / len(records),
        "average_precision": _average_precision(ordered),
        "pairwise_accuracy": _pairwise_accuracy(records),
        "accepted_score_mean": statistics.mean(accepted_scores) if accepted_scores else None,
        "accepted_score_median": statistics.median(accepted_scores) if accepted_scores else None,
        "rejected_score_mean": statistics.mean(rejected_scores) if rejected_scores else None,
        "rejected_score_median": statistics.median(rejected_scores) if rejected_scores else None,
        "score_95_plus_rate": sum(float(item["predicted_score"]) >= 95 for item in records) / len(records),
        "score_99_rate": sum(float(item["predicted_score"]) == 99 for item in records) / len(records),
        "brier_score": brier,
        "expected_calibration_error": expected_calibration_error,
        "reading_recall": reading_recall,
        "reading_specificity": reading_specificity,
        "assigned_tag_precision": (tag_verdicts.count("correct") / len(tag_verdicts)) if tag_verdicts else None,
    }
    for limit in top_ks:
        metrics[f"precision_at_{limit}"] = _precision_at(ordered, limit)
        metrics[f"recall_at_{limit}"] = _recall_at(ordered, limit)
        metrics[f"ndcg_at_{limit}"] = _ndcg_at(ordered, limit)
        metrics[f"duplicate_exposure_at_{limit}"] = _duplicate_exposure(ordered, limit)
        selected = ordered[: min(limit, len(ordered))]
        metrics[f"reading_exposure_at_{limit}"] = (
            sum(bool(item.get("expected_reading")) for item in selected) / len(selected) if selected else 0.0
        )

    rejected_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        if item["decision"] == "rejected":
            rejected_by_code[str(item.get("rejection_code") or "other")].append(item)
    metrics["false_positive_rate_by_rejection_code"] = {
        code: sum(float(item["predicted_score"]) >= 50 for item in items) / len(items)
        for code, items in sorted(rejected_by_code.items())
    }

    failures: list[str] = []
    selected_thresholds = thresholds or {}
    for name, threshold in selected_thresholds.items():
        if name.endswith("_min"):
            metric_name = name[:-4]
            value = metrics.get(metric_name)
            if value is not None and float(value) < threshold:
                failures.append(f"{metric_name}={value:.4f} is below {threshold:.4f}")
        elif name.endswith("_max"):
            metric_name = name[:-4]
            value = metrics.get(metric_name)
            if value is not None and float(value) > threshold:
                failures.append(f"{metric_name}={value:.4f} is above {threshold:.4f}")
        else:
            raise BenchmarkValidationError(f"Threshold name must end in _min or _max: {name}")

    if len(records) < 50 or len(groups) < 3:
        status = "INSUFFICIENT_DATA"
    elif failures:
        status = "FAIL"
    else:
        status = "PASS"
    return {"schema_version": SCHEMA_VERSION, "status": status, "failures": failures, "metrics": metrics}


def dump_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
