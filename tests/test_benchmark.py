from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.benchmark import (
    BenchmarkValidationError,
    evaluate_feature_benchmark,
    load_feature_benchmark,
    validate_feature_record,
)


FIXTURE = Path(__file__).parent / "fixtures" / "benchmark-smoke.jsonl"


def test_smoke_benchmark_is_deterministic_and_reports_seed_status():
    _metadata, records = load_feature_benchmark(FIXTURE)
    first = evaluate_feature_benchmark(records)
    second = evaluate_feature_benchmark(records)
    assert first == second
    assert first["status"] == "INSUFFICIENT_DATA"
    assert first["metrics"]["reviewed"] == 8
    assert first["metrics"]["precision_at_5"] >= 0.6
    assert first["metrics"]["reading_recall"] == 1.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("transcript", "private words"),
        ("embedding", [0.1, 0.2]),
        ("path", "C:/private/video.mp4"),
        ("chat_messages", [{"message": "private"}]),
        ("review_reason_text", "private custom reason"),
    ],
)
def test_feature_benchmark_rejects_private_fields(field, value):
    _metadata, records = load_feature_benchmark(FIXTURE)
    leaked = dict(records[0])
    leaked[field] = value
    with pytest.raises(BenchmarkValidationError, match="Private fields"):
        validate_feature_record(leaked)


def test_duplicate_sample_ids_are_rejected(tmp_path):
    _metadata, records = load_feature_benchmark(FIXTURE)
    path = tmp_path / "duplicates.jsonl"
    path.write_text("\n".join(json.dumps(records[0]) for _ in range(2)), encoding="utf-8")
    with pytest.raises(BenchmarkValidationError, match="Duplicate sample_id"):
        load_feature_benchmark(path)


def test_threshold_failure_is_not_a_gate_until_dataset_is_large_enough():
    _metadata, records = load_feature_benchmark(FIXTURE)
    report = evaluate_feature_benchmark(records, thresholds={"precision_at_5_min": 1.0})
    assert report["status"] == "INSUFFICIENT_DATA"
    assert report["failures"]

