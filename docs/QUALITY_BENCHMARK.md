# ClipFinder quality benchmark

The benchmark has two layers:

1. Synthetic fixtures committed to Git. They contain no user data and test
   business rules, metric calculation and known regressions without loading
   Whisper, FFmpeg, Torch, Sentence Transformers or a recording.
2. A local feature-only export built from reviewed candidates. It is written
   below `data/benchmarks/`, which is ignored by Git.

## Local commands

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-test.txt
python -m pytest
python scripts\export_review_benchmark.py
python scripts\evaluate_benchmark.py data\benchmarks\reviewed-clips.jsonl `
  --report data\benchmarks\latest-report.json
```

For an installed ClipFinder library, export from the per-user data directory:

```powershell
python scripts\export_review_benchmark.py --data-dir "$env:LOCALAPPDATA\ClipFinder\data"
python scripts\evaluate_benchmark.py "$env:LOCALAPPDATA\ClipFinder\data\benchmarks\reviewed-clips.jsonl" --enforce
```

Use `--thresholds path\to\thresholds.json` to test an intentionally reviewed
threshold set and `--report path\to\report.json` to keep the machine-readable
result. `--enforce` returns a non-zero exit code only for a sufficiently large
benchmark with failing thresholds; seed datasets remain informational.

The exporter runs the current discovery ranking and freezes only:

- anonymous HMAC identifiers;
- accepted/rejected decision;
- normalized rejection category;
- numeric candidate features;
- assigned tag IDs and their review verdicts;
- current predicted score.

It does **not** export transcripts, embeddings, media paths, source URLs,
timestamps, chat content, usernames or custom rejection text. Embeddings are
treated as semantic user data rather than anonymous numbers.

## Interpreting the result

`INSUFFICIENT_DATA` is expected until the dataset contains at least 50 reviews
from three recordings. This seed threshold prevents a tiny sample from being
presented as a passing or failing quality gate. Production thresholds should
not be tuned until there are approximately 300-500 reviewed candidates from at
least ten recordings, with both high- and low-scoring candidates reviewed.

The report includes precision/recall/nDCG at 5, 10 and 20, pairwise ranking
accuracy, score saturation, Brier calibration, expected calibration error,
reading recall/specificity, assigned-tag precision, duplicate exposure and
false-positive rates by normalized rejection category.

Tag feedback currently measures precision only: the UI can mark an assigned
tag as correct or incorrect, but cannot yet add a missing tag. Therefore tag
recall must not be claimed until missing-tag annotation is supported.

## Rules for changing the benchmark

- Never commit a real export from `data/benchmarks/`.
- Never update a baseline automatically during a test.
- Prefer relational assertions (A ranks above B, reading stays below a cap)
  over exact scores, because score calibration is expected to evolve.
- Split future train/test data by anonymous recording group, never by segment;
  candidates from the same recording or reanalysis must not leak across both
  sides.
- A failing known-regression test may be marked `xfail(strict=True)` only when
  it names the scheduled repair step. Remove the marker as part of that repair.
