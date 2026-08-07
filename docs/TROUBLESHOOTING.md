# ClipFinder troubleshooting

Start with the status line at the top of ClipFinder, then use **Options ->
Download diagnostic report**. The report is designed to be shareable: it does
not include recordings, transcripts, prompts, chat content or source URLs.

For an installed build, the two primary local diagnostics are:

- `%LOCALAPPDATA%\ClipFinder\data\logs\clipfinder.log`
- `%LOCALAPPDATA%\ClipFinder\setup-status.txt`

Do not attach the database, recordings or the whole data directory to a public
bug report.

## The desktop window does not start

1. Check whether another ClipFinder instance is already using
   `127.0.0.1:8000`; close it and retry.
2. Read `clipfinder.log` and `setup-status.txt`.
3. Confirm that Microsoft Edge WebView2 Runtime and Microsoft Visual C++
   Redistributable (x64) are installed. Repair or reinstall ClipFinder if they
   are missing.
4. For a source checkout, run `scripts\doctor.py` from its Python 3.11 virtual
   environment and confirm that FFmpeg and FFprobe are available.

The installed app should not require Python from `PATH`; it ships its packaged
runtime. A source checkout requires 64-bit Python 3.11.

## `LOCAL / CPU MODE`, `CPU FALLBACK` or `GPU READY`

- **CPU MODE** means CUDA transcription is disabled in configuration.
- **CPU FALLBACK** means CUDA was requested but the authoritative native probe
  could not load the configured runtime. Analysis continues on CPU.
- **GPU READY** means the CUDA transcription probe passed. It does not mean the
  similarity model has already been loaded.

If an NVIDIA computer unexpectedly falls back to CPU:

1. Download the diagnostic report and look for the first CUDA/CTranslate2 DLL
   error.
2. Verify that `runtime.json` points to existing CUDA and cuDNN `bin`
   directories.
3. Use a supported CUDA 12 runtime and cuDNN 9 built for the same CUDA minor
   version. Installing CUDA 13 alone does not provide `cublas64_12.dll`.
4. Confirm that the NVIDIA driver is working, then restart ClipFinder. Runtime
   hardware probes are cached briefly, so an already-open process may still
   show its earlier result.
5. Re-run the GPU add-on only if runtime repair is needed; ordinary ClipFinder
   updates do not require reinstalling it.

Do not copy arbitrary DLLs between unrelated CUDA versions. A detected GPU name
from `nvidia-smi` is useful information, but successful CTranslate2 loading is
the actual GPU-readiness test.

## Similarity search says CPU or changes after use

The similarity model is loaded lazily on the first semantic search. Before that
operation the UI may show a readiness state; afterwards it reports the device
actually selected. The CPU-only base package can use GPU transcription while
still using CPU similarity search. This is expected and does not invalidate an
analysis.

## Upload, URL import or analysis fails

1. Keep the desktop/server process open until processing finishes. The web page
   itself may be closed after upload completes.
2. Confirm sufficient free space in the managed data directory. Long recordings
   need room for the source plus temporary audio and exported clips.
3. Confirm FFmpeg and FFprobe are available. For URL imports, also confirm
   internet access and that the source is public and supported.
4. If a selected audio track does not exist in that recording, switch analysis
   to a valid track or single-track mode.
5. Use **Run analysis again** only after correcting the reported deterministic
   error. A failed reanalysis does not replace the last completed run.

An HTTP 500 message in the UI is only the API symptom. The redacted exception
and pipeline stage in `clipfinder.log` are the useful diagnostic evidence.

## Work appears stuck after a restart

Recording analysis and reference import are durable queues. On startup,
ClipFinder returns abandoned or expired leases to the queue and retries eligible
work. Leave one instance open and use **Refresh now** before submitting a
duplicate job. The worker intentionally processes heavy model work one item at
a time.

If progress never changes:

1. confirm only one ClipFinder process uses the data directory;
2. check the log for repeated lease, database or native-runtime errors;
3. cancel the queued item from the UI if it is no longer wanted;
4. restart once after fixing the underlying error—the queue does not need to be
   deleted manually.

## Startup is slow

The first run can download the pinned transcription/embedding models. Startup
may also perform one-time, repairable maintenance for data created by older
versions. Later analyses can still take time to lazy-load a model.

Check `clipfinder.log` for `Startup maintenance started/completed` entries. Do
not delete `maintenance_tasks` or edit the SQLite database to speed this up.
Cache files may be removed when troubleshooting disk usage, but doing so only
forces expensive stages to be calculated again.

## Cache-related symptoms

The pipeline cache is an optimization, not the result database. ClipFinder
validates entry schema, parameters, source fingerprint and integrity; a broken
entry becomes a cache miss. Cache read/write failures are logged and the valid
analysis should continue without cache reuse.

Use the application's storage controls where available. If manual cleanup is
unavoidable, close ClipFinder first and remove only the managed
`data\cache\pipeline` directory. Never remove `clipfinder.sqlite3` as a cache
repair.

## Database schema or downgrade error

If ClipFinder reports that the data library was opened by a newer application,
install that newer ClipFinder version. Do not reset `PRAGMA user_version`, copy
an old database over the current one or edit revision tables manually. Those
actions can detach human reviews from their analyzed revision.

Before experimenting with a development build, back up the entire data
directory while ClipFinder is closed.

## Update is not offered or cannot be applied

1. The configured GitHub repository and Release must be public for the built-in
   unauthenticated updater.
2. The Release needs `ClipFinder-Setup-x.y.z.exe`. A compact update additionally
   needs the exact `ClipFinder-patch-old-to-new.zip` and matching target
   manifest.
3. The asset must expose a valid GitHub SHA-256 digest. ClipFinder refuses an
   unverifiable download.
4. A compact patch works only from the exact predecessor and verifies existing
   application hashes. Local modifications cause a safe failure; use the full
   installer instead.
5. If Windows or power interrupted a compact update, run ClipFinder/update
   again. The helper reads its durable transaction journal and restores the
   previous complete version before retrying; do not delete the update work
   directory while recovery is pending.
6. Close applications that lock ClipFinder files. If normal replacement lacks
   permission, approve the Windows elevation prompt or run the full installer.

The updater log is written beside the normal application diagnostics. User data
under `%LOCALAPPDATA%\ClipFinder\data` is not replaced by patch or full update.

## Source video was removed

Removing a source is intentionally different from deleting analysis history.
ClipFinder keeps analyzed metadata, reviews, tags, preference evidence and small
review-audio snapshots. Operations that need original video bytes—reanalysis,
full-recording preview and MP4 export—are unavailable afterwards. Restore or
upload the source again if one of those operations is required.

## What to include in a bug report

Include:

- ClipFinder version and whether it is installed or run from source;
- the downloaded diagnostic report;
- the exact action that failed and the UI error text;
- whether the same input works in CPU mode;
- for media errors, container type, duration and number of audio tracks (not
  the media itself unless you explicitly choose to share it privately).

Exclude passwords, account tokens, private URLs, chat logs, recordings and the
SQLite database from public reports.
