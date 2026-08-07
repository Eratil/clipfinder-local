# ClipFinder architecture

This document describes the main technical boundaries of the local Windows
application. It intentionally avoids user recordings, configuration values and
machine-specific paths.

## Process model

ClipFinder is a local application; its normal desktop flow is:

1. `clipfinder_desktop.py` loads the per-user runtime profile and starts an
   Uvicorn server bound to `127.0.0.1:8000`.
2. FastAPI (`app/main.py`) exposes the JSON/media API and serves the static web
   interface from `app/static`.
3. pywebview opens that local interface in a native Windows window.
4. A single durable background worker processes recording-analysis and
   reference-import queues. Heavy native/model operations are serialized to
   avoid competing model instances and GPU memory exhaustion.

The browser UI is a client of the same local API. Closing only a browser tab
does not cancel queued work; closing the desktop/server process stops active
processing, which is recovered on the next start.

## Persistent state

SQLite is the authoritative metadata store. Every connection enables WAL,
foreign-key enforcement and a busy timeout. The most important data model is:

- `videos` stores source metadata and source-removal state;
- `jobs` and `reference_imports` are durable work queues;
- `analysis_runs` records each analysis attempt and identifies the current
  successful run;
- `segments` represents a stable logical moment across reanalysis;
- `segment_revisions` stores immutable machine-produced output for a specific
  run;
- `segment_reviews` and `segment_tag_reviews` bind human decisions to a
  specific revision;
- `preference_feedback`, reference collections and discovery pattern data
  retain learning evidence independently from transient UI state.

Reanalysis creates another run and new revisions. Matching logic attempts to
reconnect the same logical moment to its stable segment identity, while review
records remain revision-aware so a changed result is not silently treated as
the previously reviewed one.

Schema evolution is handled inside `app/database.py`. SQLite
`PRAGMA user_version` is the database-level schema version. Upgrades are
transactional and idempotent; an older executable refuses to write a database
whose schema version is newer than it supports. One-time feature backfills are
tracked separately in `maintenance_tasks`.

## Durable work and recovery

Both long-running queues use a state machine with queued, running and terminal
states. A worker claims work with a time-limited lease and refreshes it with a
heartbeat. This provides:

- atomic ownership of a job;
- bounded retry with backoff for retryable failures;
- cooperative cancellation;
- recovery of expired leases and work abandoned by a stopped process;
- a filesystem lease that prevents two ClipFinder processes sharing one data
  directory from running heavy jobs concurrently.

Progress displayed in the UI is persisted queue state rather than an
in-memory-only task status.

## Analysis pipeline and cache

The analysis pipeline probes media with FFprobe, decodes media with FFmpeg,
transcribes speech, derives audio/visual/context signals, creates embeddings,
scores candidate moments and persists a complete run with provenance. Analysis
modes select how much of this work is performed; they do not change the storage
contract described above.

Expensive deterministic stages use the filesystem-backed pipeline cache under
the managed data directory. A cache key contains a source fingerprint, stage
name and every parameter that can affect the result. Entries use versioned,
canonical JSON plus integrity checks. Invalid, truncated, incompatible or
unsafe entries are treated as cache misses; cache failure must not make a valid
recording fail analysis. Cache files are disposable and are not the source of
truth for reviews or results.

## CPU and NVIDIA runtime

The distributed base application is CPU-capable and must start without NVIDIA
components. Runtime selection is read from the per-user `runtime.json`:

- CPU mode uses CPU transcription and the CPU-compatible base dependencies;
- GPU-ready mode requires a supported CUDA 12 and matching cuDNN 9 runtime and
  a successful CTranslate2 probe;
- if CUDA is configured but unavailable, transcription reports CPU fallback
  instead of preventing the application from starting.

GPU discovery uses the configured CUDA/cuDNN directories; `nvidia-smi` is used
for display information, not as proof that transcription can load CUDA. The
similarity model is lazy-loaded, so its reported state can change from “ready”
to its actual active device after the first search.

The optional GPU add-on configures the runtime but is versioned independently
from normal application updates. The base installer and compact patches do not
embed CUDA or cuDNN.

## Local files

An installed build keeps mutable user data outside the program directory:

```text
%LOCALAPPDATA%\ClipFinder\
|- runtime.json
|- setup-status.txt
`- data\
   |- clipfinder.sqlite3
   |- incoming\
   |- exports\
   |- previews\
   |- review-audio\
   |- reference-downloads\
   |- cache\pipeline\
   |- work\
   `- logs\
```

The source checkout uses `./data` by default. Tests and developers can override
the location with `CLIPFINDER_DATA_DIR`. Model downloads use their provider's
normal cache and are not stored in the application directory.

Large source removal is explicit. It can preserve the database history,
learning evidence and revision-specific review audio while removing the managed
recording. MP4 export, reanalysis and full-recording preview then require the
source to be restored or uploaded again.

## Updates

The app checks a configured public GitHub Releases repository. An update is
accepted only for a newer semantic version and an exact expected asset name.
Downloaded assets must have the SHA-256 digest supplied by GitHub.

For an exact predecessor, a release may provide a compact patch and its target
manifest. The helper verifies the installed previous hashes, stages changed
files, writes durable rollback copies and a transaction journal, verifies all
target hashes and only then commits the replacement. A later helper invocation
automatically rolls back an interrupted transaction or finishes cleanup after
a committed one. If
that exact patch is unavailable or inapplicable, ClipFinder selects the full
setup executable. The helper runs after the desktop process exits and restarts
ClipFinder after a successful update. User data under `%LOCALAPPDATA%` is not
part of either update artifact.

## Diagnostics and privacy

Operational diagnostics use a small rotating log in `data\logs`. The shareable
diagnostic report includes application/build versions, runtime status, platform
information and recent operational events. Paths, URLs and exception content
are redacted; recordings, audio, transcripts, prompts, chat messages and source
URLs are deliberately excluded.
