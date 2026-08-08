import base64
import hashlib
import json
import os
import re
import shutil
import statistics
import threading
import time
import uuid
from collections import Counter, defaultdict
from copy import deepcopy
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

try:
    # This is an application entry point, so the global injection is safe and
    # lets yt-dlp use certificates trusted by Windows (including enterprise or
    # antivirus HTTPS inspection certificates).
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app import database as db
from app.config import settings
from app.models import (
    CaptionDefaultsUpdate,
    CaptionFavoriteCreate,
    AnalysisAudioDefaultsUpdate,
    ChatDelayUpdate,
    DiscoveryDefaultsUpdate,
    DiscoveryPatternSetCreate,
    CollectionCreate,
    DescriptionSearch,
    ExampleCreate,
    ExportDefaultsUpdate,
    ComposerCaptionRefresh,
    LayoutPresetCreate,
    ExportRequest,
    RatingUpdate,
    RejectionReasonCreate,
    ReferenceFolderImport,
    ReferenceUrlImport,
    RemotePreviewCreate,
    RemotePreviewSave,
    RemoteVideoCreate,
    SavedPromptCreate,
    SegmentTimingUpdate,
    SegmentCensorUpdate,
    SegmentPauseTrimUpdate,
    SegmentTranscriptUpdate,
    TagFeedbackUpdate,
    PublicationFeedbackUpdate,
    SimilaritySearch,
)
from app.services.embeddings import cosine, embed_texts
from app.services.chat import apply_chat_reactions, chat_summary, import_chat, update_chat_delay
from app.services.discovery import (
    active_profile,
    assign_duplicate_groups,
    best_of_stream,
    filter_profanity,
    is_disallowed_reading,
    profile_payload,
    preference_features,
    score_candidates,
    suppress_duplicate_groups,
)
from app.services.media import MediaError, audio_track_count, export_audio_preview, export_clip, pause_trim_ranges, remap_words_for_kept_ranges, run as run_media_command, write_caption_ass
from app.services.pipeline import analyse, import_reference_files, import_reference_folder, transcribe, transcribe_clip_range
from app.services.analysis_store import (
    fail_running_analysis,
    record_manual_revision_with_updates,
    set_latest_run_elapsed,
    update_current_segment_and_revision,
)
from app.services.feature_graph import recompute_segment_features
from app.services.feedback import set_review, set_tag_verdict
from app.services.background_worker import (
    DurableBackgroundWorker,
    PermanentWorkError,
    QueueAdapter,
    WorkCancelled,
    WorkPaused,
)
from app.services import job_queue, reference_queue
from app.services.pipeline_cache import PipelineCache
from app.services.workspace_cleanup import cleanup_workspace
from app.services.tagging import (
    CHAT_QUESTION_ANSWER_TAG,
    CHAT_QUESTION_TAG,
    GAME_REACTION_TAG,
    build_reference_prompt,
    detailed_lexical_tags,
    infer_tags,
)
from app.services.tag_taxonomy import GAME_REACTION_MIN_SCORE, canonical_tag, canonicalize_tags
from app.services.updater import automatic_updates_available, install_downloaded_update, job_status as update_download_status, start_download as start_update_download
from app.services.updates import update_status
from app.services.runtime_status import runtime_status
from app.services import diagnostics
from app.version import __version__


def backfill_segment_quality() -> None:
    """Add lightweight quality/read-aloud data to clips analyzed before this feature."""
    for item in db.rows(
        "SELECT id FROM segments WHERE lifecycle_state='current' AND quality_score=0"
    ):
        _recompute_persisted_segment(item["id"])


def backfill_reading_filter() -> None:
    """Apply the stricter task/note reading rule to existing recordings once."""
    items = db.rows(
        """SELECT id, quality_signals, reading_likelihood,
                  visual_reading_likelihood
           FROM segments WHERE lifecycle_state='current' AND quality_signals NOT LIKE ?""",
        ('%"reading heuristics v3"%',),
    )
    for item in items:
        # Older rows stored only the final probability. Preserve it as legacy
        # visual evidence so a text-only migration can never erase a prior
        # visual or Extended reading detection, regardless of analysis mode.
        preserved = max(
            float(item.get("reading_likelihood") or 0.0),
            float(item.get("visual_reading_likelihood") or 0.0),
        )
        updates = _recompute_persisted_segment(
            item["id"],
            overrides={"visual_reading_likelihood": preserved},
        )
        signals = list(dict.fromkeys(list(updates.get("quality_signals") or []) + ["reading heuristics v3"]))
        current = db.row("SELECT revision_number FROM segments WHERE id=?", (item["id"],)) or {}
        update_current_segment_and_revision(
            item["id"],
            {"quality_signals": json.dumps(signals, ensure_ascii=False)},
            expected_revision_number=int(current.get("revision_number") or 1),
        )


def backfill_context_signals() -> None:
    """Make the new context and game-reaction signals available for old clips."""
    items = db.rows(
        "SELECT id, transcript, tags, game_reaction_score FROM segments "
        "WHERE lifecycle_state='current' AND (logical_sense_score < 0 OR (game_reaction_score >= 7 AND tags NOT LIKE ?)) ",
        (f'%"{GAME_REACTION_TAG}"%',),
    )
    for item in items:
        _recompute_persisted_segment(item["id"])


def backfill_segment_context() -> None:
    """Build lightweight context from adjacent existing candidates once."""
    updates = []
    for video in db.rows("SELECT DISTINCT video_id FROM segments WHERE lifecycle_state='current' AND (context_score < 0 OR self_contained_score < 0)"):
        segments = db.rows(
            "SELECT id, start_seconds, end_seconds, transcript, context_score, self_contained_score FROM segments WHERE video_id=? AND lifecycle_state='current' ORDER BY start_seconds",
            (video["video_id"],),
        )
        for segment in segments:
            if int(segment.get("context_score") or -1) >= 0 and int(segment.get("self_contained_score") or -1) >= 0:
                continue
            start, end = float(segment["start_seconds"]), float(segment["end_seconds"])
            before = " ".join(item["transcript"] for item in segments if start - 12 <= float(item["end_seconds"]) <= start and item["id"] != segment["id"])[-700:]
            after = " ".join(item["transcript"] for item in segments if end <= float(item["start_seconds"]) <= end + 12 and item["id"] != segment["id"])[:700]
            updates.append((segment["id"], before, after))
    for segment_id, before, after in updates:
        _recompute_persisted_segment(
            segment_id,
            {"context_before", "context_after"},
            {"context_before": before, "context_after": after},
        )


def backfill_moment_reactions() -> None:
    """Seed the game-to-voice stage for clips analysed before the combined score."""
    items = db.rows("SELECT id, game_reaction_score FROM segments WHERE lifecycle_state='current' AND moment_reaction_score=0 AND game_reaction_score>=7")
    for item in items:
        _recompute_persisted_segment(item["id"], {"game_reaction_score"})


def backfill_stricter_game_reaction_tags() -> None:
    """Remove legacy reaction labels which do not meet the causal threshold."""
    items = db.rows(
        """SELECT id FROM segments
           WHERE lifecycle_state='current'
             AND game_reaction_score < ?
             AND tags LIKE ?""",
        (GAME_REACTION_MIN_SCORE, f'%"{GAME_REACTION_TAG}"%'),
    )
    for item in items:
        _recompute_persisted_segment(item["id"], {"game_reaction_score"})


def backfill_detailed_tags() -> None:
    """Add precise text/context labels to existing clips without retranscribing."""
    items = db.rows(
        """SELECT id, transcript, tags, logical_sense_score, reading_likelihood,
                  game_reaction_score, voice_expression_score, moment_reaction_score, moment_reaction_stage, chat_reaction_score, context_score, self_contained_score,
                  chat_joy_score, vision_score
           FROM segments WHERE lifecycle_state='current'"""
    )
    for item in items:
        # Question labels are now evidence-based: only chat.py may restore
        # them after matching a viewer question to a spoken answer.
        previous = [
            tag for tag in json.loads(item.get("tags") or "[]")
            if tag not in {CHAT_QUESTION_TAG, "forma: pytanie", CHAT_QUESTION_ANSWER_TAG}
        ]
        tags = list(dict.fromkeys(previous + detailed_lexical_tags(item["transcript"])))
        _recompute_persisted_segment(item["id"], {"tags"}, {"tags": tags})


def backfill_short_potential() -> None:
    """Give older candidates the separate short-format suitability score."""
    for item in db.rows(
        "SELECT id FROM segments WHERE lifecycle_state='current' AND short_potential_score < 0"
    ):
        _recompute_persisted_segment(item["id"], {"quality_score"})


def remove_legacy_game_audio_bonus() -> None:
    """Do not keep old scores where a loud game sound was treated as a reaction."""
    items = db.rows(
        """SELECT id, transcript, tags, word_timestamps, start_seconds, end_seconds, quality_signals
           FROM segments
           WHERE lifecycle_state='current' AND audio_event_score > 0 AND game_reaction_score=0 AND voice_expression_score=0"""
    )
    legacy_labels = {"all-sounds event", "game-audio event"}
    for item in items:
        previous_signals = set(json.loads(item.get("quality_signals") or "[]"))
        if previous_signals.intersection(legacy_labels):
            _recompute_persisted_segment(
                item["id"],
                {"audio_event_score"},
                {"audio_event_score": 0},
            )


def backfill_duplicate_groups() -> None:
    """Group older candidates once so the compact review list works immediately."""
    for video in db.rows("SELECT DISTINCT video_id FROM segments WHERE lifecycle_state='current' AND embedding IS NOT NULL"):
        items = db.rows("SELECT id, start_seconds, end_seconds, embedding FROM segments WHERE video_id=? AND lifecycle_state='current' AND embedding IS NOT NULL", (video["video_id"],))
        records = [{"id": item["id"], "start": item["start_seconds"], "end": item["end_seconds"], "vector": json.loads(item["embedding"]), "duplicate_group": ""} for item in items]
        assign_duplicate_groups(records)
        revisions = {
            item["id"]: int((db.row("SELECT revision_number FROM segments WHERE id=?", (item["id"],)) or {}).get("revision_number") or 1)
            for item in items
        }
        with db.connection() as con:
            for record in records:
                update_current_segment_and_revision(
                    record["id"],
                    {"duplicate_group": record["duplicate_group"]},
                    con=con,
                    expected_revision_number=revisions[record["id"]],
                )


def backfill_preference_feedback() -> None:
    """Seed the general profile with prior review decisions from older versions."""
    items = db.rows(
        """SELECT s.*, r.rating, r.review_reason
           FROM segments s
           JOIN segment_reviews r ON r.segment_id=s.id
           JOIN segment_revisions sr
             ON sr.id=r.reviewed_revision_id AND sr.segment_id=s.id
            AND sr.revision_number=s.revision_number
           WHERE r.rating IN ('accepted', 'rejected') AND s.embedding IS NOT NULL"""
    )
    if not items:
        return
    timestamp = db.now()
    with db.connection() as con:
        for item in items:
            con.execute(
                """INSERT OR IGNORE INTO preference_feedback
                   (id, segment_id, profile, decision, review_reason, embedding,
                    features, reviewed_revision_number, created_at, updated_at)
                   VALUES (?, ?, 'general', ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), item["id"], item["rating"], item.get("review_reason") or "", item["embedding"],
                 json.dumps(preference_features(item), ensure_ascii=False), int(item.get("revision_number") or 1),
                 timestamp, timestamp),
            )


def run_startup_maintenance() -> None:
    """Run historic data migrations once instead of delaying every app launch.

    New recordings, chat imports and review actions already update their own
    data at the point of change.  These tasks only exist to bring databases
    created by older ClipFinder versions up to the current feature set.
    """
    tasks = (
        ("segment-quality-v1", backfill_segment_quality),
        ("reading-filter-v3", backfill_reading_filter),
        ("context-signals-v1", backfill_context_signals),
        ("segment-context-v1", backfill_segment_context),
        ("legacy-game-audio-v1", remove_legacy_game_audio_bonus),
        ("moment-reactions-v1", backfill_moment_reactions),
        ("game-reaction-threshold-v2", backfill_stricter_game_reaction_tags),
        ("duplicate-groups-v1", backfill_duplicate_groups),
        ("detailed-tags-v2", backfill_detailed_tags),
        ("preference-feedback-v1", backfill_preference_feedback),
    )
    tasks = (*tasks, ("chat-reactions-v1", lambda: [apply_chat_reactions(item["video_id"]) for item in db.rows("SELECT video_id FROM chat_settings")]))
    tasks = (*tasks, ("short-potential-v1", backfill_short_potential))
    for task_name, callback in tasks:
        if db.maintenance_task_completed(task_name):
            continue
        started = time.perf_counter()
        diagnostics.logger().info("Startup maintenance started: %s", task_name)
        try:
            callback()
        except Exception as exc:
            # These are repairable feature backfills, not schema migrations.
            # One malformed legacy row must not make the whole desktop app
            # unavailable; an unmarked task is retried on the next launch.
            diagnostics.log_failure(f"Startup maintenance deferred: {task_name}", exc)
        else:
            db.mark_maintenance_task_completed(task_name)
            diagnostics.logger().info(
                "Startup maintenance completed: %s in %.2fs",
                task_name,
                time.perf_counter() - started,
            )


_durable_worker: DurableBackgroundWorker | None = None
_durable_worker_start_lock = threading.Lock()


def _start_durable_worker_after_healthcheck() -> None:
    """Start cleanup/queue work only after the local API is reachable.

    The desktop wrapper waits for ``/api/health`` before it loads the main
    window.  A worker can immediately scan a sizeable local library when it
    acquires its lease, so starting it during the ASGI lifespan used to let
    that optional work delay the first health response.  Starting it from the
    health endpoint keeps startup deterministic while retaining the same
    durable queue behaviour for every normal desktop launch.
    """
    worker = _durable_worker
    if worker is None or worker.running:
        return
    with _durable_worker_start_lock:
        worker = _durable_worker
        if worker is None or worker.running:
            return
        diagnostics.logger().info("Starting durable worker after local health check")
        worker.start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _durable_worker
    diagnostics.configure()
    diagnostics.logger().info("Backend starting: version=%s pid=%s", __version__, os.getpid())
    db.initialize()
    run_startup_maintenance()
    current_runtime = runtime_status()
    diagnostics.logger().info(
        "Runtime detected: headline=%s transcription=%s similarity=%s gpu=%s",
        current_runtime.get("headline"),
        current_runtime.get("transcription", {}).get("label"),
        current_runtime.get("embeddings", {}).get("label"),
        current_runtime.get("gpu", {}).get("name") if current_runtime.get("gpu") else "not detected",
    )
    _durable_worker = build_durable_worker()
    diagnostics.logger().info("Backend API ready; durable worker is deferred until the first health check")
    try:
        yield
    finally:
        with _durable_worker_start_lock:
            worker = _durable_worker
            _durable_worker = None
        if worker is not None:
            stopped = worker.stop(timeout=10.0)
            if not stopped:
                diagnostics.logger().warning(
                    "Durable worker is still finishing a native operation; its leased job will resume after restart"
                )
        diagnostics.logger().info("Backend stopped")


app = FastAPI(title="ClipFinder Local", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/assets", StaticFiles(directory=Path(__file__).parent.parent / "assets"), name="assets")


@app.middleware("http")
async def prevent_frontend_cache(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception as exc:
        diagnostics.log_failure(f"Unhandled API error: {request.method} {request.url.path}", exc)
        raise
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def not_found(detail: str = "Resource not found"):
    raise HTTPException(status_code=404, detail=detail)


def update_job(job_id: str, progress: int, message: str, state: str = "running") -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE jobs SET state=?, progress=?, message=?, updated_at=? WHERE id=?",
            (state, max(0, min(progress, 100)), message, db.now(), job_id),
        )


def save_analysis_duration(video_id: str, started_at: float) -> None:
    """Store elapsed wall time for the most recent analysis attempt."""
    elapsed = round(max(0.0, time.monotonic() - started_at), 2)
    with db.connection() as con:
        con.execute("UPDATE videos SET analysis_seconds=?, updated_at=? WHERE id=?", (elapsed, db.now(), video_id))


def estimate_analysis_duration(video: dict) -> tuple[float | None, int]:
    """Estimate a new run from the user's completed analyses.

    The ratio of processing time to video length is more stable than a fixed
    number of minutes.  Use a median to keep one unusually slow model download
    or long video from skewing the prediction.
    """
    duration = float(video.get("duration_seconds") or 0)
    if duration <= 0 or video.get("status") not in {"queued", "processing"}:
        return None, 0
    audio_mode = str(video.get("audio_analysis_mode") or "single")
    analysis_mode = str(video.get("analysis_mode") or "default")
    history = db.rows(
        """SELECT duration_seconds, analysis_seconds FROM videos
           WHERE status='ready' AND id != ? AND audio_analysis_mode=? AND analysis_mode=?
             AND duration_seconds >= 30 AND analysis_seconds > 1
           ORDER BY updated_at DESC""",
        (video["id"], audio_mode, analysis_mode),
    )
    ratios = [float(item["analysis_seconds"]) / float(item["duration_seconds"]) for item in history if item["duration_seconds"]]
    if not ratios:
        return None, 0
    # Recent recordings better reflect the current model cache and machine
    # state; the median of the latest eight stays robust against outliers.
    ratio = statistics.median(ratios[:8])
    return round(max(15.0, duration * ratio), 1), min(8, len(ratios))


def approximate_word_timestamps(transcript: str, start: float, end: float) -> list[dict]:
    """Use proportional word timing after a user rewrites the transcript."""
    tokens = re.findall(r"\S+", transcript)
    if not tokens:
        return []
    total = sum(max(1, len(token)) for token in tokens)
    cursor = start
    result = []
    for token in tokens:
        span = (end - start) * max(1, len(token)) / total
        result.append({"start": cursor, "end": cursor + span, "word": token})
        cursor += span
    return result


def _segment_context(video_id: str, segment_id: str, start: float, end: float, window: float = 16.0) -> tuple[str, str]:
    """Load neighbouring current speech for deterministic editor rescoring."""
    neighbours = db.rows(
        """SELECT id, start_seconds, end_seconds, transcript FROM segments
           WHERE video_id=? AND lifecycle_state='current' AND id != ?
             AND end_seconds >= ? AND start_seconds <= ?
           ORDER BY start_seconds""",
        (video_id, segment_id, max(0.0, start - window), end + window),
    )
    before = " ".join(
        str(item.get("transcript") or "") for item in neighbours
        if float(item["end_seconds"]) <= start
    )[-900:]
    after = " ".join(
        str(item.get("transcript") or "") for item in neighbours
        if float(item["start_seconds"]) >= end
    )[:900]
    return before, after


def _encode_feature_updates(values: dict) -> dict:
    encoded = dict(values)
    for field in (
        "keywords", "tags", "word_timestamps", "quality_signals",
        "short_potential_signals", "boundary_signals", "context_signals",
        "extended_story_signals",
        "chat_messages",
    ):
        if field in encoded and not isinstance(encoded[field], str):
            encoded[field] = json.dumps(encoded[field], ensure_ascii=False)
    if "embedding" in encoded and encoded["embedding"] is not None and not isinstance(encoded["embedding"], str):
        encoded["embedding"] = json.dumps(encoded["embedding"])
    return encoded


_GRAPH_JSON_FIELDS = {
    "keywords", "tags", "word_timestamps", "quality_signals",
    "short_potential_signals", "boundary_signals", "context_signals",
    "extended_story_signals",
    "chat_messages",
}


def _decoded_graph_state(segment: dict) -> dict:
    state = dict(segment)
    for field in _GRAPH_JSON_FIELDS:
        value = state.get(field)
        if isinstance(value, str):
            try:
                decoded = json.loads(value or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = []
            state[field] = decoded if isinstance(decoded, list) else []
    return state


def _recompute_persisted_segment(
    segment_id: str,
    changed_fields: set[str] | None = None,
    overrides: dict | None = None,
) -> dict:
    """Run the canonical graph for one legacy/current segment and sync its snapshot."""
    segment = db.row(
        """SELECT s.*, v.analysis_mode FROM segments s
           JOIN videos v ON v.id=s.video_id
           WHERE s.id=? AND s.lifecycle_state='current'""",
        (segment_id,),
    )
    if not segment:
        return {}
    state = _decoded_graph_state(segment)
    state.update(overrides or {})
    result = recompute_segment_features(state, changed_fields)
    persisted = {
        **{key: value for key, value in (overrides or {}).items() if key in db.SEGMENT_MACHINE_COLUMNS},
        **result.updates,
    }
    update_current_segment_and_revision(
        segment_id,
        _encode_feature_updates(persisted),
        expected_revision_number=int(segment.get("revision_number") or 1),
    )
    return result.updates


def _refresh_duplicate_groups(video_id: str) -> None:
    """Rebuild duplicate groups after a manual timing or transcript edit."""
    items = db.rows(
        """SELECT id, start_seconds, end_seconds, embedding, revision_number
           FROM segments
           WHERE video_id=? AND lifecycle_state='current' AND embedding IS NOT NULL
           ORDER BY start_seconds""",
        (video_id,),
    )
    records = [
        {
            "id": item["id"],
            "start": item["start_seconds"],
            "end": item["end_seconds"],
            "vector": json.loads(item["embedding"]),
            "duplicate_group": "",
        }
        for item in items
    ]
    assign_duplicate_groups(records)
    revisions = {str(item["id"]): int(item.get("revision_number") or 1) for item in items}
    with db.connection() as con:
        for record in records:
            update_current_segment_and_revision(
                str(record["id"]),
                {"duplicate_group": str(record.get("duplicate_group") or "")},
                con=con,
                expected_revision_number=revisions[str(record["id"])],
            )


def _refresh_neighbour_contexts(
    video_id: str,
    changed_segment_id: str,
    range_start: float,
    range_end: float,
    window: float = 16.0,
) -> None:
    """Rescore nearby moments whose context includes an edited utterance."""
    neighbours = db.rows(
        """SELECT id, start_seconds, end_seconds FROM segments
           WHERE video_id=? AND lifecycle_state='current' AND id<>?
             AND end_seconds>=? AND start_seconds<=?
           ORDER BY start_seconds""",
        (
            video_id,
            changed_segment_id,
            max(0.0, float(range_start) - window),
            float(range_end) + window,
        ),
    )
    for neighbour in neighbours:
        before, after = _segment_context(
            video_id,
            str(neighbour["id"]),
            float(neighbour["start_seconds"]),
            float(neighbour["end_seconds"]),
            window,
        )
        _recompute_persisted_segment(
            str(neighbour["id"]),
            {"context_before", "context_after"},
            {"context_before": before, "context_after": after},
        )


def run_analysis(video_id: str, job_id: str, report=None) -> None:
    """Execute one analysis attempt; the durable worker owns job finalization."""
    started_at = time.monotonic()
    publish = report or (lambda progress, message: update_job(job_id, progress, message))
    try:
        diagnostics.logger().info("Analysis started: video_id=%s job_id=%s", video_id, job_id)
        publish(1, "Waiting for worker")
        job = db.row("SELECT payload_json FROM jobs WHERE id=?", (job_id,)) or {}
        payload = job_queue.decode_payload(job)
        audio_snapshot = payload.get("analysis_audio")
        analyse(video_id, publish, audio_snapshot if isinstance(audio_snapshot, dict) else None)
        save_analysis_duration(video_id, started_at)
        set_latest_run_elapsed(video_id, time.monotonic() - started_at)
        diagnostics.logger().info("Analysis completed: video_id=%s job_id=%s elapsed_seconds=%.2f", video_id, job_id, time.monotonic() - started_at)
    except Exception as exc:
        elapsed = round(max(0.0, time.monotonic() - started_at), 2)
        fail_running_analysis(video_id, str(exc))
        set_latest_run_elapsed(video_id, elapsed)
        with db.connection() as con:
            con.execute("UPDATE videos SET analysis_seconds=?, updated_at=? WHERE id=?", (elapsed, db.now(), video_id))
        if not isinstance(exc, (WorkCancelled, WorkPaused)):
            diagnostics.log_failure(f"Analysis failed: video_id={video_id} job_id={job_id} elapsed_seconds={elapsed:.2f}", exc)
        # Native decoder/model/configuration failures are deterministic for a
        # given file and runtime. Repeating a multi-hour analysis three times
        # only wastes time and disk. Keep automatic retry for short-lived file,
        # database and network conditions; the user can explicitly reanalyse a
        # permanently failed recording after fixing its environment or source.
        transient = isinstance(exc, (PermissionError, TimeoutError)) or any(
            marker in str(exc).casefold()
            for marker in (
                "database is locked", "database is busy", "sharing violation",
                "temporarily unavailable", "temporary failure", "timed out",
                "connection reset", "connection aborted", "connection refused",
                "service unavailable", "http 502", "http 503", "http 504",
            )
        )
        if isinstance(exc, (WorkCancelled, WorkPaused, PermanentWorkError)) or transient:
            raise
        raise PermanentWorkError(str(exc)) from exc


def _supported_remote_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    supported = ("youtube.com", "youtu.be", "twitch.tv")
    if parsed.scheme not in {"http", "https"} or not host or not any(host == domain or host.endswith("." + domain) for domain in supported):
        raise HTTPException(400, "Use a public YouTube link or a Twitch VOD link.")
    return url


def _supported_reference_url(value: str) -> str:
    """Validate a public, single short/video used only as a local example."""
    url = value.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    supported = ("youtube.com", "youtu.be", "tiktok.com")
    if parsed.scheme not in {"http", "https"} or not host or not any(host == domain or host.endswith("." + domain) for domain in supported):
        raise HTTPException(400, "Use a public YouTube Short/video or TikTok link.")
    return url


def _downloaded_video_path(video_id: str) -> Path:
    candidates = [
        path for path in settings.incoming_dir.glob(f"{video_id}.*")
        if path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"} and path.is_file()
    ]
    if not candidates:
        raise RuntimeError("The download finished without a usable video file.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _downloaded_reference_path(import_id: str) -> Path:
    candidates = [
        path for path in settings.reference_dir.glob(f"{import_id}.*")
        if path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".avi"} and path.is_file()
    ]
    if not candidates:
        raise RuntimeError("The reference download finished without a usable video file.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _configure_remote_download_certificates() -> None:
    """Use Windows certificates, with certifi as a verified fallback.

    Remote downloads remain certificate-verified; this avoids yt-dlp's insecure
    no-check option while supporting local antivirus/enterprise HTTPS roots.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        return
    except ImportError:
        pass
    try:
        import certifi

        certificate_bundle = certifi.where()
    except (ImportError, OSError):
        return
    if Path(certificate_bundle).is_file():
        os.environ["SSL_CERT_FILE"] = certificate_bundle
        os.environ["REQUESTS_CA_BUNDLE"] = certificate_bundle


def run_remote_import(video_id: str, job_id: str, report=None) -> None:
    """Download and analyse one URL; the durable worker owns retries/state."""
    publish = report or (lambda progress, message: update_job(job_id, progress, message))
    video = db.row("SELECT source_url FROM videos WHERE id=?", (video_id,))
    if not video or not video.get("source_url"):
        raise PermanentWorkError("Remote source URL is missing.")
    # The recording card starts its progress bar with the download. Keep the
    # displayed elapsed time aligned with that whole visible job, not just the
    # later transcription stage.
    analysis_started_at = time.monotonic()
    try:
        diagnostics.logger().info("Remote import started: video_id=%s job_id=%s", video_id, job_id)
        from yt_dlp import YoutubeDL

        _configure_remote_download_certificates()

        last_progress = -1

        def report_download(status: dict) -> None:
            nonlocal last_progress
            if status.get("status") == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                current = status.get("downloaded_bytes") or 0
                percentage = int(current / total * 20) if total else 1
                if percentage != last_progress:
                    last_progress = percentage
                    publish(max(1, percentage), f"Downloading video: {min(100, int(current / total * 100)) if total else 'working'}%")
            elif status.get("status") == "finished":
                publish(20, "Download completed. Preparing analysis.")

        publish(1, "Preparing YouTube/Twitch download.")
        options = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": str(settings.incoming_dir / f"{video_id}.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [report_download],
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "quiet": True,
            "no_warnings": True,
        }
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(video["source_url"], download=True)
        source_path = _downloaded_video_path(video_id)
        title = str(info.get("title") or "Remote video").strip()
        with db.connection() as con:
            con.execute(
                "UPDATE videos SET original_name=?, path=?, status='queued', transcript_audio_track=1, audio_analysis_mode='single', error_message=NULL, updated_at=? WHERE id=?",
                (f"{title}{source_path.suffix}", str(source_path), db.now(), video_id),
            )
            # Once the source is complete, retries/restarts only need to redo
            # analysis; they must not contact the remote service again.
            con.execute(
                "UPDATE jobs SET kind='analysis', updated_at=? WHERE id=? AND state='running'",
                (db.now(), job_id),
            )
        job = db.row("SELECT payload_json FROM jobs WHERE id=?", (job_id,)) or {}
        payload = job_queue.decode_payload(job)
        audio_snapshot = payload.get("analysis_audio")
        analyse(
            video_id,
            lambda progress, message: publish(20 + int(progress * 0.8), message),
            audio_snapshot if isinstance(audio_snapshot, dict) else None,
        )
        save_analysis_duration(video_id, analysis_started_at)
        set_latest_run_elapsed(video_id, time.monotonic() - analysis_started_at)
        diagnostics.logger().info("Remote import and analysis completed: video_id=%s job_id=%s elapsed_seconds=%.2f", video_id, job_id, time.monotonic() - analysis_started_at)
    except ModuleNotFoundError as exc:
        detail = "Remote import requires yt-dlp. Run: python -m pip install -r requirements.txt" if exc.name == "yt_dlp" else str(exc)
        elapsed = round(max(0.0, time.monotonic() - analysis_started_at), 2)
        with db.connection() as con:
            con.execute("UPDATE videos SET analysis_seconds=?, updated_at=? WHERE id=?", (elapsed, db.now(), video_id))
        raise PermanentWorkError(detail) from exc
    except Exception as exc:
        detail = str(exc)
        elapsed = round(max(0.0, time.monotonic() - analysis_started_at), 2)
        fail_running_analysis(video_id, detail)
        set_latest_run_elapsed(video_id, elapsed)
        with db.connection() as con:
            con.execute("UPDATE videos SET analysis_seconds=?, updated_at=? WHERE id=?", (elapsed, db.now(), video_id))
        if not isinstance(exc, (WorkCancelled, WorkPaused)):
            diagnostics.log_failure(f"Remote import failed: video_id={video_id} job_id={job_id}", exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail.lower():
            detail = (
                "HTTPS certificate verification failed while contacting YouTube/Twitch. "
                "Update/reinstall ClipFinder so its certificate bundle is refreshed, then try again."
            )
        if isinstance(exc, (WorkCancelled, WorkPaused)):
            raise
        raise RuntimeError(detail) from exc


def update_reference_import(import_id: str, progress: int, message: str, state: str = "running", imported_files: int | None = None) -> None:
    with db.connection() as con:
        if imported_files is None:
            con.execute(
                "UPDATE reference_imports SET state=?, progress=?, message=?, updated_at=? WHERE id=?",
                (state, max(0, min(progress, 100)), message, db.now(), import_id),
            )
        else:
            con.execute(
                "UPDATE reference_imports SET state=?, progress=?, message=?, imported_files=?, updated_at=? WHERE id=?",
                (state, max(0, min(progress, 100)), message, imported_files, db.now(), import_id),
            )


def run_reference_import(
    collection_id: str,
    import_id: str,
    folder_path: str,
    include_subfolders: bool,
    report=None,
) -> int:
    """Import a saved folder; durable queue state is finalized by the worker."""
    publish = report or (lambda progress, message: update_reference_import(import_id, progress, message))
    publish(1, "Reading reference folder")
    count = import_reference_folder(collection_id, folder_path, include_subfolders, publish)
    with db.connection() as con:
        con.execute(
            "UPDATE reference_imports SET imported_files=?, message=?, updated_at=? WHERE id=?",
            (count, f"Imported {count} reference clips", db.now(), import_id),
        )
    return count


def run_reference_url_import(collection_id: str, import_id: str, source_url: str, report=None) -> int:
    """Download one public short/video, then index it as a collection example."""
    publish = report or (lambda progress, message: update_reference_import(import_id, progress, message))
    try:
        from yt_dlp import YoutubeDL

        _configure_remote_download_certificates()
        publish(1, "Preparing reference download")

        def report_download(status: dict) -> None:
            if status.get("status") == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                current = status.get("downloaded_bytes") or 0
                percent = int(current / total * 45) if total else 5
                publish(max(2, min(45, percent)), "Downloading reference clip")
            elif status.get("status") == "finished":
                publish(46, "Download complete. Transcribing reference clip")

        options = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": str(settings.reference_dir / f"{import_id}.%(ext)s"),
            "noplaylist": True,
            "progress_hooks": [report_download],
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "quiet": True,
            "no_warnings": True,
        }
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(source_url, download=True)
        source_path = _downloaded_reference_path(import_id)
        title = str(info.get("title") or source_path.stem).strip()
        count = import_reference_files(
            collection_id,
            [source_path],
            lambda progress, message: publish(46 + int(progress * 0.54), message),
            {source_path.resolve(): source_url},
        )
        with db.connection() as con:
            con.execute(
                """INSERT INTO reference_url_sources (id, collection_id, source_url, source_path, original_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(collection_id, source_url) DO UPDATE SET source_path=excluded.source_path, original_name=excluded.original_name, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), collection_id, source_url, str(source_path), title, db.now(), db.now()),
            )
        with db.connection() as con:
            con.execute(
                "UPDATE reference_imports SET imported_files=?, message=?, updated_at=? WHERE id=?",
                (count, f"Imported {count} reference clip from link", db.now(), import_id),
            )
        return count
    except Exception as exc:
        detail = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail.lower():
            detail = "HTTPS certificate verification failed while downloading the reference link. Update/reinstall ClipFinder, then try again."
        if isinstance(exc, (WorkCancelled, WorkPaused)):
            raise
        if isinstance(exc, ModuleNotFoundError):
            raise PermanentWorkError(detail) from exc
        raise RuntimeError(detail) from exc


def _wake_durable_worker() -> None:
    if _durable_worker is not None:
        _durable_worker.wake()


def _sync_video_job(job: dict | None) -> None:
    if not job:
        return
    state = str(job.get("state") or "")
    video_id = str(job.get("video_id") or "")
    if not video_id:
        return
    timestamp = db.now()
    with db.connection() as con:
        if state == "queued":
            con.execute(
                "UPDATE videos SET status='queued', error_message=NULL, updated_at=? WHERE id=?",
                (timestamp, video_id),
            )
        elif state == "running":
            con.execute(
                "UPDATE videos SET status='processing', error_message=NULL, updated_at=? WHERE id=?",
                (timestamp, video_id),
            )
        elif state == "paused":
            con.execute(
                "UPDATE videos SET status='paused', error_message=NULL, updated_at=? WHERE id=?",
                (timestamp, video_id),
            )
        elif state == "completed":
            con.execute(
                "UPDATE videos SET status='ready', error_message=NULL, updated_at=? WHERE id=?",
                (timestamp, video_id),
            )
        elif state in {"failed", "cancelled"}:
            current_run = con.execute(
                "SELECT 1 FROM analysis_runs WHERE video_id=? AND is_current=1 AND state='completed' LIMIT 1",
                (video_id,),
            ).fetchone()
            # A failed/cancelled reanalysis must not hide the last successful
            # current run. Keep that result reviewable and surface the latest
            # job message as a warning on the recording card.
            status = "ready" if current_run else "failed"
            detail = str(job.get("last_error") or job.get("message") or state.title())
            con.execute(
                "UPDATE videos SET status=?, error_message=?, updated_at=? WHERE id=?",
                (status, detail, timestamp, video_id),
            )


def _claim_video_job(worker_id: str, lease_seconds: float) -> dict | None:
    with db.connection() as con:
        released = job_queue.release_expired_leases(con)
        claimed = job_queue.claim_next(con, worker_id=worker_id, lease_seconds=lease_seconds)
    for item in released:
        _sync_video_job(item)
    if released:
        diagnostics.logger().warning("Recovered %s recording job(s) with expired leases", len(released))
    _sync_video_job(claimed)
    return claimed


def _heartbeat_video_job(job_id: str, lease_token: str, lease_seconds: float) -> dict | None:
    with db.connection() as con:
        return job_queue.heartbeat(con, job_id, lease_token, lease_seconds=lease_seconds)


def _progress_video_job(job_id: str, lease_token: str, progress: int, message: str,
                        lease_seconds: float) -> dict | None:
    with db.connection() as con:
        return job_queue.update_progress(
            con, job_id, lease_token, progress, message, lease_seconds=lease_seconds,
        )


def _complete_video_job(job_id: str, lease_token: str) -> dict | None:
    with db.connection() as con:
        result = job_queue.complete(con, job_id, lease_token, message="Analysis completed")
    _sync_video_job(result)
    return result


def _fail_video_job(job_id: str, lease_token: str, error: str, retryable: bool) -> dict | None:
    with db.connection() as con:
        result = job_queue.fail(con, job_id, lease_token, error, retryable=retryable)
    _sync_video_job(result)
    return result


def _recover_video_jobs() -> None:
    with db.connection() as con:
        timestamp = db.now()
        # Older releases could restart between atomically activating a run and
        # marking its public job complete. Do not redo work that is already a
        # valid current result.
        con.execute(
            """UPDATE jobs
               SET state='completed', progress=100,
                   message='Analysis completed before restart', updated_at=?
               WHERE state IN ('running', 'interrupted')
                 AND EXISTS (
                     SELECT 1 FROM videos v
                     WHERE v.id=jobs.video_id AND v.status='ready'
                 )
                 AND EXISTS (
                     SELECT 1 FROM analysis_runs ar
                     WHERE ar.video_id=jobs.video_id AND ar.is_current=1 AND ar.state='completed'
                 )""",
            (timestamp,),
        )
        con.execute(
            """UPDATE analysis_runs
               SET state='interrupted', error_message='Application restarted', completed_at=?
               WHERE state='running'""",
            (timestamp,),
        )
        recovered = job_queue.recover_abandoned(con)
        # Reconcile the public recording card from the latest durable row as
        # well. A process can exit after the queue transaction commits but
        # before the best-effort videos-table projection is updated.
        latest = [dict(item) for item in con.execute(
            """SELECT j.* FROM jobs j
               WHERE NOT EXISTS (
                   SELECT 1 FROM jobs newer
                   WHERE newer.video_id=j.video_id
                     AND (newer.created_at > j.created_at
                          OR (newer.created_at=j.created_at AND newer.id > j.id))
               )
               ORDER BY j.created_at, j.id"""
        ).fetchall()]
    for item in [*recovered, *latest]:
        _sync_video_job(item)
    if recovered:
        diagnostics.logger().warning("Recovered %s recording job(s) after restart", len(recovered))


def _dispatch_video_job(job: dict, report, should_cancel) -> None:
    if should_cancel():
        raise WorkCancelled("Cancellation requested")
    video = db.row("SELECT * FROM videos WHERE id=?", (job["video_id"],))
    if not video:
        raise PermanentWorkError("Video was deleted before its queued job started.")
    if video.get("source_removed"):
        raise PermanentWorkError("The source video was removed and cannot be analysed again.")
    source = Path(str(video.get("path") or ""))
    kind = str(job.get("kind") or "analysis")
    if kind == "remote_import" or (not source.is_file() and video.get("source_url")):
        run_remote_import(str(video["id"]), str(job["id"]), report)
        return
    if kind != "analysis":
        raise PermanentWorkError(f"Unsupported recording job kind: {kind}")
    if not source.is_file():
        raise PermanentWorkError("The original recording file is no longer available.")
    run_analysis(str(video["id"]), str(job["id"]), report)


def _claim_reference_job(worker_id: str, lease_seconds: float) -> dict | None:
    with db.connection() as con:
        released = reference_queue.release_expired_leases(con)
        claimed = reference_queue.claim_next(con, worker_id=worker_id, lease_seconds=lease_seconds)
    if released:
        diagnostics.logger().warning("Recovered %s reference import(s) with expired leases", len(released))
    return claimed


def _heartbeat_reference_job(import_id: str, lease_token: str, lease_seconds: float) -> dict | None:
    with db.connection() as con:
        return reference_queue.heartbeat(con, import_id, lease_token, lease_seconds=lease_seconds)


def _progress_reference_job(import_id: str, lease_token: str, progress: int, message: str,
                            lease_seconds: float) -> dict | None:
    with db.connection() as con:
        return reference_queue.update_progress(
            con, import_id, lease_token, progress, message, lease_seconds=lease_seconds,
        )


def _complete_reference_job(import_id: str, lease_token: str) -> dict | None:
    current = db.row("SELECT imported_files FROM reference_imports WHERE id=?", (import_id,)) or {}
    imported = int(current.get("imported_files") or 0)
    with db.connection() as con:
        return reference_queue.complete(
            con,
            import_id,
            lease_token,
            message=f"Imported {imported} reference clip{'s' if imported != 1 else ''}",
            imported_files=imported,
        )


def _fail_reference_job(import_id: str, lease_token: str, error: str, retryable: bool) -> dict | None:
    with db.connection() as con:
        return reference_queue.fail(con, import_id, lease_token, error, retryable=retryable)


def _recover_reference_jobs() -> None:
    with db.connection() as con:
        recovered = reference_queue.recover_abandoned(con)
    if recovered:
        diagnostics.logger().warning("Recovered %s reference import(s) after restart", len(recovered))


def _dispatch_reference_job(job: dict, report, should_cancel) -> None:
    if should_cancel():
        raise WorkCancelled("Cancellation requested")
    if not db.row("SELECT id FROM collections WHERE id=?", (job["collection_id"],)):
        raise PermanentWorkError("The reference collection was deleted.")
    kind = str(job.get("kind") or "folder")
    source = str(job.get("folder_path") or "")
    if kind == "folder":
        folder = Path(source).expanduser()
        if not folder.is_dir():
            raise PermanentWorkError("The saved reference folder is no longer available.")
        run_reference_import(
            str(job["collection_id"]), str(job["id"]), str(folder),
            bool(job.get("include_subfolders")), report,
        )
        return
    if kind == "url":
        try:
            source_url = _supported_reference_url(source)
        except HTTPException as exc:
            raise PermanentWorkError(str(exc.detail)) from exc
        run_reference_url_import(str(job["collection_id"]), str(job["id"]), source_url, report)
        return
    raise PermanentWorkError(f"Unsupported reference job kind: {kind}")


def _cleanup_abandoned_temporary_files() -> None:
    active_paths: list[Path] = []
    for item in db.rows(
        """SELECT v.id, v.path FROM jobs j JOIN videos v ON v.id=j.video_id
           WHERE j.state IN ('queued', 'running')"""
    ):
        active_paths.append(Path(item["path"]))
        active_paths.extend(settings.incoming_dir.glob(f"{item['id']}*"))
        active_paths.extend(settings.work_dir.glob(f"{item['id']}*"))
    for item in db.rows(
        "SELECT id FROM reference_imports WHERE state IN ('queued', 'running')"
    ):
        active_paths.extend(settings.reference_dir.glob(f"{item['id']}*"))
    result = cleanup_workspace(
        [
            settings.incoming_dir,
            settings.work_dir,
            settings.reference_dir,
            settings.clipfinder_data_dir.parent / "updates",
        ],
        older_than_seconds=24 * 60 * 60,
        active_paths=active_paths,
        dry_run=False,
    )
    if result.deleted or result.errors:
        diagnostics.logger().info(
            "Temporary workspace cleanup: deleted=%s skipped=%s errors=%s",
            len(result.deleted), len(result.skipped), len(result.errors),
        )
    try:
        cache_result = PipelineCache(settings.pipeline_cache_dir).cleanup(
            older_than_seconds=180 * 24 * 60 * 60,
            max_total_bytes=4 * 1024 * 1024 * 1024,
            temporary_older_than_seconds=60 * 60,
            dry_run=False,
        )
    except Exception as exc:
        diagnostics.log_failure("Pipeline cache cleanup skipped", exc)
    else:
        if cache_result.removed or cache_result.errors:
            diagnostics.logger().info(
                "Pipeline cache cleanup: removed=%s reclaimed_bytes=%s errors=%s",
                len(cache_result.removed), cache_result.reclaimed_bytes,
                len(cache_result.errors),
            )


def build_durable_worker() -> DurableBackgroundWorker:
    video_adapter = QueueAdapter(
        name="recording",
        claim=_claim_video_job,
        heartbeat=_heartbeat_video_job,
        update_progress=_progress_video_job,
        complete=_complete_video_job,
        fail=_fail_video_job,
        dispatch=_dispatch_video_job,
        recover=_recover_video_jobs,
    )
    reference_adapter = QueueAdapter(
        name="reference import",
        claim=_claim_reference_job,
        heartbeat=_heartbeat_reference_job,
        update_progress=_progress_reference_job,
        complete=_complete_reference_job,
        fail=_fail_reference_job,
        dispatch=_dispatch_reference_job,
        recover=_recover_reference_jobs,
    )
    return DurableBackgroundWorker(
        [video_adapter, reference_adapter],
        lock_directory=settings.work_dir / "locks",
        on_acquired=_cleanup_abandoned_temporary_files,
        on_error=lambda context, exc: diagnostics.log_failure(context, exc),
    )


# Remote previews deliberately stay out of SQLite: they are a transient way to
# review one public Short/video. The source media is never stored in a
# collection, incoming recording or export directory.
_remote_preview_jobs: dict[str, dict] = {}
_remote_preview_fingerprints: dict[str, dict] = {}
_remote_preview_lock = threading.RLock()
_media_output_lock = threading.RLock()
_REMOTE_PREVIEW_MAX_SECONDS = 600
_REMOTE_PREVIEW_TTL_SECONDS = 60 * 60
_REMOTE_PREVIEW_MAX_JOBS = 24


def _prune_remote_previews(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    with _remote_preview_lock:
        removable = [
            job_id for job_id, job in _remote_preview_jobs.items()
            if job.get("state") not in {"queued", "running"}
            and current - float(job.get("updated_monotonic") or current) >= _REMOTE_PREVIEW_TTL_SECONDS
        ]
        for job_id in removable:
            _remote_preview_jobs.pop(job_id, None)
            _remote_preview_fingerprints.pop(job_id, None)
        overflow = max(0, len(_remote_preview_jobs) - _REMOTE_PREVIEW_MAX_JOBS)
        if overflow:
            completed = sorted(
                ((job_id, job) for job_id, job in _remote_preview_jobs.items() if job.get("state") not in {"queued", "running"}),
                key=lambda item: float(item[1].get("updated_monotonic") or 0),
            )
            for job_id, _ in completed[:overflow]:
                _remote_preview_jobs.pop(job_id, None)
                _remote_preview_fingerprints.pop(job_id, None)


def _update_remote_preview(job_id: str, *, state: str | None = None, progress: int | None = None,
                           message: str | None = None, result: dict | None = None) -> None:
    with _remote_preview_lock:
        job = _remote_preview_jobs.get(job_id)
        if not job:
            return
        if state is not None:
            job["state"] = state
        if progress is not None:
            job["progress"] = max(0, min(100, int(progress)))
        if message is not None:
            job["message"] = message
        if result is not None:
            job["result"] = result
        job["updated_monotonic"] = time.monotonic()


def _remote_stream_info(source_url: str, selector: str) -> tuple[dict, str]:
    from yt_dlp import YoutubeDL

    options = {
        "format": selector,
        "noplaylist": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "extractor_retries": 2,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(source_url, download=False)
    stream_url = str(info.get("url") or "")
    if not stream_url:
        raise RuntimeError("The service did not provide a usable temporary stream for this link.")
    return info, stream_url


def _preview_ffmpeg_headers(info: dict) -> list[str]:
    headers = info.get("http_headers") or {}
    useful = [(str(key), str(value)) for key, value in headers.items()
              if str(key).lower() in {"user-agent", "referer", "origin"} and value]
    if not useful:
        return []
    return ["-headers", "".join(f"{key}: {value}\\r\\n" for key, value in useful)]


def run_remote_preview(job_id: str, source_url: str) -> None:
    work_dir = settings.work_dir / f"remote-preview-{job_id}"
    audio_path = work_dir / "audio.wav"
    frame_path = work_dir / "frame.jpg"
    try:
        from yt_dlp import YoutubeDL  # Clear error if the optional dependency is absent.
        del YoutubeDL
        _configure_remote_download_certificates()
        work_dir.mkdir(parents=True, exist_ok=True)
        _update_remote_preview(job_id, state="running", progress=4, message="Reading public link metadata")
        info, audio_url = _remote_stream_info(source_url, "bestaudio/best")
        duration = float(info.get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("Could not determine the video duration. Try a public, single Short/video link.")
        if duration > _REMOTE_PREVIEW_MAX_SECONDS:
            raise RuntimeError("This preview accepts one Short/video up to 10 minutes. Use normal recording analysis for longer material.")
        title = str(info.get("title") or "Untitled short/video").strip()

        _update_remote_preview(job_id, progress=12, message="Streaming temporary audio for analysis")
        run_media_command([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            *_preview_ffmpeg_headers(info), "-i", audio_url, "-t", f"{duration:.3f}",
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path),
        ])

        _update_remote_preview(job_id, progress=24, message="Capturing one temporary preview frame")
        frame_data_url = ""
        try:
            frame_info, frame_url = _remote_stream_info(source_url, "bestvideo[height<=480]/best")
            run_media_command([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                *_preview_ffmpeg_headers(frame_info), "-ss", "1", "-i", frame_url,
                "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "6", str(frame_path),
            ])
            if frame_path.is_file() and frame_path.stat().st_size <= 900_000:
                frame_data_url = "data:image/jpeg;base64," + base64.b64encode(frame_path.read_bytes()).decode("ascii")
        except Exception as frame_error:
            diagnostics.logger().info("Remote preview frame skipped: %s", frame_error)

        def report(progress: int, message: str) -> None:
            _update_remote_preview(job_id, progress=25 + int(progress * 0.6), message=message)

        _update_remote_preview(job_id, progress=28, message="Transcribing temporary audio")
        parts = transcribe(audio_path, report, duration=duration, progress_start=5, progress_end=95)
        transcript = " ".join(str(part.get("text") or "").strip() for part in parts).strip()
        words = [word for part in parts for word in (part.get("words") or [])]
        _update_remote_preview(job_id, progress=85, message="Scoring transcript")
        embedding = embed_texts([transcript or "no speech detected"])[0]
        semantic_tags = infer_tags(transcript, embedding)
        preview_features = recompute_segment_features({
            "transcript": transcript,
            "start_seconds": 0.0,
            "end_seconds": duration,
            "word_timestamps": words,
            "tags": semantic_tags,
            "analysis_mode": "default",
            # The complete remote short is the available context window.
            "context_before": "",
            "context_after": "",
            "boundary_signals": [],
            "extended_completeness_score": -1,
            "game_reaction_score": 0,
            "voice_expression_score": 0,
            "vision_score": 0,
            "chat_reaction_score": 0,
            "chat_joy_score": 0,
        }).updates
        tags = preview_features["tags"]
        quality = int(preview_features["quality_score"])
        quality_signals = preview_features["quality_signals"]
        reading_likelihood = float(preview_features["reading_likelihood"])
        logical_sense = int(preview_features["logical_sense_score"])
        with _remote_preview_lock:
            _remote_preview_fingerprints[job_id] = {
                "duration_seconds": duration,
                "tags": tags,
                "quality_score": quality,
                "logical_sense_score": logical_sense,
                "reading_likelihood": reading_likelihood,
                "embedding": embedding,
            }

        result = {
            "title": title,
            "source_url": source_url,
            "duration_seconds": round(duration, 2),
            "transcript": transcript,
            "tags": tags,
            "quality_score": quality,
            "quality_signals": quality_signals,
            "logical_sense_score": logical_sense,
            "reading_likelihood": reading_likelihood,
            "frame_data_url": frame_data_url,
            "retention": "Temporary audio and frame were deleted after analysis. The original video was not saved.",
        }
        _update_remote_preview(job_id, state="completed", progress=100, message="Preview analysis completed", result=result)
    except Exception as exc:
        detail = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail.lower():
            detail = "HTTPS certificate verification failed. Update/reinstall ClipFinder, then try again."
        diagnostics.log_failure(f"Remote preview failed: url={source_url}", exc)
        _update_remote_preview(job_id, state="failed", progress=100, message=detail)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health():
    _start_durable_worker_after_healthcheck()
    return {"status": "ok", "data_dir": str(settings.clipfinder_data_dir), "version": __version__}


@app.post("/api/remote-preview", status_code=202)
def create_remote_preview(body: RemotePreviewCreate, background_tasks: BackgroundTasks):
    source_url = _supported_reference_url(body.source_url)
    job_id = str(uuid.uuid4())
    _prune_remote_previews()
    with _remote_preview_lock:
        _remote_preview_jobs[job_id] = {
            "id": job_id, "state": "queued", "progress": 0, "message": "Queued", "result": None,
            "updated_monotonic": time.monotonic(),
        }
    _prune_remote_previews()
    background_tasks.add_task(run_remote_preview, job_id, source_url)
    return {"job_id": job_id}


@app.get("/api/remote-preview/{job_id}")
def get_remote_preview(job_id: str):
    _prune_remote_previews()
    with _remote_preview_lock:
        job = deepcopy(_remote_preview_jobs.get(job_id))
    if not job:
        not_found("Preview job not found. Preview results are available only during this app session.")
    job.pop("updated_monotonic", None)
    return job


@app.post("/api/remote-preview/{job_id}/save-pattern", status_code=201)
def save_remote_preview_pattern(job_id: str, body: RemotePreviewSave):
    with _remote_preview_lock:
        job = deepcopy(_remote_preview_jobs.get(job_id))
        fingerprint = deepcopy(_remote_preview_fingerprints.get(job_id))
    if not job or job.get("state") != "completed" or not fingerprint:
        raise HTTPException(400, "Complete the temporary preview before saving its analysis pattern.")
    pattern_set = db.row("SELECT id, name FROM discovery_pattern_sets WHERE id=?", (body.pattern_set_id,))
    if not pattern_set:
        not_found("Discovery pattern set not found")
    with db.connection() as con:
        con.execute(
            """INSERT INTO discovery_pattern_examples
               (id, pattern_set_id, duration_seconds, tags, quality_score, logical_sense_score, reading_likelihood, embedding, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), pattern_set["id"], float(fingerprint["duration_seconds"]),
                json.dumps(canonicalize_tags(fingerprint["tags"]), ensure_ascii=False), int(fingerprint["quality_score"]),
                int(fingerprint["logical_sense_score"]), float(fingerprint["reading_likelihood"]),
                json.dumps(fingerprint["embedding"]), db.now(),
            ),
        )
    with _remote_preview_lock:
        _remote_preview_fingerprints.pop(job_id, None)
    return {"ok": True, "pattern_set_id": pattern_set["id"], "pattern_set_name": pattern_set["name"]}


@app.get("/api/runtime-status")
def get_runtime_status():
    return {**runtime_status(), "version": __version__}


@app.get("/api/statistics")
def app_statistics():
    """Return privacy-safe, local review feedback for the Statistics tab."""
    rows = db.rows(
        """SELECT r.rating, r.review_reason, sr.payload_json,
                  s.tags, s.quality_score, s.short_potential_score,
                  s.logical_sense_score, s.context_score, s.self_contained_score, s.extended_completeness_score, s.opening_clarity_score, s.extended_punchline_score,
                  s.reading_likelihood, v.analysis_mode,
                  pf.platform, pf.views, pf.average_watch_percent, pf.shares, pf.comments
           FROM segments s
           JOIN videos v ON v.id=s.video_id
           JOIN segment_reviews r ON r.segment_id=s.id
           LEFT JOIN segment_revisions sr ON sr.id=r.reviewed_revision_id
           LEFT JOIN publication_feedback pf ON pf.segment_id=s.id
           WHERE s.lifecycle_state='current' OR r.rating IN ('accepted', 'rejected')"""
    )
    decisions = Counter({"accepted": 0, "rejected": 0, "unrated": 0})
    reasons: Counter[str] = Counter()
    tags: dict[str, Counter[str]] = defaultdict(Counter)
    modes: dict[str, Counter[str]] = defaultdict(Counter)
    score_values: dict[str, dict[str, list[float]]] = {
        "quality": defaultdict(list), "short_potential": defaultdict(list),
        "logical_sense": defaultdict(list), "context": defaultdict(list),
        "self_contained": defaultdict(list), "extended_completeness": defaultdict(list),
        "opening_clarity": defaultdict(list), "punchline": defaultdict(list),
    }
    reading = Counter({"accepted": 0, "rejected": 0, "unrated": 0})
    published_rows: list[dict] = []
    for row in rows:
        try:
            reviewed_machine = json.loads(row.get("payload_json") or "{}")
        except (TypeError, ValueError):
            reviewed_machine = {}
        if isinstance(reviewed_machine, dict):
            # Statistics describe what the user actually reviewed, not a newer
            # machine revision that inherited the stable moment ID.
            row = {**row, **reviewed_machine}
        rating = str(row.get("rating") or "unrated")
        if rating not in decisions:
            rating = "unrated"
        decisions[rating] += 1
        modes[str(row.get("analysis_mode") or "default")][rating] += 1
        if rating == "rejected":
            reasons[str(row.get("review_reason") or "No reason given")] += 1
        try:
            row_tags = canonicalize_tags(json.loads(row.get("tags") or "[]"))
        except (TypeError, ValueError):
            row_tags = []
        for tag in row_tags:
            if isinstance(tag, str) and tag.strip():
                tags[tag.strip()][rating] += 1
        if float(row.get("reading_likelihood") or 0) >= 0.48:
            reading[rating] += 1
        if row.get("platform") or int(row.get("views") or 0) or int(row.get("shares") or 0) or int(row.get("comments") or 0):
            published_rows.append(row)
        if rating in {"accepted", "rejected"}:
            for key, column in (
                ("quality", "quality_score"), ("short_potential", "short_potential_score"),
                ("logical_sense", "logical_sense_score"), ("context", "context_score"),
                ("self_contained", "self_contained_score"), ("extended_completeness", "extended_completeness_score"),
                ("opening_clarity", "opening_clarity_score"), ("punchline", "extended_punchline_score"),
            ):
                value = float(row.get(column) or -1)
                if value >= 0:
                    score_values[key][rating].append(value)

    reviewed = decisions["accepted"] + decisions["rejected"]
    def average(values: list[float]) -> int | None:
        return round(sum(values) / len(values)) if values else None
    # Show saved custom reasons even before they are used.  That makes the
    # Statistics tab a useful checklist while the user is building a review
    # vocabulary, instead of silently hiding every zero-count custom reason.
    saved_reason_rows = db.rows("SELECT reason FROM rejection_reasons ORDER BY created_at DESC")
    saved_reasons = [str(item.get("reason") or "").strip() for item in saved_reason_rows]
    listed_reasons: list[dict] = []
    for reason in saved_reasons:
        if reason:
            listed_reasons.append({"reason": reason, "count": reasons.pop(reason, 0), "saved": True})
    listed_reasons.extend({"reason": reason, "count": count, "saved": False} for reason, count in reasons.most_common(10))
    score_comparison = {
        key: {
            "accepted": average(values["accepted"]),
            "rejected": average(values["rejected"]),
            "accepted_count": len(values["accepted"]),
            "rejected_count": len(values["rejected"]),
        }
        for key, values in score_values.items()
    }
    calibration = []
    for key, values in score_comparison.items():
        accepted, rejected = values["accepted"], values["rejected"]
        sample_size = min(values["accepted_count"], values["rejected_count"])
        delta = None if accepted is None or rejected is None else accepted - rejected
        if sample_size < 5:
            verdict = "collecting_data"
        elif delta is not None and delta >= 10:
            verdict = "strong_signal"
        elif delta is not None and delta >= 4:
            verdict = "weak_signal"
        else:
            verdict = "needs_tuning"
        calibration.append({"score": key, "delta": delta, "sample_size": sample_size, "verdict": verdict})

    return {
        "overview": {"total": len(rows), "reviewed": reviewed, "accepted": decisions["accepted"], "rejected": decisions["rejected"], "approval_rate": round(decisions["accepted"] * 100 / reviewed) if reviewed else None, "published": len(published_rows)},
        "published_performance": {
            "count": len(published_rows),
            "views": sum(int(row.get("views") or 0) for row in published_rows),
            "median_watch_percent": round(statistics.median([float(row.get("average_watch_percent") or 0) for row in published_rows if float(row.get("average_watch_percent") or 0) > 0]), 1) if any(float(row.get("average_watch_percent") or 0) > 0 for row in published_rows) else None,
            "shares": sum(int(row.get("shares") or 0) for row in published_rows),
            "comments": sum(int(row.get("comments") or 0) for row in published_rows),
        },
        "rejection_reasons": listed_reasons,
        "tags": [
            {"tag": tag, "accepted": counts["accepted"], "rejected": counts["rejected"], "unrated": counts["unrated"], "total": sum(counts.values()), "approval_rate": round(counts["accepted"] * 100 / (counts["accepted"] + counts["rejected"])) if counts["accepted"] + counts["rejected"] else None}
            for tag, counts in sorted(tags.items(), key=lambda item: (sum(item[1].values()), item[0]), reverse=True)[:14]
        ],
        "analysis_modes": [{"mode": mode, "accepted": counts["accepted"], "rejected": counts["rejected"], "unrated": counts["unrated"]} for mode, counts in sorted(modes.items())],
        "score_comparison": score_comparison,
        "calibration": calibration,
        "reading_flags": dict(reading),
    }


@app.get("/api/diagnostics/report")
def diagnostic_report():
    """Download a support report without user media, transcripts or source links."""
    return PlainTextResponse(
        diagnostics.build_report(runtime_status(), __version__),
        headers={"Content-Disposition": "attachment; filename=clipfinder-diagnostics.txt"},
    )


@app.get("/api/update-status")
def app_update_status():
    return {**update_status(), "automatic_install_available": automatic_updates_available()}


@app.post("/api/updates/download")
def download_app_update():
    try:
        return start_update_download()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/updates/downloads/{job_id}")
def app_update_download_status(job_id: str):
    job = update_download_status(job_id)
    if not job:
        raise HTTPException(404, "Update download was not found.")
    return job


@app.post("/api/updates/downloads/{job_id}/install")
def install_app_update(job_id: str):
    if db.row("SELECT id FROM jobs WHERE state IN ('queued', 'running') LIMIT 1") or db.row(
        "SELECT id FROM reference_imports WHERE state IN ('queued', 'running') LIMIT 1"
    ):
        raise HTTPException(409, "Wait for active analysis/import jobs to finish or cancel them before installing an update.")
    try:
        install_downloaded_update(job_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"state": "installing"}


_ANALYSIS_AUDIO_PAYLOAD_KEYS = (
    "mode", "single_track", "microphone_track", "all_sounds_track",
    "game_track", "use_all_sounds", "use_game",
)


def _analysis_job_payload(**extra) -> dict:
    """Freeze analysis inputs which could otherwise change while queued."""
    current = db.row("SELECT * FROM analysis_audio_defaults WHERE id=1") or {}
    snapshot = {key: current[key] for key in _ANALYSIS_AUDIO_PAYLOAD_KEYS if key in current}
    return {**extra, "analysis_audio": snapshot}


def _store_uploaded_video(source, partial_destination: Path, destination: Path) -> None:
    """Persist a potentially multi-GB upload outside the async event loop."""
    with partial_destination.open("wb") as output:
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    # Windows Defender and third-party antivirus tools can briefly open a
    # freshly closed upload. Retry only this atomic rename.
    for attempt in range(6):
        try:
            os.replace(partial_destination, destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


@app.post("/api/videos", status_code=201)
async def upload_video(file: UploadFile = File(...), analysis_mode: str = Form("default")):
    if not file.filename or Path(file.filename).suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm"}:
        raise HTTPException(400, "Add MP4, MKV, MOV or WebM video file.")
    if analysis_mode not in {"fast", "default", "extended"}:
        raise HTTPException(400, "Choose Fast, Default or Extended analysis.")
    video_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    destination = settings.incoming_dir / f"{video_id}{Path(file.filename).suffix.lower()}"
    partial_destination = destination.with_name(f"{destination.name}.upload.part")
    try:
        await run_in_threadpool(_store_uploaded_video, file.file, partial_destination, destination)
    except OSError as exc:
        partial_destination.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise HTTPException(507, f"Could not store the upload: {exc}. Check free disk space and write access to the ClipFinder data folder.") from exc
    finally:
        await file.close()
    timestamp = db.now()
    job_payload = _analysis_job_payload(analysis_mode=analysis_mode)
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO videos (id, original_name, path, analysis_mode, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (video_id, file.filename, str(destination), analysis_mode, timestamp, timestamp),
            )
            job = job_queue.enqueue(
                con, video_id=video_id, kind="analysis", job_id=job_id,
                payload=job_payload, message="Queued", now=timestamp,
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    _wake_durable_worker()
    return {"video_id": video_id, "job_id": job["id"]}


@app.post("/api/videos/from-url", status_code=201)
def import_remote_video(body: RemoteVideoCreate):
    source_url = _supported_remote_url(body.source_url)
    video_id, job_id, timestamp = str(uuid.uuid4()), str(uuid.uuid4()), db.now()
    job_payload = _analysis_job_payload(source_url=source_url)
    placeholder = settings.incoming_dir / f"{video_id}.download"
    with db.connection() as con:
        con.execute(
            "INSERT INTO videos (id, original_name, path, source_url, analysis_mode, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (video_id, "YouTube/Twitch download", str(placeholder), source_url, body.analysis_mode, timestamp, timestamp),
        )
        job = job_queue.enqueue(
            con, video_id=video_id, kind="remote_import", job_id=job_id,
            payload=job_payload, message="Queued remote download", now=timestamp,
        )
    _wake_durable_worker()
    return {"video_id": video_id, "job_id": job["id"]}


@app.post("/api/videos/{video_id}/analyse", status_code=202)
def restart_analysis(video_id: str):
    video = db.row("SELECT * FROM videos WHERE id=?", (video_id,))
    if not video:
        not_found("Video not found")
    if video.get("source_removed"):
        raise HTTPException(409, "The source video was removed to save disk space. Its analysis archive remains available, but it cannot be reanalysed.")
    if not Path(video["path"]).is_file() and not video.get("source_url"):
        raise HTTPException(400, "The original video file is no longer available.")
    job_id, timestamp = str(uuid.uuid4()), db.now()
    job_payload = _analysis_job_payload(reanalyze=True)
    with db.connection() as con:
        kind = "analysis" if Path(video["path"]).is_file() else "remote_import"
        job = job_queue.enqueue(
            con, video_id=video_id, kind=kind, job_id=job_id,
            payload=job_payload, message="Queued again", now=timestamp,
        )
        if str(job["id"]) == job_id:
            con.execute(
                "UPDATE videos SET status='queued', error_message=NULL, analysis_seconds=0, updated_at=? WHERE id=?",
                (timestamp, video_id),
            )
    _wake_durable_worker()
    return {"job_id": job["id"]}


@app.get("/api/videos")
def videos():
    items = db.rows(
        """SELECT v.*, j.id AS job_id, j.kind AS job_kind, j.progress, j.message,
                  j.state AS job_state, j.pause_requested
           FROM videos v LEFT JOIN jobs j ON j.id=(
               SELECT latest.id FROM jobs latest
               WHERE latest.video_id=v.id
               ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1
           )
           ORDER BY v.created_at DESC"""
    )
    for item in items:
        source = Path(item["path"])
        item["size_bytes"] = source.stat().st_size if source.is_file() else 0
        item["source_removed"] = bool(item.get("source_removed"))
        item["source_size_bytes"] = int(item.get("source_size_bytes") or item["size_bytes"] or 0)
        estimate, samples = estimate_analysis_duration(item)
        item["estimated_analysis_seconds"] = estimate
        item["estimate_sample_count"] = samples
    return items


@app.get("/api/storage")
def storage_usage():
    source_paths = {Path(item["path"]) for item in db.rows("SELECT path FROM videos")}
    video_bytes = sum(path.stat().st_size for path in source_paths if path.is_file())
    export_files = [path for path in settings.exports_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}]
    clip_bytes = sum(path.stat().st_size for path in export_files)
    review_audio_files = [path for path in settings.review_audio_dir.rglob("*.mp3") if path.is_file()]
    review_audio_bytes = sum(path.stat().st_size for path in review_audio_files)
    return {
        "video_bytes": video_bytes,
        "clip_bytes": clip_bytes,
        "video_count": sum(1 for path in source_paths if path.is_file()),
        "clip_count": len(export_files),
        "review_audio_bytes": review_audio_bytes,
        "review_audio_count": len(review_audio_files),
    }


def _segments_requiring_review_audio(video_id: str) -> list[dict]:
    """Return immutable snapshots for every moment carrying human evidence."""
    segments = db.rows(
        """SELECT s.id, s.start_seconds, s.end_seconds, s.word_timestamps,
                  s.archive_audio_path, s.archive_audio_track, s.revision_number,
                  r.reviewed_revision_id, r.rating, r.review_reason,
                  r.censor_profanity, r.remove_pauses,
                  r.archive_audio_path AS review_archive_audio_path,
                  r.archive_audio_track AS review_archive_audio_track
           FROM segments s
           JOIN segment_reviews r ON r.segment_id=s.id
           WHERE s.video_id=? AND (
               r.rating IN ('accepted', 'rejected')
               OR r.review_reason<>'' OR r.censor_profanity=1 OR r.remove_pauses=1
               OR EXISTS(SELECT 1 FROM segment_tag_reviews tr WHERE tr.segment_id=s.id)
               OR EXISTS(SELECT 1 FROM tag_feedback tf WHERE tf.segment_id=s.id)
               OR EXISTS(SELECT 1 FROM collection_examples ce WHERE ce.segment_id=s.id)
               OR EXISTS(SELECT 1 FROM preference_feedback pf WHERE pf.segment_id=s.id)
               OR EXISTS(
                   SELECT 1 FROM segment_revisions mr
                   WHERE mr.segment_id=s.id
                     AND mr.revision_kind NOT IN ('analysis', 'reanalysis', 'legacy')
               )
           )
           ORDER BY s.start_seconds, s.id""",
        (video_id,),
    )
    snapshots: list[dict] = []
    for item in segments:
        revision = None
        if item.get("rating") in {"accepted", "rejected"} and item.get("reviewed_revision_id"):
            revision = db.row("SELECT * FROM segment_revisions WHERE id=?", (item["reviewed_revision_id"],))
        if not revision:
            revision = db.row(
                """SELECT sr.* FROM segment_tag_reviews tr
                   JOIN segment_revisions sr ON sr.id=tr.reviewed_revision_id
                   WHERE tr.segment_id=? ORDER BY tr.updated_at DESC LIMIT 1""",
                (item["id"],),
            )
        if not revision:
            revision = db.row(
                """SELECT * FROM segment_revisions
                   WHERE segment_id=? AND revision_kind NOT IN ('analysis', 'reanalysis', 'legacy')
                   ORDER BY revision_number DESC LIMIT 1""",
                (item["id"],),
            )
        if not revision:
            revision = db.row(
                """SELECT sr.* FROM collection_examples ce
                   JOIN segment_revisions sr
                     ON sr.segment_id=ce.segment_id AND sr.revision_number=ce.revision_number
                   WHERE ce.segment_id=? ORDER BY ce.created_at DESC LIMIT 1""",
                (item["id"],),
            )
        if not revision and item.get("reviewed_revision_id"):
            revision = db.row("SELECT * FROM segment_revisions WHERE id=?", (item["reviewed_revision_id"],))
        if not revision:
            revision = db.row(
                """SELECT * FROM segment_revisions WHERE segment_id=?
                   ORDER BY is_current DESC, revision_number DESC LIMIT 1""",
                (item["id"],),
            )
        snapshot = dict(item)
        if revision:
            snapshot.update({
                "revision_id": revision["id"],
                "revision_number": int(revision["revision_number"]),
                "start_seconds": float(revision["start_seconds"]),
                "end_seconds": float(revision["end_seconds"]),
                "payload_json": revision["payload_json"],
            })
        else:
            snapshot["revision_id"] = f"segment:{item['id']}:{item.get('revision_number') or 1}"
            snapshot["payload_json"] = json.dumps(
                {"word_timestamps": item.get("word_timestamps") or "[]"},
                ensure_ascii=False,
            )
        snapshot["archive_audio_path"] = (
            item.get("review_archive_audio_path") or item.get("archive_audio_path") or ""
        )
        snapshot["archive_audio_track"] = int(
            item.get("review_archive_audio_track") or item.get("archive_audio_track") or 1
        )
        snapshots.append(snapshot)
    return snapshots


def _review_audio_archive_path(segment: dict, audio_track: int) -> Path:
    """Name an archive by the exact revision and playback transformation."""
    raw_id = str(segment["id"])
    readable = re.sub(r"[^A-Za-z0-9_-]+", "-", raw_id).strip("-")[:40] or "moment"
    identity = "|".join((
        raw_id,
        str(segment.get("revision_id") or segment.get("revision_number") or 1),
        f"{float(segment['start_seconds']):.6f}",
        f"{float(segment['end_seconds']):.6f}",
        str(audio_track),
        str(int(bool(segment.get("remove_pauses")))),
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    settings.review_audio_dir.mkdir(parents=True, exist_ok=True)
    return settings.review_audio_dir / f"{readable}-{digest}.mp3"


def _record_review_audio_archives(archives: list[tuple[dict, Path, int]]) -> None:
    """Atomically point durable reviews at fully written archive files."""
    stale_paths: set[Path] = set()
    timestamp = db.now()
    with db.connection() as con:
        for segment, archive_path, archive_track in archives:
            for old_value in (
                segment.get("archive_audio_path"),
                segment.get("review_archive_audio_path"),
            ):
                if old_value and Path(old_value) != archive_path:
                    stale_paths.add(Path(old_value))
            con.execute(
                "UPDATE segments SET archive_audio_path=?, archive_audio_track=? WHERE id=?",
                (str(archive_path), archive_track, segment["id"]),
            )
            con.execute(
                """UPDATE segment_reviews
                   SET archive_audio_path=?, archive_audio_track=?, updated_at=?
                   WHERE segment_id=?""",
                (str(archive_path), archive_track, timestamp, segment["id"]),
            )
    review_root = settings.review_audio_dir.resolve()
    for stale_path in stale_paths:
        try:
            resolved = stale_path.resolve()
            resolved.relative_to(review_root)
            resolved.unlink(missing_ok=True)
        except (OSError, ValueError):
            # A stale preview is not allowed to make source removal fail after
            # the new, referenced archive has been committed successfully.
            pass


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str):
    """Remove only the large source file while retaining analytical history.

    Ratings, tag feedback, vectors, chat analysis and reviewed-clip audio are
    deliberately preserved. This keeps local learning data usable after disk
    cleanup and avoids the old destructive cascade through ``segments``.
    """
    video = db.row("SELECT * FROM videos WHERE id=?", (video_id,))
    if not video:
        not_found("Video not found")
    if video["status"] in {"queued", "processing"}:
        raise HTTPException(409, "Wait for the current analysis to finish before deleting this recording.")
    if video.get("source_removed"):
        return {"ok": True, "already_removed": True, "archived_segments": 0}
    source = Path(video["path"])
    source_size_bytes = source.stat().st_size if source.is_file() else int(video.get("source_size_bytes") or 0)
    reviewed_segments = _segments_requiring_review_audio(video_id)
    archived: list[tuple[dict, Path, int]] = []
    if source.exists():
        try:
            source.resolve().relative_to(settings.incoming_dir.resolve())
        except ValueError as exc:
            raise HTTPException(400, "This recording is outside ClipFinder's managed incoming folder and will not be deleted.") from exc
        try:
            available_tracks = audio_track_count(source)
        except MediaError as exc:
            raise HTTPException(500, f"Could not inspect source audio before removal: {exc}") from exc
        if reviewed_segments and available_tracks < 1:
            raise HTTPException(
                409,
                "The source has no audio track, so ClipFinder cannot preserve audio for reviewed moments.",
            )
        for segment in reviewed_segments:
            archive_track = max(1, min(available_tracks, int(segment.get("archive_audio_track") or 1)))
            destination = _review_audio_archive_path(segment, archive_track)
            temporary = destination.with_name(
                f".{destination.stem}.{uuid.uuid4().hex}.tmp.mp3"
            )
            try:
                try:
                    reviewed_machine = json.loads(segment.get("payload_json") or "{}")
                except (TypeError, ValueError):
                    reviewed_machine = {}
                words = reviewed_machine.get("word_timestamps", []) if isinstance(reviewed_machine, dict) else []
                if isinstance(words, str):
                    words = json.loads(words or "[]")
                pause_ranges = (
                    pause_trim_ranges(
                        words,
                        float(segment["end_seconds"]) - float(segment["start_seconds"]),
                        float(segment["start_seconds"]),
                    )
                    if segment.get("remove_pauses") else None
                )
                export_audio_preview(
                    source, temporary, float(segment["start_seconds"]),
                    float(segment["end_seconds"]), archive_track, pause_ranges,
                )
                os.replace(temporary, destination)
                archived.append((segment, destination, archive_track))
            except (MediaError, OSError) as exc:
                temporary.unlink(missing_ok=True)
                raise HTTPException(500, f"Could not archive reviewed clip audio before source removal: {exc}") from exc

        # Commit the exact audio snapshots before removing the only source. If
        # the process is interrupted between these operations, a retry can use
        # the already recorded archive rather than silently losing review data.
        _record_review_audio_archives(archived)
        try:
            source.unlink()
        except OSError as exc:
            raise HTTPException(500, f"Could not delete the source recording: {exc}") from exc
    else:
        # Handles recovery after an interruption that happened just after the
        # file was unlinked but after archive paths had already been committed.
        missing_archives: list[str] = []
        for segment in reviewed_segments:
            existing = Path(segment.get("archive_audio_path") or "")
            if existing.is_file():
                archived.append((segment, existing, max(1, int(segment.get("archive_audio_track") or 1))))
            else:
                missing_archives.append(str(segment["id"]))
        if missing_archives:
            raise HTTPException(
                409,
                "The source recording is currently unavailable and reviewed moments do not all have "
                "archived audio. Restore the source file and try again; ClipFinder has not marked it "
                "as removed.",
            )

    with db.connection() as con:
        con.execute(
            "UPDATE videos SET source_removed=1, source_removed_at=?, source_size_bytes=?, updated_at=? WHERE id=?",
            (db.now(), source_size_bytes, db.now(), video_id),
        )
    for item in db.rows("SELECT id FROM segments WHERE video_id=?", (video_id,)):
        for preview in settings.previews_dir.glob(f"{item['id']}-*"):
            preview.unlink(missing_ok=True)
    try:
        cache_result = PipelineCache(settings.pipeline_cache_dir).invalidate_video(video_id)
    except Exception as exc:
        # The recording and its durable review history are already safe. Cache
        # reclamation is best-effort and must not turn source removal into an
        # apparent failure for the user.
        diagnostics.log_failure(f"Pipeline cache invalidation skipped: video_id={video_id}", exc)
    else:
        if cache_result.reclaimed_bytes:
            diagnostics.logger().info(
                "Pipeline cache invalidated: video_id=%s reclaimed_bytes=%s",
                video_id, cache_result.reclaimed_bytes,
            )
    return {
        "ok": True,
        "archived_segments": sum(1 for _segment, path, _track in archived if path.is_file()),
        "source_size_bytes": source_size_bytes,
    }


@app.get("/api/videos/{video_id}/stream")
def stream_video(video_id: str):
    video = db.row("SELECT path, original_name FROM videos WHERE id=?", (video_id,))
    if not video:
        not_found("Video not found")
    source = Path(video["path"])
    if not source.is_file():
        raise HTTPException(404, "Original video file is no longer available.")
    return FileResponse(source)


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    result = db.row("SELECT * FROM jobs WHERE id=?", (job_id,))
    return result or not_found()


@app.post("/api/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str):
    with db.connection() as con:
        result = job_queue.request_cancel(con, job_id)
    if not result:
        not_found("Job not found")
    _sync_video_job(result)
    _wake_durable_worker()
    return result


@app.post("/api/jobs/{job_id}/pause", status_code=202)
def pause_job(job_id: str):
    with db.connection() as con:
        result = job_queue.request_pause(con, job_id)
    if not result:
        not_found("Job not found")
    _sync_video_job(result)
    return result


@app.post("/api/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str):
    with db.connection() as con:
        result = job_queue.resume(con, job_id)
    if not result:
        not_found("Job not found")
    _sync_video_job(result)
    _wake_durable_worker()
    return result


@app.post("/api/reference-imports/{import_id}/cancel", status_code=202)
def cancel_reference_import(import_id: str):
    with db.connection() as con:
        result = reference_queue.request_cancel(con, import_id)
    if not result:
        not_found("Reference import not found")
    _wake_durable_worker()
    return result


@app.get("/api/videos/{video_id}/segments")
def video_segments(video_id: str, q: str = "", rating: str = "", tag: str = "", hide_reading: bool = False, show_duplicates: bool = False, sort: str = "suggested_desc"):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    clauses, parameters = ["s.video_id=?", "s.lifecycle_state='current'"], [video_id]
    if q.strip():
        clauses.append("s.transcript LIKE ?")
        parameters.append(f"%{q.strip()}%")
    if rating in {"unrated", "accepted", "rejected"}:
        clauses.append("r.rating=?")
        parameters.append(rating)
    if hide_reading:
        # ``reading`` was a legacy duplicate tag.  The likelihood field is the
        # authoritative signal and works for both older and newer analyses.
        clauses.append("s.reading_likelihood < ?")
        parameters.append(0.48)
    scoped_clauses = " AND ".join(clauses)
    items = db.rows(
        f"""SELECT s.*, r.rating, r.review_reason, v.source_removed
            FROM segments s
            JOIN videos v ON v.id=s.video_id
            JOIN segment_reviews r ON r.segment_id=s.id
            WHERE {scoped_clauses} AND s.embedding IS NOT NULL""",
        tuple(parameters),
    )
    requested_tag = canonical_tag(tag) if tag.strip() else None
    if requested_tag:
        items = [
            item for item in items
            if requested_tag in canonicalize_tags(json.loads(item.get("tags") or "[]"))
        ]
    ranked = suppress_duplicate_groups(score_candidates(items, profile=active_profile()), keep_alternatives=show_duplicates)
    if requested_tag != "format: czytanie":
        ranked = [item for item in ranked if not is_disallowed_reading(item)]
    ranked = filter_profanity(ranked)
    sort_fields = {
        "suggested_desc": ("ranking_score", True), "suggested_asc": ("ranking_score", False),
        "quality_desc": ("quality_score", True), "quality_asc": ("quality_score", False),
        "short_potential_desc": ("short_potential_score", True), "short_potential_asc": ("short_potential_score", False),
        "self_contained_desc": ("self_contained_score", True), "self_contained_asc": ("self_contained_score", False),
    }
    field, descending = sort_fields.get(sort, sort_fields["suggested_desc"])
    ranked.sort(key=lambda item: float(item.get(field) or 0), reverse=descending)
    return db.serialize_segments(ranked)


@app.get("/api/videos/{video_id}/chat")
def video_chat_summary(video_id: str):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    return chat_summary(video_id)


_MAX_CHAT_UPLOAD_BYTES = 50 * 1024 * 1024


async def _read_upload_limited(upload: UploadFile, limit: int = _MAX_CHAT_UPLOAD_BYTES) -> bytes:
    """Read at most ``limit + 1`` bytes so oversized chat files stay bounded."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = await upload.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        raise HTTPException(400, f"The chat file is too large (maximum {limit // (1024 * 1024)} MB).")
    return b"".join(chunks)


@app.post("/api/videos/{video_id}/chat")
async def upload_video_chat(video_id: str, chat_file: UploadFile = File(...), delay_seconds: float = Form(6)):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    if not 0 <= delay_seconds <= 60:
        raise HTTPException(400, "Chat delay must be between 0 and 60 seconds.")
    try:
        raw = await _read_upload_limited(chat_file)
        if not raw:
            raise HTTPException(400, "The chat file is empty.")
        try:
            return await run_in_threadpool(
                import_chat, video_id, chat_file.filename or "chat.txt", raw, delay_seconds,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        await chat_file.close()


@app.patch("/api/videos/{video_id}/chat")
def update_video_chat_delay(video_id: str, body: ChatDelayUpdate):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    try:
        return update_chat_delay(video_id, body.delay_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/segments/{segment_id}")
def rate_segment(segment_id: str, body: RatingUpdate):
    profile = active_profile()
    try:
        result = set_review(segment_id, body.rating, body.review_reason, profile)
    except ValueError as exc:
        if str(exc) == "Segment not found.":
            not_found("Segment not found")
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **result}


@app.patch("/api/segments/{segment_id}/timing")
def update_segment_timing(segment_id: str, body: SegmentTimingUpdate):
    segment = db.row(
        """SELECT s.*, v.duration_seconds, v.path, v.transcript_audio_track,
                  v.analysis_mode
           FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?""",
        (segment_id,),
    )
    if not segment:
        not_found("Segment not found")
    if body.end_seconds - body.start_seconds < 0.5:
        raise HTTPException(400, "The clip must be at least 0.5 seconds long.")
    if segment["duration_seconds"] and body.end_seconds > segment["duration_seconds"]:
        raise HTTPException(400, "The end time is outside the recording.")
    try:
        transcript, words = transcribe_clip_range(Path(segment["path"]), body.start_seconds, body.end_seconds, int(segment.get("transcript_audio_track") or 1))
        vector = embed_texts([transcript or "bez wypowiedzi"])[0]
        keywords = [word.strip(".,!?;:").lower() for word in transcript.split() if len(word.strip(".,!?;:")) >= 6][:12]
        tags = infer_tags(transcript, vector)
        before, after = _segment_context(
            segment["video_id"], segment_id, body.start_seconds, body.end_seconds,
        )
        state = {
            **segment,
            "start_seconds": body.start_seconds,
            "end_seconds": body.end_seconds,
            "transcript": transcript,
            "word_timestamps": words,
            "tags": tags,
            "context_before": before,
            "context_after": after,
            "analysis_mode": segment.get("analysis_mode") or "default",
            # Range-dependent evidence is invalid until the next full media
            # analysis. Never keep evidence sampled for the previous range.
            "vision_score": 0,
            "visual_reading_likelihood": 0.0,
            "boundary_signals": [],
            "audio_event_score": 0,
            "game_reaction_score": 0,
            "voice_expression_score": 0,
            "chat_reaction_score": 0,
            "chat_joy_score": 0,
            "chat_question_match_score": 0,
            "chat_question_text": "",
        }
        derived = recompute_segment_features(
            state,
            {
                "transcript", "start_seconds", "end_seconds",
                "word_timestamps", "tags", "context_before", "context_after",
            },
        ).updates
    except Exception as exc:
        raise HTTPException(500, f"Unable to update captions for the new range: {exc}") from exc
    record_manual_revision_with_updates(
        segment_id,
        _encode_feature_updates({
            **derived,
            "start_seconds": body.start_seconds,
            "end_seconds": body.end_seconds,
            "transcript": transcript,
            "keywords": keywords,
            "tags": derived.get("tags", tags),
            "word_timestamps": words,
            "embedding": vector,
            "context_before": before,
            "context_after": after,
            "boundary_signals": [],
            "vision_score": 0,
            "visual_reading_likelihood": 0.0,
            "audio_event_score": 0,
            "game_reaction_score": 0,
            "voice_expression_score": 0,
            "chat_reaction_score": 0,
            "chat_joy_score": 0,
            "chat_message_count": 0,
            "chat_unique_authors": 0,
            "chat_surge": 0.0,
            "chat_messages": [],
            "chat_question_match_score": 0,
            "chat_question_text": "",
            "duplicate_group": "",
        }),
        "timing_edit",
    )
    _refresh_neighbour_contexts(
        segment["video_id"],
        segment_id,
        min(float(segment["start_seconds"]), float(body.start_seconds)),
        max(float(segment["end_seconds"]), float(body.end_seconds)),
    )
    apply_chat_reactions(segment["video_id"], [segment_id])
    _refresh_duplicate_groups(segment["video_id"])
    for preview in settings.previews_dir.glob(f"{segment_id}-*"):
        preview.unlink(missing_ok=True)
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.post("/api/segments/{segment_id}/composer-captions")
def refresh_composer_captions(segment_id: str, body: ComposerCaptionRefresh):
    """Transcribe a Composer-only range without changing saved clip analysis."""
    segment = db.row(
        """SELECT s.id, v.path, v.duration_seconds, v.transcript_audio_track
           FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?""",
        (segment_id,),
    )
    if not segment:
        not_found("Segment not found")
    if body.end_seconds - body.start_seconds < 0.5:
        raise HTTPException(400, "The clip must be at least 0.5 seconds long.")
    duration = float(segment.get("duration_seconds") or 0)
    if duration and body.end_seconds > duration:
        raise HTTPException(400, "The end time is outside the recording.")
    source = Path(segment["path"])
    if not source.is_file():
        raise HTTPException(409, "The source video was removed, so captions cannot be refreshed.")
    try:
        transcript, words = transcribe_clip_range(
            source,
            float(body.start_seconds),
            float(body.end_seconds),
            int(segment.get("transcript_audio_track") or 1),
        )
    except Exception as exc:
        raise HTTPException(500, f"Unable to refresh captions: {exc}") from exc
    return {
        "transcript": transcript,
        "word_timestamps": words,
        "start_seconds": float(body.start_seconds),
        "end_seconds": float(body.end_seconds),
    }


@app.patch("/api/segments/{segment_id}/transcript")
def update_segment_transcript(segment_id: str, body: SegmentTranscriptUpdate):
    segment = db.row(
        """SELECT s.*, v.analysis_mode FROM segments s
           JOIN videos v ON v.id=s.video_id WHERE s.id=?""",
        (segment_id,),
    )
    if not segment:
        not_found("Segment not found")
    transcript = " ".join(body.transcript.split())
    vector = embed_texts([transcript or "bez wypowiedzi"])[0]
    keywords = [word.strip(".,!?;:").lower() for word in transcript.split() if len(word.strip(".,!?;:")) >= 6][:12]
    tags = infer_tags(transcript, vector)
    words = approximate_word_timestamps(transcript, segment["start_seconds"], segment["end_seconds"])
    state = {
        **segment,
        "transcript": transcript,
        "word_timestamps": words,
        "tags": tags,
        "analysis_mode": segment.get("analysis_mode") or "default",
        # A former Q&A match describes the old wording. Extended scores are
        # rebuilt by the graph from the corrected transcript and context.
        "chat_question_match_score": 0,
        "chat_question_text": "",
    }
    derived = recompute_segment_features(
        state,
        {"transcript", "word_timestamps", "tags"},
    ).updates
    record_manual_revision_with_updates(
        segment_id,
        _encode_feature_updates({
            **derived,
            "transcript": transcript,
            "keywords": keywords,
            "tags": derived.get("tags", tags),
            "word_timestamps": words,
            "embedding": vector,
            "chat_question_match_score": 0,
            "chat_question_text": "",
            "duplicate_group": "",
        }),
        "transcript_edit",
    )
    _refresh_neighbour_contexts(
        segment["video_id"],
        segment_id,
        float(segment["start_seconds"]),
        float(segment["end_seconds"]),
    )
    apply_chat_reactions(segment["video_id"], [segment_id])
    _refresh_duplicate_groups(segment["video_id"])
    for preview in settings.previews_dir.glob(f"{segment_id}-*"):
        preview.unlink(missing_ok=True)
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/censor")
def update_segment_censor(segment_id: str, body: SegmentCensorUpdate):
    with db.connection() as con:
        if not con.execute("SELECT id FROM segments WHERE id=?", (segment_id,)).fetchone():
            not_found("Segment not found")
        value = int(body.censor_profanity)
        con.execute(
            "UPDATE segment_reviews SET censor_profanity=?, updated_at=? WHERE segment_id=?",
            (value, db.now(), segment_id),
        )
        con.execute("UPDATE segments SET censor_profanity=? WHERE id=?", (value, segment_id))
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/pause-trim")
def update_segment_pause_trim(segment_id: str, body: SegmentPauseTrimUpdate):
    with db.connection() as con:
        segment = con.execute(
            "SELECT s.id, v.path FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?",
            (segment_id,),
        ).fetchone()
        if not segment:
            not_found("Segment not found")
        if not Path(segment["path"]).is_file():
            raise HTTPException(409, "Pause removal cannot be changed after the source recording is removed. The archived audio keeps the setting used during removal.")
        value = int(body.remove_pauses)
        con.execute(
            "UPDATE segment_reviews SET remove_pauses=?, updated_at=? WHERE segment_id=?",
            (value, db.now(), segment_id),
        )
        con.execute("UPDATE segments SET remove_pauses=? WHERE id=?", (value, segment_id))
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/tag-feedback")
def update_segment_tag_feedback(segment_id: str, body: TagFeedbackUpdate):
    try:
        set_tag_verdict(segment_id, body.tag, body.verdict)
    except ValueError as exc:
        if str(exc) == "Segment not found.":
            not_found("Segment not found")
        raise HTTPException(400, str(exc)) from exc
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.get("/api/segments/{segment_id}/publication-feedback")
def get_publication_feedback(segment_id: str):
    if not db.row("SELECT id FROM segments WHERE id=?", (segment_id,)):
        not_found("Segment not found")
    saved = db.row("SELECT platform, published_url, views, average_watch_percent, shares, comments, updated_at FROM publication_feedback WHERE segment_id=?", (segment_id,))
    return saved or {
        "platform": "", "published_url": "", "views": 0,
        "average_watch_percent": 0, "shares": 0, "comments": 0,
        "updated_at": None,
    }


@app.put("/api/segments/{segment_id}/publication-feedback")
def update_publication_feedback(segment_id: str, body: PublicationFeedbackUpdate):
    if not db.row("SELECT id FROM segments WHERE id=?", (segment_id,)):
        not_found("Segment not found")
    platform = " ".join(body.platform.split())[:32]
    published_url = body.published_url.strip()
    with db.connection() as con:
        con.execute(
            """INSERT INTO publication_feedback
               (segment_id, platform, published_url, views, average_watch_percent, shares, comments, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(segment_id) DO UPDATE SET
                   platform=excluded.platform, published_url=excluded.published_url,
                   views=excluded.views, average_watch_percent=excluded.average_watch_percent,
                   shares=excluded.shares, comments=excluded.comments, updated_at=excluded.updated_at""",
            (segment_id, platform, published_url, body.views, body.average_watch_percent, body.shares, body.comments, db.now()),
        )
    return get_publication_feedback(segment_id)


@app.post("/api/segments/{segment_id}/export")
def export_segment(segment_id: str, body: ExportRequest):
    return _export_segment(segment_id, body.lead_in_seconds, body.lead_out_seconds, body.captions_preset, body.caption_position, body.base_color, body.active_color, body.layout, body.audio_track, body.filename, body.outline_enabled, body.outline_color, body.glow_enabled, body.opacity, body.font_family, body.max_lines, body.camera_x, body.camera_y, body.camera_width, body.camera_height, body.game_x, body.game_y, body.game_width, body.game_height, body.censor_profanity, body.remove_pauses, body.microphone_enhancement, body.normalize_loudness, body.volume_gain_db, body.start_seconds, body.end_seconds, body.hook_seconds, body.caption_text, body.caption_word_timestamps)


@app.get("/api/segments/{segment_id}/export")
def download_segment(segment_id: str, lead_in_seconds: float = 0, lead_out_seconds: float = 0, captions_preset: str = "none", caption_position: str = "bottom", base_color: str = "#FFFFFF", active_color: str = "#FFFF00", layout: str = "original", audio_track: int = 1, filename: str = "", outline_enabled: bool = True, outline_color: str = "#000000", glow_enabled: bool = False, opacity: int = 100, font_family: str = "Inter", max_lines: int = 2, camera_x: float | None = None, camera_y: float | None = None, camera_width: float | None = None, camera_height: float | None = None, game_x: float | None = None, game_y: float | None = None, game_width: float | None = None, game_height: float | None = None, censor_profanity: bool | None = None, remove_pauses: bool | None = None, microphone_enhancement: bool = False, normalize_loudness: bool = False, volume_gain_db: float = 0, start_seconds: float | None = None, end_seconds: float | None = None, hook_seconds: float = 0):
    return _export_segment(segment_id, lead_in_seconds, lead_out_seconds, captions_preset, caption_position, base_color, active_color, layout, audio_track, filename, outline_enabled, outline_color, glow_enabled, opacity, font_family, max_lines, camera_x, camera_y, camera_width, camera_height, game_x, game_y, game_width, game_height, censor_profanity, remove_pauses, microphone_enhancement, normalize_loudness, volume_gain_db, start_seconds, end_seconds, hook_seconds)


def _safe_export_name(value: str, fallback: str) -> str:
    name = Path(value.strip()).stem if value.strip() else fallback
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(". ")
    return name[:120] or fallback


def _available_export_path(stem: str) -> Path:
    candidate = settings.exports_dir / f"{stem}.mp4"
    counter = 2
    while candidate.exists():
        candidate = settings.exports_dir / f"{stem}_{counter}.mp4"
        counter += 1
    return candidate


def _export_segment(*args, **kwargs):
    # Filename selection, ASS generation and ffmpeg output must be one atomic
    # operation. Two simultaneous clicks previously selected the same output
    # path and reused the same temporary subtitle file.
    with _media_output_lock:
        return _export_segment_locked(*args, **kwargs)


def _export_segment_locked(segment_id: str, lead_in_seconds: float, lead_out_seconds: float, captions_preset: str = "none", caption_position: str = "bottom", base_color: str = "#FFFFFF", active_color: str = "#FFFF00", layout: str = "original", audio_track: int = 1, filename: str = "", outline_enabled: bool = True, outline_color: str = "#000000", glow_enabled: bool = False, opacity: int = 100, font_family: str = "Inter", max_lines: int = 2, camera_x: float | None = None, camera_y: float | None = None, camera_width: float | None = None, camera_height: float | None = None, game_x: float | None = None, game_y: float | None = None, game_width: float | None = None, game_height: float | None = None, censor_profanity: bool | None = None, remove_pauses: bool | None = None, microphone_enhancement: bool = False, normalize_loudness: bool = False, volume_gain_db: float = 0, start_seconds: float | None = None, end_seconds: float | None = None, hook_seconds: float = 0, caption_text: str | None = None, caption_word_timestamps: list[dict] | None = None):
    segment = db.row(
        """SELECT s.*, r.rating, r.review_reason, r.censor_profanity,
                  r.remove_pauses, sr.id AS current_revision_id,
                  r.reviewed_revision_id, v.path, v.original_name, v.duration_seconds
           FROM segments s
           JOIN videos v ON v.id=s.video_id
           JOIN segment_reviews r ON r.segment_id=s.id
           JOIN segment_revisions sr
             ON sr.segment_id=s.id AND sr.revision_number=s.revision_number
           WHERE s.id=?""",
        (segment_id,),
    )
    if not segment:
        not_found("Segment not found")
    if segment["rating"] != "accepted":
        raise HTTPException(409, "Approve this clip before exporting MP4.")
    if segment.get("reviewed_revision_id") != segment.get("current_revision_id"):
        raise HTTPException(409, "This clip changed during reanalysis. Review and approve the current version before exporting MP4.")
    if not Path(segment["path"]).is_file():
        raise HTTPException(409, "The source video was removed. Analysis data and archived review audio remain available, but MP4 export is no longer possible.")
    if (start_seconds is None) != (end_seconds is None):
        raise HTTPException(400, "Provide both custom start and end times together.")
    if start_seconds is None:
        start = max(0, segment["start_seconds"] - min(10, max(0, lead_in_seconds)))
        end = segment["end_seconds"] + min(10, max(0, lead_out_seconds))
    else:
        start, end = max(0, float(start_seconds)), float(end_seconds)
    duration_limit = float(segment.get("duration_seconds") or 0)
    if duration_limit > 0:
        end = min(end, duration_limit)
    if end - start < 0.5:
        raise HTTPException(400, "A clip must be at least 0.5 seconds long.")
    hook_seconds = max(0.0, float(hook_seconds))
    if hook_seconds and hook_seconds >= end - start - 0.49:
        raise HTTPException(400, "The opening hook must be shorter than the selected clip.")
    if captions_preset not in {"none", "clean", "highlight", "minimal", "boxed_pop", "neon_gaming", "cinematic", "karaoke_punch", "minimal_center"}:
        raise HTTPException(400, "Unknown caption preset.")
    if layout not in {"original", "portrait_camera", "portrait_game", "portrait_split"}:
        raise HTTPException(400, "Unknown clip layout.")
    if audio_track not in {1, 2, 3, 4}:
        raise HTTPException(400, "Audio track must be between 1 and 4.")
    if caption_position not in {"top", "two_fifths", "middle", "four_fifths", "bottom"} or font_family not in {"Inter", "Montserrat", "Poppins", "Lato", "Roboto Condensed", "Oswald", "Nunito", "Noto Sans", "Bungee", "Cinzel", "Pixelify Sans"} or not re.fullmatch(r"#[0-9A-Fa-f]{6}", base_color) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", active_color) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", outline_color) or not 20 <= opacity <= 100 or not 1 <= int(max_lines) <= 4 or not -12 <= float(volume_gain_db) <= 12:
        raise HTTPException(400, "Invalid caption settings.")
    censor_profanity = bool(segment.get("censor_profanity")) if censor_profanity is None else bool(censor_profanity)
    remove_pauses = bool(segment.get("remove_pauses")) if remove_pauses is None else bool(remove_pauses)
    if hook_seconds and remove_pauses:
        raise HTTPException(400, "Opening hook and pause removal cannot be combined yet. Turn off pause removal for this export.")
    suffix = ("" if captions_preset == "none" else f"_captions-{captions_preset}") + ("" if layout == "original" else f"_{layout}") + (f"_hook-{hook_seconds:.0f}s" if hook_seconds else "") + ("_censored" if censor_profanity else "") + ("_dynamic" if remove_pauses else "") + ("_voice" if microphone_enhancement else "") + ("_levelled" if normalize_loudness else "")
    fallback = f"{Path(segment['original_name']).stem}_{start:.0f}-{end:.0f}{suffix}"
    destination = _available_export_path(_safe_export_name(filename, fallback))
    requested_rectangles = (camera_x, camera_y, camera_width, camera_height, game_x, game_y, game_width, game_height)
    if any(value is None for value in requested_rectangles) and not all(value is None for value in requested_rectangles):
        raise HTTPException(400, "Provide all camera and gameplay layout coordinates together.")
    if all(value is None for value in requested_rectangles):
        defaults = export_defaults() or {}
        camera_rect = tuple(float(defaults.get(key, value)) for key, value in (
            ("camera_x", 0.78), ("camera_y", 0.03), ("camera_width", 0.11), ("camera_height", 0.11),
        ))
        game_rect = tuple(float(defaults.get(key, value)) for key, value in (
            ("game_x", 0.22), ("game_y", 0.0), ("game_width", 0.56), ("game_height", 1.0),
        ))
    else:
        camera_rect = tuple(float(value) for value in requested_rectangles[:4])
        game_rect = tuple(float(value) for value in requested_rectangles[4:])
        for label, rect in (("Camera", camera_rect), ("Gameplay", game_rect)):
            x, y, width, height = rect
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0.02 < width <= 1 and 0.02 < height <= 1 and x + width <= 1 and y + height <= 1):
                raise HTTPException(400, f"{label} layout area must fit inside the source frame.")
    captions_path = None
    try:
        if caption_word_timestamps is not None:
            if len(caption_word_timestamps) > 6000:
                raise HTTPException(400, "Too many caption words supplied for export.")
            word_timestamps = [word for word in caption_word_timestamps if isinstance(word, dict)]
        else:
            word_timestamps = json.loads(segment.get("word_timestamps") or "[]")
        caption_transcript = segment["transcript"] if caption_text is None else caption_text
        if hook_seconds:
            split_at = end - start - hook_seconds
            remapped_words = []
            for word in word_timestamps:
                if word.get("start") is None or word.get("end") is None:
                    continue
                left, right = float(word["start"]) - start, float(word["end"]) - start
                if right <= 0 or left >= end - start:
                    continue
                if left >= split_at:
                    remapped_words.append({**word, "start": start + left - split_at, "end": start + right - split_at})
                elif right <= split_at:
                    remapped_words.append({**word, "start": start + hook_seconds + left, "end": start + hook_seconds + right})
            word_timestamps = remapped_words
        pause_ranges = pause_trim_ranges(word_timestamps, end - start, start) if remove_pauses else [(0.0, end - start)]
        output_duration = sum(right - left for left, right in pause_ranges)
        if remove_pauses and len(pause_ranges) > 1:
            relative_words = [
                {**word, "start": float(word["start"]) - start, "end": float(word["end"]) - start}
                for word in word_timestamps
                if word.get("start") is not None and word.get("end") is not None
            ]
            remapped = remap_words_for_kept_ranges(relative_words, pause_ranges)
            word_timestamps = [{**word, "start": start + float(word["start"]), "end": start + float(word["end"])} for word in remapped]
        if captions_preset != "none":
            captions_path = settings.work_dir / f"{segment_id}-{captions_preset}.ass"
            write_caption_ass(captions_path, caption_transcript, output_duration, captions_preset, word_timestamps, start, caption_position, base_color, active_color, censor_profanity, outline_enabled, outline_color, glow_enabled, opacity, font_family, int(max_lines))
        export_clip(Path(segment["path"]), destination, start, end, captions_path, layout, audio_track, word_timestamps, caption_transcript, censor_profanity, camera_rect, game_rect, pause_ranges, microphone_enhancement, normalize_loudness, volume_gain_db, hook_seconds)
    except MediaError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(500, f"Unable to export clip: {exc}") from exc
    finally:
        if captions_path:
            captions_path.unlink(missing_ok=True)
    return FileResponse(destination, media_type="video/mp4", filename=destination.name)


def _resolve_audio_preview(segment_id: str, audio_track: int, remove_pauses: bool) -> tuple[dict, Path | None, Path | None]:
    segment = db.row("SELECT s.*, v.path, v.source_removed FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?", (segment_id,))
    if not segment:
        not_found("Segment not found")
    if audio_track not in {1, 2, 3, 4}:
        raise HTTPException(400, "Audio track must be between 1 and 4.")
    source = Path(segment["path"])
    if not source.is_file():
        archive = Path(segment.get("archive_audio_path") or "")
        if archive.is_file():
            archived_track = int(segment.get("archive_audio_track") or 1)
            if audio_track != archived_track:
                raise HTTPException(400, f"Only archived track {archived_track} is available after source removal.")
            if bool(remove_pauses) != bool(segment.get("remove_pauses")):
                raise HTTPException(409, "Archived audio is available only with the pause-removal setting saved when the source was removed.")
            return segment, None, archive
        raise HTTPException(404, "The original recording was removed and this clip has no archived audio. Analysis data and reviews are still retained.")
    try:
        available_tracks = audio_track_count(source)
    except MediaError as exc:
        raise HTTPException(500, f"Unable to inspect audio tracks: {exc}") from exc
    if audio_track > available_tracks:
        raise HTTPException(400, f"Track {audio_track} does not exist in this recording. Available audio tracks: {available_tracks}.")
    return segment, source, None


@app.get("/api/segments/{segment_id}/audio-preview/check")
def check_audio_preview(segment_id: str, audio_track: int = 1, remove_pauses: bool = False):
    segment, source, archive = _resolve_audio_preview(segment_id, audio_track, remove_pauses)
    return {
        "status": "ok",
        "audio_track": audio_track,
        "remove_pauses": bool(remove_pauses),
        "archived": archive is not None,
        "duration_seconds": max(0.0, float(segment["end_seconds"]) - float(segment["start_seconds"])),
    }


@app.get("/api/segments/{segment_id}/audio-preview")
def audio_preview(segment_id: str, audio_track: int = 1, remove_pauses: bool = False):
    segment, source, archive = _resolve_audio_preview(segment_id, audio_track, remove_pauses)
    if archive is not None:
        return FileResponse(archive, media_type="audio/mpeg", filename=archive.name)
    assert source is not None
    destination = settings.previews_dir / f"{segment_id}-track{audio_track}{'-dynamic' if remove_pauses else ''}.mp3"
    with _media_output_lock:
        if not destination.is_file():
            try:
                words = json.loads(segment.get("word_timestamps") or "[]")
                pause_ranges = pause_trim_ranges(words, float(segment["end_seconds"]) - float(segment["start_seconds"]), float(segment["start_seconds"])) if remove_pauses else None
                export_audio_preview(source, destination, segment["start_seconds"], segment["end_seconds"], audio_track, pause_ranges)
            except MediaError as exc:
                raise HTTPException(500, f"Unable to prepare audio preview: {exc}") from exc
    return FileResponse(destination, media_type="audio/mpeg", filename=destination.name)


@app.get("/api/segments/{segment_id}/composer-audio-preview")
def composer_audio_preview(segment_id: str, audio_track: int = 1, censor_profanity: bool = False, remove_pauses: bool = False, microphone_enhancement: bool = False, normalize_loudness: bool = False, volume_gain_db: float = 0):
    """Render an exact audio-only Composer preview with the export settings."""
    if not -12 <= float(volume_gain_db) <= 12:
        raise HTTPException(400, "Volume correction must be between -12 dB and +12 dB.")
    segment, source, archive = _resolve_audio_preview(segment_id, audio_track, remove_pauses)
    if archive is not None or source is None:
        raise HTTPException(409, "An exact Composer audio preview needs the original recording. Archived review audio cannot be reprocessed.")
    settings_key = json.dumps({
        "track": audio_track, "censor": bool(censor_profanity), "pauses": bool(remove_pauses),
        "voice": bool(microphone_enhancement), "normalize": bool(normalize_loudness), "gain": round(float(volume_gain_db), 2),
    }, sort_keys=True)
    digest = hashlib.sha256(settings_key.encode("utf-8")).hexdigest()[:16]
    destination = settings.previews_dir / f"composer-{segment_id}-{digest}.mp3"
    with _media_output_lock:
        if not destination.is_file():
            try:
                start, end = float(segment["start_seconds"]), float(segment["end_seconds"])
                words = json.loads(segment.get("word_timestamps") or "[]")
                pause_ranges = pause_trim_ranges(words, end - start, start) if remove_pauses else [(0.0, end - start)]
                if remove_pauses and len(pause_ranges) > 1:
                    relative_words = [
                        {**word, "start": float(word["start"]) - start, "end": float(word["end"]) - start}
                        for word in words if word.get("start") is not None and word.get("end") is not None
                    ]
                    remapped = remap_words_for_kept_ranges(relative_words, pause_ranges)
                    words = [{**word, "start": start + float(word["start"]), "end": start + float(word["end"])} for word in remapped]
                export_audio_preview(source, destination, start, end, audio_track, pause_ranges, words, segment["transcript"], censor_profanity, microphone_enhancement, normalize_loudness, volume_gain_db)
            except MediaError as exc:
                destination.unlink(missing_ok=True)
                raise HTTPException(500, f"Unable to render Composer audio preview: {exc}") from exc
    return FileResponse(destination, media_type="audio/mpeg", filename=destination.name)


@app.get("/api/collections")
def collections():
    return db.rows(
        """SELECT c.*,
                  (SELECT COUNT(*) FROM collection_examples e WHERE e.collection_id=c.id) +
                  (SELECT COUNT(*) FROM external_examples x WHERE x.collection_id=c.id) AS examples,
                  (SELECT COUNT(*) FROM external_examples x WHERE x.collection_id=c.id) AS external_examples
           FROM collections c ORDER BY c.name"""
    )


@app.get("/api/reference-sources")
def reference_sources():
    return db.rows(
        """SELECT r.*, c.name AS collection_name,
                  (SELECT COUNT(*) FROM external_examples x WHERE x.collection_id=r.collection_id) AS imported_examples
           FROM reference_sources r JOIN collections c ON c.id=r.collection_id
           ORDER BY r.updated_at DESC"""
    )


@app.get("/api/prompts")
def saved_prompts():
    return db.rows("SELECT * FROM saved_prompts ORDER BY updated_at DESC")


@app.get("/api/rejection-reasons")
def rejection_reasons():
    return db.rows("SELECT reason FROM rejection_reasons ORDER BY created_at DESC")


@app.post("/api/rejection-reasons", status_code=201)
def create_rejection_reason(body: RejectionReasonCreate):
    reason = " ".join(body.reason.split())
    if not reason:
        raise HTTPException(400, "A rejection reason is required.")
    with db.connection() as con:
        con.execute("INSERT OR IGNORE INTO rejection_reasons (reason, created_at) VALUES (?, ?)", (reason, db.now()))
    return {"reason": reason}


@app.delete("/api/rejection-reasons/{reason}")
def delete_rejection_reason(reason: str):
    """Remove a saved suggestion without rewriting past clip decisions."""
    normalized = " ".join(reason.split())
    with db.connection() as con:
        deleted = con.execute("DELETE FROM rejection_reasons WHERE reason=?", (normalized,)).rowcount
    if not deleted:
        not_found("Saved rejection reason not found")
    return {"ok": True, "reason": normalized}


@app.get("/api/caption-defaults")
def caption_defaults():
    return db.row("SELECT * FROM caption_defaults WHERE id=1")


@app.get("/api/export-defaults")
def export_defaults():
    return db.row("SELECT * FROM export_defaults WHERE id=1")


def _validate_layout_rectangles(body: ExportDefaultsUpdate) -> None:
    if body.camera_x + body.camera_width > 1 or body.camera_y + body.camera_height > 1:
        raise HTTPException(400, "Camera area must fit inside the source frame.")
    if body.game_x + body.game_width > 1 or body.game_y + body.game_height > 1:
        raise HTTPException(400, "Gameplay area must fit inside the source frame.")


def _layout_values(body: ExportDefaultsUpdate) -> tuple:
    return (body.layout, body.camera_x, body.camera_y, body.camera_width, body.camera_height,
            body.game_x, body.game_y, body.game_width, body.game_height)


@app.get("/api/layout-presets")
def layout_presets():
    return db.rows("SELECT * FROM layout_presets ORDER BY name COLLATE NOCASE")


@app.post("/api/layout-presets")
def create_layout_preset(body: LayoutPresetCreate):
    _validate_layout_rectangles(body)
    preset_id = str(uuid.uuid4())
    try:
        with db.connection() as con:
            con.execute(
                """INSERT INTO layout_presets (id, name, layout, camera_x, camera_y, camera_width, camera_height,
                   game_x, game_y, game_width, game_height, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (preset_id, body.name.strip(), *_layout_values(body), db.now()),
            )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "A layout preset with this name already exists.") from exc
        raise
    return db.row("SELECT * FROM layout_presets WHERE id=?", (preset_id,))


@app.delete("/api/layout-presets/{preset_id}")
def delete_layout_preset(preset_id: str):
    with db.connection() as con:
        con.execute("DELETE FROM layout_presets WHERE id=?", (preset_id,))
    return {"ok": True}


@app.post("/api/layout-presets/{preset_id}/apply")
def apply_layout_preset(preset_id: str):
    preset = db.row("SELECT * FROM layout_presets WHERE id=?", (preset_id,))
    if not preset:
        not_found("Layout preset not found")
    with db.connection() as con:
        con.execute(
            """UPDATE export_defaults SET layout=?, camera_x=?, camera_y=?, camera_width=?, camera_height=?,
               game_x=?, game_y=?, game_width=?, game_height=?, updated_at=? WHERE id=1""",
            (preset["layout"], preset["camera_x"], preset["camera_y"], preset["camera_width"], preset["camera_height"],
             preset["game_x"], preset["game_y"], preset["game_width"], preset["game_height"], db.now()),
        )
    return export_defaults()


@app.get("/api/analysis-audio-defaults")
def analysis_audio_defaults():
    return db.row("SELECT * FROM analysis_audio_defaults WHERE id=1")


@app.get("/api/discovery-defaults")
def discovery_defaults():
    return profile_payload()


@app.put("/api/discovery-defaults")
def update_discovery_defaults(body: DiscoveryDefaultsUpdate):
    pattern_set_id = body.pattern_set_id.strip()
    if pattern_set_id and not db.row(
        "SELECT id FROM discovery_pattern_sets WHERE id=? AND profile=?",
        (pattern_set_id, body.active_profile),
    ):
        raise HTTPException(400, "Choose a pattern set created for this discovery profile.")
    with db.connection() as con:
        con.execute(
            "UPDATE discovery_defaults SET active_profile=?, pattern_set_id=?, profanity_filter=?, updated_at=? WHERE id=1",
            (body.active_profile, pattern_set_id, body.profanity_filter, db.now()),
        )
    return profile_payload()


@app.post("/api/discovery-pattern-sets", status_code=201)
def create_discovery_pattern_set(body: DiscoveryPatternSetCreate):
    pattern_set_id = str(uuid.uuid4())
    name = " ".join(body.name.split())
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO discovery_pattern_sets (id, name, profile, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (pattern_set_id, name, body.profile, db.now(), db.now()),
            )
    except Exception as exc:
        raise HTTPException(409, "This profile already has a pattern set with that name.") from exc
    return {"id": pattern_set_id, "name": name, "profile": body.profile}


@app.delete("/api/discovery-pattern-sets/{pattern_set_id}")
def delete_discovery_pattern_set(pattern_set_id: str):
    pattern_set = db.row("SELECT id, name FROM discovery_pattern_sets WHERE id=?", (pattern_set_id,))
    if not pattern_set:
        not_found("Discovery pattern set not found")
    with db.connection() as con:
        con.execute("DELETE FROM discovery_pattern_sets WHERE id=?", (pattern_set_id,))
        con.execute("UPDATE discovery_defaults SET pattern_set_id='' WHERE id=1 AND pattern_set_id=?", (pattern_set_id,))
    return {"ok": True, "name": pattern_set["name"]}


@app.get("/api/videos/{video_id}/top-clips")
def top_clips(video_id: str, limit: int = 10, unrated_only: bool = False):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    query = """SELECT s.*, r.rating, r.review_reason
               FROM segments s JOIN segment_reviews r ON r.segment_id=s.id
               WHERE s.video_id=? AND s.lifecycle_state='current'
                 AND s.embedding IS NOT NULL AND r.rating != 'rejected'"""
    if unrated_only:
        query += " AND r.rating='unrated'"
    candidates = db.rows(query, (video_id,))
    ranked = score_candidates(candidates, profile=active_profile())
    ranked = [item for item in ranked if not is_disallowed_reading(item)]
    ranked = filter_profanity(ranked)
    ranked = best_of_stream(ranked, limit=max(1, min(30, limit)))
    return db.serialize_segments(ranked)


@app.put("/api/analysis-audio-defaults")
def update_analysis_audio_defaults(body: AnalysisAudioDefaultsUpdate):
    with db.connection() as con:
        con.execute(
            "UPDATE analysis_audio_defaults SET mode=?, single_track=?, microphone_track=?, all_sounds_track=?, game_track=?, use_all_sounds=?, use_game=?, updated_at=? WHERE id=1",
            (body.mode, body.single_track, body.microphone_track, body.all_sounds_track, body.game_track, int(body.use_all_sounds), int(body.use_game), db.now()),
        )
    return analysis_audio_defaults()


@app.put("/api/export-defaults")
def update_export_defaults(body: ExportDefaultsUpdate):
    _validate_layout_rectangles(body)
    with db.connection() as con:
        con.execute(
            """UPDATE export_defaults SET layout=?, audio_track=?, camera_x=?, camera_y=?, camera_width=?, camera_height=?,
               game_x=?, game_y=?, game_width=?, game_height=?, updated_at=? WHERE id=1""",
            (body.layout, body.audio_track, body.camera_x, body.camera_y, body.camera_width, body.camera_height,
             body.game_x, body.game_y, body.game_width, body.game_height, db.now()),
        )
    return export_defaults()


@app.put("/api/caption-defaults")
def update_caption_defaults(body: CaptionDefaultsUpdate):
    with db.connection() as con:
        con.execute(
            "UPDATE caption_defaults SET captions_preset=?, base_color=?, active_color=?, font_family=?, outline_enabled=?, outline_color=?, glow_enabled=?, opacity=?, max_lines=?, updated_at=? WHERE id=1",
            (body.captions_preset, body.base_color.upper(), body.active_color.upper(), body.font_family, int(body.outline_enabled), body.outline_color.upper(), int(body.glow_enabled), body.opacity, body.max_lines, db.now()),
        )
    return caption_defaults()


@app.get("/api/caption-favorites")
def caption_favorites():
    return db.rows("SELECT * FROM caption_favorites ORDER BY created_at DESC")


@app.post("/api/caption-favorites", status_code=201)
def create_caption_favorite(body: CaptionFavoriteCreate):
    favorite = {"id": str(uuid.uuid4()), "name": body.name.strip(), "captions_preset": body.captions_preset, "base_color": body.base_color.upper(), "active_color": body.active_color.upper(), "font_family": body.font_family, "outline_enabled": int(body.outline_enabled), "outline_color": body.outline_color.upper(), "glow_enabled": int(body.glow_enabled), "opacity": body.opacity, "max_lines": body.max_lines, "created_at": db.now()}
    if not favorite["name"]:
        raise HTTPException(400, "Favorite name is required.")
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO caption_favorites (id, name, captions_preset, base_color, active_color, font_family, outline_enabled, outline_color, glow_enabled, opacity, max_lines, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(favorite.values()),
            )
    except Exception as exc:
        raise HTTPException(409, "A favorite with this name already exists.") from exc
    return favorite


@app.delete("/api/caption-favorites/{favorite_id}")
def delete_caption_favorite(favorite_id: str):
    with db.connection() as con:
        cursor = con.execute("DELETE FROM caption_favorites WHERE id=?", (favorite_id,))
    if not cursor.rowcount:
        not_found("Caption favorite not found")
    return {"ok": True}


@app.post("/api/prompts", status_code=201)
def create_saved_prompt(body: SavedPromptCreate):
    prompt_id, timestamp = str(uuid.uuid4()), db.now()
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO saved_prompts (id, name, prompt, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (prompt_id, body.name.strip(), body.prompt.strip(), timestamp, timestamp),
            )
    except Exception as exc:
        raise HTTPException(409, "Saved prompt with that name already exists.") from exc
    return {"id": prompt_id, "name": body.name.strip(), "prompt": body.prompt.strip()}


@app.delete("/api/prompts/{prompt_id}", status_code=204)
def delete_saved_prompt(prompt_id: str):
    with db.connection() as con:
        if not con.execute("DELETE FROM saved_prompts WHERE id=?", (prompt_id,)).rowcount:
            not_found("Saved prompt not found")


@app.post("/api/collections", status_code=201)
def create_collection(body: CollectionCreate):
    collection_id = str(uuid.uuid4())
    try:
        with db.connection() as con:
            con.execute("INSERT INTO collections (id, name, created_at) VALUES (?, ?, ?)", (collection_id, body.name.strip(), db.now()))
    except Exception as exc:
        raise HTTPException(409, "Collection with that name already exists.") from exc
    return {"id": collection_id, "name": body.name.strip()}


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: str):
    """Delete a collection and its locally stored reference metadata/files."""
    collection = db.row("SELECT id, name FROM collections WHERE id=?", (collection_id,))
    if not collection:
        not_found("Collection not found")
    if db.row(
        "SELECT id FROM reference_imports WHERE collection_id=? AND state IN ('queued', 'running') LIMIT 1",
        (collection_id,),
    ):
        raise HTTPException(409, "Cancel or finish the active reference import before deleting this collection.")
    remote_sources = db.rows("SELECT source_path FROM reference_url_sources WHERE collection_id=?", (collection_id,))
    with db.connection() as con:
        # Older databases may have been created before foreign keys were
        # enabled on every SQLite connection, so delete dependent rows here.
        con.execute("DELETE FROM collection_examples WHERE collection_id=?", (collection_id,))
        con.execute("DELETE FROM external_examples WHERE collection_id=?", (collection_id,))
        con.execute("DELETE FROM reference_imports WHERE collection_id=?", (collection_id,))
        con.execute("DELETE FROM reference_sources WHERE collection_id=?", (collection_id,))
        con.execute("DELETE FROM reference_url_sources WHERE collection_id=?", (collection_id,))
        con.execute("DELETE FROM collections WHERE id=?", (collection_id,))
    reference_root = settings.reference_dir.resolve()
    for source in remote_sources:
        try:
            path = Path(source["source_path"]).resolve()
            if reference_root in path.parents:
                path.unlink(missing_ok=True)
        except OSError:
            # The collection is already removed; a locked download can be
            # deleted later without affecting the rest of the app.
            pass
    return {"ok": True, "name": collection["name"]}


@app.post("/api/collections/{collection_id}/examples", status_code=201)
def add_example(collection_id: str, body: ExampleCreate):
    with db.connection() as con:
        if not con.execute("SELECT id FROM collections WHERE id=?", (collection_id,)).fetchone():
            not_found("Collection not found")
        if not con.execute("SELECT id FROM segments WHERE id=?", (body.segment_id,)).fetchone():
            not_found("Segment not found")
        segment = con.execute(
            """SELECT video_id, revision_number, start_seconds, end_seconds, transcript, embedding
               FROM segments WHERE id=?""",
            (body.segment_id,),
        ).fetchone()
        con.execute(
            """INSERT INTO collection_examples
               (collection_id, segment_id, revision_number, snapshot_video_id,
                snapshot_start_seconds, snapshot_end_seconds, snapshot_transcript,
                snapshot_embedding, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(collection_id, segment_id) DO NOTHING""",
            (
                collection_id, body.segment_id, int(segment["revision_number"] or 1),
                segment["video_id"], segment["start_seconds"], segment["end_seconds"],
                segment["transcript"], segment["embedding"], db.now(),
            ),
        )
    return {"ok": True}


@app.get("/api/collections/{collection_id}/imports")
def reference_imports(collection_id: str):
    # Always expose active work so an older queued/running import cannot fall
    # outside the recent-history limit and disappear together with its cancel
    # button.
    return db.rows(
        """SELECT * FROM reference_imports current
           WHERE current.collection_id=?
             AND (
                 current.state IN ('queued', 'running')
                 OR current.id IN (
                     SELECT recent.id FROM reference_imports recent
                     WHERE recent.collection_id=?
                     ORDER BY recent.created_at DESC, recent.id DESC LIMIT 10
                 )
             )
           ORDER BY CASE WHEN current.state IN ('queued', 'running') THEN 0 ELSE 1 END,
                    current.created_at DESC, current.id DESC""",
        (collection_id, collection_id),
    )


@app.post("/api/collections/{collection_id}/imports", status_code=202)
def import_references(collection_id: str, body: ReferenceFolderImport):
    if not db.row("SELECT id FROM collections WHERE id=?", (collection_id,)):
        not_found("Collection not found")
    folder = Path(body.folder_path).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, "This folder does not exist or cannot be accessed by the local server.")
    timestamp = db.now()
    with db.connection() as con:
        source = con.execute("SELECT id FROM reference_sources WHERE collection_id=? AND folder_path=?", (collection_id, str(folder.resolve()))).fetchone()
        source_id = source["id"] if source else str(uuid.uuid4())
        con.execute(
            """INSERT INTO reference_sources (id, collection_id, folder_path, include_subfolders, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(collection_id, folder_path) DO UPDATE SET include_subfolders=excluded.include_subfolders, updated_at=excluded.updated_at""",
            (source_id, collection_id, str(folder.resolve()), int(body.include_subfolders), timestamp, timestamp),
        )
    import_id = queue_reference_import(collection_id, str(folder.resolve()), body.include_subfolders, "folder")
    _wake_durable_worker()
    return {"import_id": import_id, "source_id": source_id}


@app.post("/api/collections/{collection_id}/imports/from-url", status_code=202)
def import_reference_url(collection_id: str, body: ReferenceUrlImport):
    if not db.row("SELECT id FROM collections WHERE id=?", (collection_id,)):
        not_found("Collection not found")
    source_url = _supported_reference_url(body.source_url)
    import_id = queue_reference_import(collection_id, source_url, False, "url")
    _wake_durable_worker()
    return {"import_id": import_id}


def queue_reference_import(
    collection_id: str,
    folder_path: str,
    include_subfolders: bool,
    kind: str = "folder",
) -> str:
    import_id, timestamp = str(uuid.uuid4()), db.now()
    with db.connection() as con:
        item = reference_queue.enqueue(
            con,
            collection_id=collection_id,
            kind=kind,
            source=folder_path,
            include_subfolders=include_subfolders,
            import_id=import_id,
            now=timestamp,
        )
    return str(item["id"])


@app.post("/api/reference-sources/{source_id}/imports", status_code=202)
def reimport_reference_source(source_id: str):
    source = db.row("SELECT * FROM reference_sources WHERE id=?", (source_id,))
    if not source:
        not_found("Reference source not found")
    if not Path(source["folder_path"]).is_dir():
        raise HTTPException(400, "Saved reference folder is no longer available.")
    import_id = queue_reference_import(
        source["collection_id"], source["folder_path"], bool(source["include_subfolders"]), "folder",
    )
    _wake_durable_worker()
    return {"import_id": import_id}


def collection_embeddings(collection_id: str) -> list[list[float]]:
    rows = db.rows(
        """SELECT COALESCE(e.snapshot_embedding, s.embedding) AS embedding
             FROM collection_examples e JOIN segments s ON e.segment_id=s.id
           WHERE e.collection_id=? AND COALESCE(e.snapshot_embedding, s.embedding) IS NOT NULL
           UNION ALL
           SELECT embedding FROM external_examples WHERE collection_id=?""",
        (collection_id, collection_id),
    )
    return [json.loads(item["embedding"]) for item in rows]


@app.post("/api/collections/{collection_id}/prompt-suggestion")
def suggest_prompt_from_collection(collection_id: str):
    collection = db.row("SELECT name FROM collections WHERE id=?", (collection_id,))
    if not collection:
        not_found("Collection not found")
    references = db.rows(
        """SELECT COALESCE(NULLIF(e.snapshot_transcript, ''), s.transcript) AS transcript,
                  COALESCE(e.snapshot_embedding, s.embedding) AS embedding
             FROM collection_examples e JOIN segments s ON e.segment_id=s.id
           WHERE e.collection_id=? AND COALESCE(e.snapshot_embedding, s.embedding) IS NOT NULL
           UNION ALL
           SELECT transcript, embedding FROM external_examples WHERE collection_id=?""",
        (collection_id, collection_id),
    )
    if not references:
        raise HTTPException(400, "Add or import at least one analyzed reference clip first.")
    transcripts = [item["transcript"] for item in references]
    embeddings = [json.loads(item["embedding"]) for item in references]
    return {"name": f"{collection['name']} prompt", "prompt": build_reference_prompt(transcripts, embeddings)}


def ranked_candidates(video_id: str, reference: list[float], limit: int) -> list[dict]:
    candidates = db.rows(
        """SELECT s.*, r.rating, r.review_reason
           FROM segments s JOIN segment_reviews r ON r.segment_id=s.id
           WHERE s.video_id=? AND s.lifecycle_state='current'
             AND s.embedding IS NOT NULL AND r.rating != 'rejected'""",
        (video_id,),
    )
    if not candidates:
        raise HTTPException(400, "Selected video does not have completed analysis.")
    ranked = suppress_duplicate_groups(score_candidates(candidates, reference=reference, profile=active_profile()))
    ranked = [item for item in ranked if not is_disallowed_reading(item)]
    ranked = filter_profanity(ranked)
    return db.serialize_segments(ranked[:limit])


@app.post("/api/collections/{collection_id}/search")
def similar(collection_id: str, body: SimilaritySearch):
    reference = collection_embeddings(collection_id)
    if not reference:
        raise HTTPException(400, "The collection does not yet contain analyzed examples.")
    return ranked_candidates(body.video_id, reference, body.limit)


@app.post("/api/search/description")
def search_by_description(body: DescriptionSearch):
    try:
        reference = embed_texts([body.description])[0]
        return ranked_candidates(body.video_id, [reference], body.limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Prompt search failed: {type(exc).__name__}: {exc}") from exc
