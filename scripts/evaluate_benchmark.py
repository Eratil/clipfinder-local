"""Evaluate a feature-only ClipFinder benchmark without loading ML models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.benchmark import DEFAULT_THRESHOLDS, dump_report, evaluate_feature_benchmark, load_feature_benchmark


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="?", type=Path, default=PROJECT_ROOT / "data" / "benchmarks" / "reviewed-clips.jsonl")
    parser.add_argument("--thresholds", type=Path, help="Optional JSON object with *_min and *_max thresholds.")
    parser.add_argument("--report", type=Path, help="Write the complete machine-readable report here.")
    parser.add_argument("--enforce", action="store_true", help="Return exit code 1 when a sufficiently large benchmark fails a threshold.")
    return parser.parse_args()


def _percent(value) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def main() -> int:
    args = _arguments()
    _metadata, records = load_feature_benchmark(args.dataset.resolve())
    if args.thresholds:
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
    else:
        thresholds = DEFAULT_THRESHOLDS
    report = evaluate_feature_benchmark(records, thresholds=thresholds)
    metrics = report["metrics"]

    print(f"Benchmark: {args.dataset.resolve()}")
    print(f"Status: {report['status']}")
    print(f"Reviewed: {metrics['reviewed']} ({metrics['accepted']} accepted / {metrics['rejected']} rejected), source groups: {metrics['source_groups']}")
    print(f"Average precision: {_percent(metrics['average_precision'])}; pairwise ranking: {_percent(metrics['pairwise_accuracy'])}")
    for limit in (5, 10, 20):
        print(
            f"Top {limit}: precision {_percent(metrics[f'precision_at_{limit}'])}, "
            f"recall {_percent(metrics[f'recall_at_{limit}'])}, "
            f"nDCG {_percent(metrics[f'ndcg_at_{limit}'])}, "
            f"duplicates {_percent(metrics[f'duplicate_exposure_at_{limit}'])}"
        )
    print(f"Reading recall: {_percent(metrics['reading_recall'])}; specificity: {_percent(metrics['reading_specificity'])}")
    print(f"Assigned-tag precision: {_percent(metrics['assigned_tag_precision'])}")
    print(f"Score saturation: >=95 {_percent(metrics['score_95_plus_rate'])}; 99 {_percent(metrics['score_99_rate'])}")
    print(f"Calibration: Brier {metrics['brier_score']:.4f}; ECE {metrics['expected_calibration_error']:.4f}")
    if report["status"] == "INSUFFICIENT_DATA":
        print("This is a seed report, not a quality gate. Collect at least 50 reviews from 3 recordings; use 300-500 reviews from 10+ recordings before tuning production thresholds.")
    for failure in report["failures"]:
        print(f"[threshold] {failure}")
    if args.report:
        dump_report(report, args.report.resolve())
        print(f"JSON report written to: {args.report.resolve()}")
    return 1 if args.enforce and report["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
