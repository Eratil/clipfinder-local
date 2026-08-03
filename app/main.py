import base64
import json
import os
import re
import shutil
import statistics
import time
import uuid
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
    SimilaritySearch,
)
from app.services.embeddings import cosine, embed_texts
from app.services.chat import apply_chat_reactions, chat_summary, import_chat, update_chat_delay
from app.services.discovery import (
    active_profile,
    assign_duplicate_groups,
    is_disallowed_reading,
    profile_payload,
    preference_features,
    score_candidates,
    suppress_duplicate_groups,
)
from app.services.media import MediaError, audio_track_count, export_audio_preview, export_clip, pause_trim_ranges, remap_words_for_kept_ranges, run as run_media_command, write_caption_ass
from app.services.pipeline import analyse, import_reference_files, import_reference_folder, transcribe, transcribe_clip_range
from app.services.tagging import CHAT_QUESTION_ANSWER_TAG, CHAT_QUESTION_TAG, GAME_REACTION_TAG, assess_clip_quality, assess_context, assess_logical_sense, assess_self_containment, build_reference_prompt, detailed_lexical_tags, enrich_tags, infer_tags, score_moment_reaction
from app.services.updater import automatic_updates_available, install_downloaded_update, job_status as update_download_status, start_download as start_update_download
from app.services.updates import update_status
from app.services.runtime_status import runtime_status
from app.services import diagnostics
from app.version import __version__


def backfill_segment_quality() -> None:
    """Add lightweight quality/read-aloud data to clips analyzed before this feature."""
    items = db.rows("SELECT id, transcript, tags, word_timestamps, start_seconds, end_seconds FROM segments WHERE quality_score=0")
    if not items:
        return
    with db.connection() as con:
        for item in items:
            tags = json.loads(item.get("tags") or "[]")
            words = json.loads(item.get("word_timestamps") or "[]")
            score, signals, reading = assess_clip_quality(item["transcript"], words, item["start_seconds"], item["end_seconds"], tags)
            if reading >= 0.48:
                tags = list(dict.fromkeys(tags + ["reading"]))
            con.execute(
                "UPDATE segments SET tags=?, quality_score=?, quality_signals=?, reading_likelihood=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), score, json.dumps(signals), reading, item["id"]),
            )


def backfill_reading_filter() -> None:
    """Apply the stricter task/note reading rule to existing recordings once."""
    items = db.rows(
        """SELECT id, transcript, tags, word_timestamps, start_seconds, end_seconds, quality_signals,
                  logical_sense_score, context_score, self_contained_score, game_reaction_score,
                  voice_expression_score, chat_reaction_score, chat_joy_score, vision_score,
                  moment_reaction_score, moment_reaction_stage
           FROM segments WHERE quality_signals NOT LIKE ?""",
        ('%"reading heuristics v3"%',),
    )
    if not items:
        return
    updates = []
    for item in items:
        original_tags = [tag for tag in json.loads(item.get("tags") or "[]") if tag != "reading"]
        words = json.loads(item.get("word_timestamps") or "[]")
        quality, new_signals, reading = assess_clip_quality(item["transcript"], words, item["start_seconds"], item["end_seconds"], original_tags)
        signals = list(dict.fromkeys(json.loads(item.get("quality_signals") or "[]") + new_signals + ["reading heuristics v3"]))
        logical = assess_logical_sense(item["transcript"])
        context = int(item.get("context_score") or 50)
        self_contained = int(item.get("self_contained_score") or 50)
        if reading >= 0.48:
            original_tags.append("reading")
            logical, context, self_contained = min(logical, 35), min(context, 35), min(self_contained, 35)
        tags = enrich_tags(
            original_tags,
            logical_sense_score=logical,
            reading_likelihood=reading,
            game_reaction_score=int(item.get("game_reaction_score") or 0),
            voice_expression_score=int(item.get("voice_expression_score") or 0),
            chat_reaction_score=int(item.get("chat_reaction_score") or 0),
            chat_joy_score=int(item.get("chat_joy_score") or 0),
            vision_score=int(item.get("vision_score") or 0),
            context_score=context,
            self_contained_score=self_contained,
            moment_reaction_score=int(item.get("moment_reaction_score") or 0),
            moment_reaction_stage=item.get("moment_reaction_stage") or "",
        )
        updates.append((quality, json.dumps(signals, ensure_ascii=False), reading, logical, context, self_contained, json.dumps(tags, ensure_ascii=False), item["id"]))
    with db.connection() as con:
        con.executemany(
            "UPDATE segments SET quality_score=?, quality_signals=?, reading_likelihood=?, logical_sense_score=?, context_score=?, self_contained_score=?, tags=? WHERE id=?",
            updates,
        )


def backfill_context_signals() -> None:
    """Make the new context and game-reaction signals available for old clips."""
    items = db.rows(
        "SELECT id, transcript, tags, game_reaction_score FROM segments "
        "WHERE logical_sense_score < 0 OR (game_reaction_score >= 7 AND tags NOT LIKE ?) ",
        (f'%"{GAME_REACTION_TAG}"%',),
    )
    if not items:
        return
    with db.connection() as con:
        for item in items:
            tags = json.loads(item.get("tags") or "[]")
            if int(item.get("game_reaction_score") or 0) >= 7:
                tags = list(dict.fromkeys(tags + [GAME_REACTION_TAG]))
            con.execute(
                "UPDATE segments SET tags=?, logical_sense_score=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), assess_logical_sense(item["transcript"]), item["id"]),
            )


def backfill_segment_context() -> None:
    """Build lightweight context from adjacent existing candidates once."""
    updates = []
    for video in db.rows("SELECT DISTINCT video_id FROM segments WHERE context_score < 0 OR self_contained_score < 0"):
        segments = db.rows(
            "SELECT id, start_seconds, end_seconds, transcript, context_score, self_contained_score FROM segments WHERE video_id=? ORDER BY start_seconds",
            (video["video_id"],),
        )
        for segment in segments:
            if int(segment.get("context_score") or -1) >= 0 and int(segment.get("self_contained_score") or -1) >= 0:
                continue
            start, end = float(segment["start_seconds"]), float(segment["end_seconds"])
            before = " ".join(item["transcript"] for item in segments if start - 12 <= float(item["end_seconds"]) <= start and item["id"] != segment["id"])[-700:]
            after = " ".join(item["transcript"] for item in segments if end <= float(item["start_seconds"]) <= end + 12 and item["id"] != segment["id"])[:700]
            score, _signals = assess_context(segment["transcript"], before, after)
            self_contained = assess_self_containment(segment["transcript"], before, after)
            updates.append((score, self_contained, before, after, segment["id"]))
    if updates:
        with db.connection() as con:
            con.executemany("UPDATE segments SET context_score=?, self_contained_score=?, context_before=?, context_after=? WHERE id=?", updates)


def backfill_moment_reactions() -> None:
    """Seed the game-to-voice stage for clips analysed before the combined score."""
    items = db.rows("SELECT id, game_reaction_score FROM segments WHERE moment_reaction_score=0 AND game_reaction_score>=7")
    if not items:
        return
    updates = [(*score_moment_reaction(int(item["game_reaction_score"])), item["id"]) for item in items]
    with db.connection() as con:
        con.executemany("UPDATE segments SET moment_reaction_score=?, moment_reaction_stage=? WHERE id=?", updates)


def backfill_detailed_tags() -> None:
    """Add precise text/context labels to existing clips without retranscribing."""
    items = db.rows(
        """SELECT id, transcript, tags, logical_sense_score, reading_likelihood,
                  game_reaction_score, voice_expression_score, moment_reaction_score, moment_reaction_stage, chat_reaction_score, context_score, self_contained_score,
                  chat_joy_score, vision_score
           FROM segments"""
    )
    updates = []
    for item in items:
        # Question labels are now evidence-based: only chat.py may restore
        # them after matching a viewer question to a spoken answer.
        previous = [
            tag for tag in json.loads(item.get("tags") or "[]")
            if tag not in {CHAT_QUESTION_TAG, "forma: pytanie", CHAT_QUESTION_ANSWER_TAG}
        ]
        tags = list(dict.fromkeys(previous + detailed_lexical_tags(item["transcript"])))
        tags = enrich_tags(
            tags,
            logical_sense_score=int(item.get("logical_sense_score") or -1),
            reading_likelihood=float(item.get("reading_likelihood") or 0),
            game_reaction_score=int(item.get("game_reaction_score") or 0),
            voice_expression_score=int(item.get("voice_expression_score") or 0),
            chat_reaction_score=int(item.get("chat_reaction_score") or 0),
            chat_joy_score=int(item.get("chat_joy_score") or 0),
            vision_score=int(item.get("vision_score") or 0),
            context_score=int(item.get("context_score") or -1),
            self_contained_score=int(item.get("self_contained_score") or -1),
            moment_reaction_score=int(item.get("moment_reaction_score") or 0),
            moment_reaction_stage=item.get("moment_reaction_stage") or "",
        )
        if tags != json.loads(item.get("tags") or "[]"):
            updates.append((json.dumps(tags, ensure_ascii=False), item["id"]))
    if updates:
        with db.connection() as con:
            con.executemany("UPDATE segments SET tags=? WHERE id=?", updates)


def remove_legacy_game_audio_bonus() -> None:
    """Do not keep old scores where a loud game sound was treated as a reaction."""
    items = db.rows(
        """SELECT id, transcript, tags, word_timestamps, start_seconds, end_seconds, quality_signals
           FROM segments
           WHERE audio_event_score > 0 AND game_reaction_score=0 AND voice_expression_score=0"""
    )
    legacy_labels = {"all-sounds event", "game-audio event"}
    with db.connection() as con:
        for item in items:
            previous_signals = set(json.loads(item.get("quality_signals") or "[]"))
            if not previous_signals.intersection(legacy_labels):
                continue
            tags = json.loads(item.get("tags") or "[]")
            words = json.loads(item.get("word_timestamps") or "[]")
            score, signals, reading = assess_clip_quality(item["transcript"], words, item["start_seconds"], item["end_seconds"], tags)
            con.execute(
                "UPDATE segments SET quality_score=?, quality_signals=?, reading_likelihood=?, audio_event_score=0 WHERE id=?",
                (score, json.dumps(signals), reading, item["id"]),
            )


def backfill_duplicate_groups() -> None:
    """Group older candidates once so the compact review list works immediately."""
    for video in db.rows("SELECT DISTINCT video_id FROM segments WHERE embedding IS NOT NULL"):
        items = db.rows("SELECT id, start_seconds, end_seconds, embedding FROM segments WHERE video_id=? AND embedding IS NOT NULL", (video["video_id"],))
        records = [{"id": item["id"], "start": item["start_seconds"], "end": item["end_seconds"], "vector": json.loads(item["embedding"]), "duplicate_group": ""} for item in items]
        assign_duplicate_groups(records)
        with db.connection() as con:
            for record in records:
                con.execute("UPDATE segments SET duplicate_group=? WHERE id=?", (record["duplicate_group"], record["id"]))


def backfill_preference_feedback() -> None:
    """Seed the general profile with prior review decisions from older versions."""
    items = db.rows("SELECT * FROM segments WHERE rating IN ('accepted', 'rejected') AND embedding IS NOT NULL")
    if not items:
        return
    timestamp = db.now()
    with db.connection() as con:
        for item in items:
            con.execute(
                """INSERT OR IGNORE INTO preference_feedback (id, segment_id, profile, decision, review_reason, embedding, features, created_at, updated_at)
                   VALUES (?, ?, 'general', ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), item["id"], item["rating"], item.get("review_reason") or "", item["embedding"],
                 json.dumps(preference_features(item), ensure_ascii=False), timestamp, timestamp),
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
        ("duplicate-groups-v1", backfill_duplicate_groups),
        ("detailed-tags-v2", backfill_detailed_tags),
        ("preference-feedback-v1", backfill_preference_feedback),
    )
    tasks = (*tasks, ("chat-reactions-v1", lambda: [apply_chat_reactions(item["video_id"]) for item in db.rows("SELECT video_id FROM chat_settings")]))
    for task_name, callback in tasks:
        if db.maintenance_task_completed(task_name):
            continue
        started = time.perf_counter()
        diagnostics.logger().info("Startup maintenance started: %s", task_name)
        callback()
        db.mark_maintenance_task_completed(task_name)
        diagnostics.logger().info(
            "Startup maintenance completed: %s in %.2fs",
            task_name,
            time.perf_counter() - started,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
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
    yield
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
    ratio = statistics.median(ratios[-8:])
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


def run_analysis(video_id: str, job_id: str) -> None:
    started_at = time.monotonic()
    try:
        diagnostics.logger().info("Analysis started: video_id=%s job_id=%s", video_id, job_id)
        update_job(job_id, 1, "Waiting for worker", "running")
        analyse(video_id, lambda progress, message: update_job(job_id, progress, message))
        save_analysis_duration(video_id, started_at)
        update_job(job_id, 100, "Analysis completed", "completed")
        diagnostics.logger().info("Analysis completed: video_id=%s job_id=%s elapsed_seconds=%.2f", video_id, job_id, time.monotonic() - started_at)
    except Exception as exc:
        elapsed = round(max(0.0, time.monotonic() - started_at), 2)
        diagnostics.log_failure(f"Analysis failed: video_id={video_id} job_id={job_id} elapsed_seconds={elapsed:.2f}", exc)
        with db.connection() as con:
            con.execute("UPDATE videos SET status='failed', error_message=?, analysis_seconds=?, updated_at=? WHERE id=?", (str(exc), elapsed, db.now(), video_id))
        update_job(job_id, 100, str(exc), "failed")


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


def run_remote_import(video_id: str, job_id: str) -> None:
    video = db.row("SELECT source_url FROM videos WHERE id=?", (video_id,))
    if not video or not video.get("source_url"):
        update_job(job_id, 100, "Remote source URL is missing.", "failed")
        return
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
                    update_job(job_id, max(1, percentage), f"Downloading video: {min(100, int(current / total * 100)) if total else 'working'}%")
            elif status.get("status") == "finished":
                update_job(job_id, 20, "Download completed. Preparing analysis.")

        update_job(job_id, 1, "Preparing YouTube/Twitch download.")
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
        analyse(video_id, lambda progress, message: update_job(job_id, 20 + int(progress * 0.8), message))
        save_analysis_duration(video_id, analysis_started_at)
        update_job(job_id, 100, "Analysis completed", "completed")
        diagnostics.logger().info("Remote import and analysis completed: video_id=%s job_id=%s elapsed_seconds=%.2f", video_id, job_id, time.monotonic() - analysis_started_at)
    except ModuleNotFoundError as exc:
        detail = "Remote import requires yt-dlp. Run: python -m pip install -r requirements.txt" if exc.name == "yt_dlp" else str(exc)
        with db.connection() as con:
            con.execute("UPDATE videos SET status='failed', error_message=?, analysis_seconds=?, updated_at=? WHERE id=?", (detail, round(max(0.0, time.monotonic() - analysis_started_at), 2) if analysis_started_at else 0, db.now(), video_id))
        update_job(job_id, 100, detail, "failed")
    except Exception as exc:
        detail = str(exc)
        diagnostics.log_failure(f"Remote import failed: video_id={video_id} job_id={job_id}", exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail.lower():
            detail = (
                "HTTPS certificate verification failed while contacting YouTube/Twitch. "
                "Update/reinstall ClipFinder so its certificate bundle is refreshed, then try again."
            )
        with db.connection() as con:
            con.execute("UPDATE videos SET status='failed', error_message=?, analysis_seconds=?, updated_at=? WHERE id=?", (detail, round(max(0.0, time.monotonic() - analysis_started_at), 2) if analysis_started_at else 0, db.now(), video_id))
        update_job(job_id, 100, detail, "failed")


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


def run_reference_import(collection_id: str, import_id: str, folder_path: str, include_subfolders: bool) -> None:
    try:
        update_reference_import(import_id, 1, "Reading reference folder", "running")
        count = import_reference_folder(
            collection_id,
            folder_path,
            include_subfolders,
            lambda progress, message: update_reference_import(import_id, progress, message),
        )
        update_reference_import(import_id, 100, f"Imported {count} reference clips", "completed", count)
    except Exception as exc:
        update_reference_import(import_id, 100, str(exc), "failed")


def run_reference_url_import(collection_id: str, import_id: str, source_url: str) -> None:
    """Download one public short/video, then index it as a collection example."""
    try:
        from yt_dlp import YoutubeDL

        _configure_remote_download_certificates()
        update_reference_import(import_id, 1, "Preparing reference download", "running")

        def report_download(status: dict) -> None:
            if status.get("status") == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                current = status.get("downloaded_bytes") or 0
                percent = int(current / total * 45) if total else 5
                update_reference_import(import_id, max(2, min(45, percent)), "Downloading reference clip")
            elif status.get("status") == "finished":
                update_reference_import(import_id, 46, "Download complete. Transcribing reference clip")

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
            lambda progress, message: update_reference_import(import_id, 46 + int(progress * 0.54), message),
            {source_path.resolve(): source_url},
        )
        with db.connection() as con:
            con.execute(
                """INSERT INTO reference_url_sources (id, collection_id, source_url, source_path, original_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(collection_id, source_url) DO UPDATE SET source_path=excluded.source_path, original_name=excluded.original_name, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), collection_id, source_url, str(source_path), title, db.now(), db.now()),
            )
        update_reference_import(import_id, 100, f"Imported {count} reference clip from link", "completed", count)
    except Exception as exc:
        detail = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail.lower():
            detail = "HTTPS certificate verification failed while downloading the reference link. Update/reinstall ClipFinder, then try again."
        update_reference_import(import_id, 100, detail, "failed")


# Remote previews deliberately stay out of SQLite: they are a transient way to
# review one public Short/video. The source media is never stored in a
# collection, incoming recording or export directory.
_remote_preview_jobs: dict[str, dict] = {}
_remote_preview_fingerprints: dict[str, dict] = {}
_REMOTE_PREVIEW_MAX_SECONDS = 600


def _update_remote_preview(job_id: str, *, state: str | None = None, progress: int | None = None,
                           message: str | None = None, result: dict | None = None) -> None:
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
        tags = infer_tags(transcript, embedding)
        quality, quality_signals, reading_likelihood = assess_clip_quality(transcript, words, 0, duration, tags)
        logical_sense = assess_logical_sense(transcript)
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
    return {"status": "ok", "data_dir": str(settings.clipfinder_data_dir), "version": __version__}


@app.post("/api/remote-preview", status_code=202)
def create_remote_preview(body: RemotePreviewCreate, background_tasks: BackgroundTasks):
    source_url = _supported_reference_url(body.source_url)
    job_id = str(uuid.uuid4())
    _remote_preview_jobs[job_id] = {
        "id": job_id, "state": "queued", "progress": 0, "message": "Queued", "result": None,
    }
    background_tasks.add_task(run_remote_preview, job_id, source_url)
    return {"job_id": job_id}


@app.get("/api/remote-preview/{job_id}")
def get_remote_preview(job_id: str):
    job = _remote_preview_jobs.get(job_id)
    if not job:
        not_found("Preview job not found. Preview results are available only during this app session.")
    return job


@app.post("/api/remote-preview/{job_id}/save-pattern", status_code=201)
def save_remote_preview_pattern(job_id: str, body: RemotePreviewSave):
    job = _remote_preview_jobs.get(job_id)
    fingerprint = _remote_preview_fingerprints.get(job_id)
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
                json.dumps(fingerprint["tags"], ensure_ascii=False), int(fingerprint["quality_score"]),
                int(fingerprint["logical_sense_score"]), float(fingerprint["reading_likelihood"]),
                json.dumps(fingerprint["embedding"]), db.now(),
            ),
        )
    return {"ok": True, "pattern_set_id": pattern_set["id"], "pattern_set_name": pattern_set["name"]}


@app.get("/api/runtime-status")
def get_runtime_status():
    return {**runtime_status(), "version": __version__}


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
    try:
        install_downloaded_update(job_id)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"state": "installing"}


@app.post("/api/videos", status_code=201)
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), analysis_mode: str = Form("default")):
    if not file.filename or Path(file.filename).suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm"}:
        raise HTTPException(400, "Add MP4, MKV, MOV or WebM video file.")
    if analysis_mode not in {"fast", "default", "extended"}:
        raise HTTPException(400, "Choose Fast, Default or Extended analysis.")
    video_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    destination = settings.incoming_dir / f"{video_id}{Path(file.filename).suffix.lower()}"
    try:
        with destination.open("wb") as output:
            shutil.copyfileobj(file.file, output)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(507, f"Could not store the upload: {exc}. Check free disk space and write access to the ClipFinder data folder.") from exc
    finally:
        await file.close()
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            "INSERT INTO videos (id, original_name, path, analysis_mode, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (video_id, file.filename, str(destination), analysis_mode, timestamp, timestamp),
        )
        con.execute(
            "INSERT INTO jobs (id, video_id, state, progress, message, created_at, updated_at) VALUES (?, ?, 'queued', 0, 'Queued', ?, ?)",
            (job_id, video_id, timestamp, timestamp),
        )
    background_tasks.add_task(run_analysis, video_id, job_id)
    return {"video_id": video_id, "job_id": job_id}


@app.post("/api/videos/from-url", status_code=201)
def import_remote_video(body: RemoteVideoCreate, background_tasks: BackgroundTasks):
    source_url = _supported_remote_url(body.source_url)
    video_id, job_id, timestamp = str(uuid.uuid4()), str(uuid.uuid4()), db.now()
    placeholder = settings.incoming_dir / f"{video_id}.download"
    with db.connection() as con:
        con.execute(
            "INSERT INTO videos (id, original_name, path, source_url, analysis_mode, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (video_id, "YouTube/Twitch download", str(placeholder), source_url, body.analysis_mode, timestamp, timestamp),
        )
        con.execute(
            "INSERT INTO jobs (id, video_id, state, progress, message, created_at, updated_at) VALUES (?, ?, 'queued', 0, 'Queued remote download', ?, ?)",
            (job_id, video_id, timestamp, timestamp),
        )
    background_tasks.add_task(run_remote_import, video_id, job_id)
    return {"video_id": video_id, "job_id": job_id}


@app.post("/api/videos/{video_id}/analyse", status_code=202)
def restart_analysis(video_id: str, background_tasks: BackgroundTasks):
    video = db.row("SELECT * FROM videos WHERE id=?", (video_id,))
    if not video:
        not_found("Video not found")
    if not Path(video["path"]).is_file() and not video.get("source_url"):
        raise HTTPException(400, "The original video file is no longer available.")
    job_id, timestamp = str(uuid.uuid4()), db.now()
    with db.connection() as con:
        con.execute("UPDATE videos SET status='queued', error_message=NULL, analysis_seconds=0, updated_at=? WHERE id=?", (timestamp, video_id))
        con.execute(
            "INSERT INTO jobs (id, video_id, state, progress, message, created_at, updated_at) VALUES (?, ?, 'queued', 0, 'Queued again', ?, ?)",
            (job_id, video_id, timestamp, timestamp),
        )
    background_tasks.add_task(run_analysis if Path(video["path"]).is_file() else run_remote_import, video_id, job_id)
    return {"job_id": job_id}


@app.get("/api/videos")
def videos():
    items = db.rows(
        """SELECT v.*, j.progress, j.message, j.state AS job_state
           FROM videos v LEFT JOIN jobs j ON j.video_id=v.id
           WHERE j.created_at = (SELECT MAX(created_at) FROM jobs WHERE video_id=v.id)
           ORDER BY v.created_at DESC"""
    )
    for item in items:
        source = Path(item["path"])
        item["size_bytes"] = source.stat().st_size if source.is_file() else 0
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
    return {
        "video_bytes": video_bytes,
        "clip_bytes": clip_bytes,
        "video_count": sum(1 for path in source_paths if path.is_file()),
        "clip_count": len(export_files),
    }


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str):
    video = db.row("SELECT * FROM videos WHERE id=?", (video_id,))
    if not video:
        not_found("Video not found")
    if video["status"] in {"queued", "processing"}:
        raise HTTPException(409, "Wait for the current analysis to finish before deleting this recording.")
    source = Path(video["path"])
    if source.exists():
        try:
            source.resolve().relative_to(settings.incoming_dir.resolve())
        except ValueError as exc:
            raise HTTPException(400, "This recording is outside ClipFinder's managed incoming folder and will not be deleted.") from exc
        try:
            source.unlink()
        except OSError as exc:
            raise HTTPException(500, f"Could not delete the source recording: {exc}") from exc
    segment_ids = [item["id"] for item in db.rows("SELECT id FROM segments WHERE video_id=?", (video_id,))]
    with db.connection() as con:
        con.execute("DELETE FROM collection_examples WHERE segment_id IN (SELECT id FROM segments WHERE video_id=?)", (video_id,))
        con.execute("DELETE FROM jobs WHERE video_id=?", (video_id,))
        con.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
        con.execute("DELETE FROM videos WHERE id=?", (video_id,))
    for segment_id in segment_ids:
        for preview in settings.previews_dir.glob(f"{segment_id}-*"):
            preview.unlink(missing_ok=True)
    return {"ok": True}


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


@app.get("/api/videos/{video_id}/segments")
def video_segments(video_id: str, q: str = "", rating: str = "", tag: str = "", hide_reading: bool = False, show_duplicates: bool = False, sort: str = "suggested_desc"):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    clauses, parameters = ["video_id=?"], [video_id]
    if q.strip():
        clauses.append("transcript LIKE ?")
        parameters.append(f"%{q.strip()}%")
    if rating in {"unrated", "accepted", "rejected"}:
        clauses.append("rating=?")
        parameters.append(rating)
    if tag.strip():
        # Tags are stored as a JSON array.  Quoting the value keeps the match
        # exact (e.g. "gniew" does not match part of another tag name).
        clauses.append("tags LIKE ?")
        parameters.append(f'%"{tag.strip()}"%')
    if hide_reading:
        clauses.append("tags NOT LIKE ?")
        parameters.append('%"reading"%')
    items = db.rows(f"SELECT * FROM segments WHERE {' AND '.join(clauses)} AND embedding IS NOT NULL", tuple(parameters))
    ranked = suppress_duplicate_groups(score_candidates(items, profile=active_profile()), keep_alternatives=show_duplicates)
    if tag.strip() != "reading":
        ranked = [item for item in ranked if not is_disallowed_reading(item)]
    sort_fields = {
        "suggested_desc": ("ranking_score", True), "suggested_asc": ("ranking_score", False),
        "quality_desc": ("quality_score", True), "quality_asc": ("quality_score", False),
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


@app.post("/api/videos/{video_id}/chat")
async def upload_video_chat(video_id: str, chat_file: UploadFile = File(...), delay_seconds: float = Form(6)):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    if not 0 <= delay_seconds <= 60:
        raise HTTPException(400, "Chat delay must be between 0 and 60 seconds.")
    raw = await chat_file.read()
    if not raw:
        raise HTTPException(400, "The chat file is empty.")
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "The chat file is too large (maximum 50 MB).")
    try:
        return import_chat(video_id, chat_file.filename or "chat.txt", raw, delay_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    with db.connection() as con:
        segment = con.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if not segment:
            not_found("Segment not found")
        reason = " ".join(body.review_reason.split()) if body.rating == "rejected" else ""
        con.execute("UPDATE segments SET rating=?, review_reason=? WHERE id=?", (body.rating, reason, segment_id))
        if reason:
            con.execute("INSERT OR IGNORE INTO rejection_reasons (reason, created_at) VALUES (?, ?)", (reason, db.now()))
        if body.rating in {"accepted", "rejected"} and segment["embedding"]:
            updated_segment = dict(segment)
            updated_segment["rating"] = body.rating
            updated_segment["review_reason"] = reason
            timestamp = db.now()
            con.execute(
                """INSERT INTO preference_feedback (id, segment_id, profile, decision, review_reason, embedding, features, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(segment_id, profile) DO UPDATE SET decision=excluded.decision, review_reason=excluded.review_reason,
                   embedding=excluded.embedding, features=excluded.features, updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), segment_id, profile, body.rating, reason, segment["embedding"],
                 json.dumps(preference_features(updated_segment), ensure_ascii=False), timestamp, timestamp),
            )
        elif body.rating == "unrated":
            con.execute("DELETE FROM preference_feedback WHERE segment_id=? AND profile=?", (segment_id, profile))
    return {"ok": True, "review_reason": reason, "profile": profile}


@app.patch("/api/segments/{segment_id}/timing")
def update_segment_timing(segment_id: str, body: SegmentTimingUpdate):
    segment = db.row(
        "SELECT s.*, v.duration_seconds, v.path, v.transcript_audio_track FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?",
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
        quality_score, quality_signals, reading_likelihood = assess_clip_quality(transcript, words, body.start_seconds, body.end_seconds, tags)
        logical_sense_score = assess_logical_sense(transcript)
        if reading_likelihood >= 0.48:
            tags = list(dict.fromkeys(tags + ["reading"]))
        tags = enrich_tags(tags, logical_sense_score=logical_sense_score, reading_likelihood=reading_likelihood)
    except Exception as exc:
        raise HTTPException(500, f"Unable to update captions for the new range: {exc}") from exc
    with db.connection() as con:
        con.execute(
            "UPDATE segments SET start_seconds=?, end_seconds=?, transcript=?, keywords=?, tags=?, word_timestamps=?, embedding=?, quality_score=?, quality_signals=?, logical_sense_score=?, context_score=-1, self_contained_score=-1, context_before='', context_after='', reading_likelihood=?, audio_event_score=0, game_reaction_score=0, voice_expression_score=0, moment_reaction_score=0, moment_reaction_stage='' WHERE id=?",
            (body.start_seconds, body.end_seconds, transcript, json.dumps(keywords, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), json.dumps(words, ensure_ascii=False), json.dumps(vector), quality_score, json.dumps(quality_signals), logical_sense_score, reading_likelihood, segment_id),
        )
    apply_chat_reactions(segment["video_id"])
    for preview in settings.previews_dir.glob(f"{segment_id}-*"):
        preview.unlink(missing_ok=True)
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/transcript")
def update_segment_transcript(segment_id: str, body: SegmentTranscriptUpdate):
    segment = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    if not segment:
        not_found("Segment not found")
    transcript = " ".join(body.transcript.split())
    vector = embed_texts([transcript or "bez wypowiedzi"])[0]
    keywords = [word.strip(".,!?;:").lower() for word in transcript.split() if len(word.strip(".,!?;:")) >= 6][:12]
    tags = infer_tags(transcript, vector)
    words = approximate_word_timestamps(transcript, segment["start_seconds"], segment["end_seconds"])
    quality_score, quality_signals, reading_likelihood = assess_clip_quality(transcript, words, segment["start_seconds"], segment["end_seconds"], tags)
    logical_sense_score = assess_logical_sense(transcript)
    self_contained_score = assess_self_containment(transcript, segment.get("context_before") or "", segment.get("context_after") or "")
    if reading_likelihood >= 0.48:
        tags = list(dict.fromkeys(tags + ["reading"]))
    tags = enrich_tags(
        tags,
        logical_sense_score=logical_sense_score,
        reading_likelihood=reading_likelihood,
        game_reaction_score=int(segment.get("game_reaction_score") or 0),
        voice_expression_score=int(segment.get("voice_expression_score") or 0),
        chat_reaction_score=int(segment.get("chat_reaction_score") or 0),
        chat_joy_score=int(segment.get("chat_joy_score") or 0),
        vision_score=int(segment.get("vision_score") or 0),
        context_score=int(segment.get("context_score") or -1),
        self_contained_score=self_contained_score,
        moment_reaction_score=int(segment.get("moment_reaction_score") or 0),
        moment_reaction_stage=segment.get("moment_reaction_stage") or "",
    )
    with db.connection() as con:
        con.execute(
            "UPDATE segments SET transcript=?, keywords=?, tags=?, word_timestamps=?, embedding=?, quality_score=?, quality_signals=?, logical_sense_score=?, self_contained_score=?, reading_likelihood=? WHERE id=?",
            (transcript, json.dumps(keywords, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), json.dumps(words, ensure_ascii=False), json.dumps(vector), quality_score, json.dumps(quality_signals), logical_sense_score, self_contained_score, reading_likelihood, segment_id),
        )
    for preview in settings.previews_dir.glob(f"{segment_id}-*"):
        preview.unlink(missing_ok=True)
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/censor")
def update_segment_censor(segment_id: str, body: SegmentCensorUpdate):
    with db.connection() as con:
        if not con.execute("SELECT id FROM segments WHERE id=?", (segment_id,)).fetchone():
            not_found("Segment not found")
        con.execute("UPDATE segments SET censor_profanity=? WHERE id=?", (int(body.censor_profanity), segment_id))
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/pause-trim")
def update_segment_pause_trim(segment_id: str, body: SegmentPauseTrimUpdate):
    with db.connection() as con:
        if not con.execute("SELECT id FROM segments WHERE id=?", (segment_id,)).fetchone():
            not_found("Segment not found")
        con.execute("UPDATE segments SET remove_pauses=? WHERE id=?", (int(body.remove_pauses), segment_id))
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.patch("/api/segments/{segment_id}/tag-feedback")
def update_segment_tag_feedback(segment_id: str, body: TagFeedbackUpdate):
    tag = " ".join(body.tag.split())
    with db.connection() as con:
        segment = con.execute("SELECT id, tags FROM segments WHERE id=?", (segment_id,)).fetchone()
        if not segment:
            not_found("Segment not found")
        tags = json.loads(segment["tags"] or "[]")
        if tag not in tags:
            raise HTTPException(400, "This tag is no longer assigned to the clip.")
        if body.verdict == "unmarked":
            con.execute("DELETE FROM tag_feedback WHERE segment_id=? AND tag=?", (segment_id, tag))
        else:
            con.execute(
                """INSERT INTO tag_feedback (segment_id, tag, verdict, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(segment_id, tag) DO UPDATE SET verdict=excluded.verdict, updated_at=excluded.updated_at""",
                (segment_id, tag, body.verdict, db.now()),
            )
    updated = db.row("SELECT * FROM segments WHERE id=?", (segment_id,))
    return db.serialize_segment(updated)


@app.post("/api/segments/{segment_id}/export")
def export_segment(segment_id: str, body: ExportRequest):
    return _export_segment(segment_id, body.lead_in_seconds, body.lead_out_seconds, body.captions_preset, body.caption_position, body.base_color, body.active_color, body.layout, body.audio_track, body.filename, body.outline_enabled, body.outline_color, body.glow_enabled, body.opacity, body.font_family, body.camera_x, body.camera_y, body.camera_width, body.camera_height, body.game_x, body.game_y, body.game_width, body.game_height)


@app.get("/api/segments/{segment_id}/export")
def download_segment(segment_id: str, lead_in_seconds: float = 0, lead_out_seconds: float = 0, captions_preset: str = "none", caption_position: str = "bottom", base_color: str = "#FFFFFF", active_color: str = "#FFFF00", layout: str = "original", audio_track: int = 1, filename: str = "", outline_enabled: bool = True, outline_color: str = "#000000", glow_enabled: bool = False, opacity: int = 100, font_family: str = "Inter", camera_x: float | None = None, camera_y: float | None = None, camera_width: float | None = None, camera_height: float | None = None, game_x: float | None = None, game_y: float | None = None, game_width: float | None = None, game_height: float | None = None):
    return _export_segment(segment_id, lead_in_seconds, lead_out_seconds, captions_preset, caption_position, base_color, active_color, layout, audio_track, filename, outline_enabled, outline_color, glow_enabled, opacity, font_family, camera_x, camera_y, camera_width, camera_height, game_x, game_y, game_width, game_height)


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


def _export_segment(segment_id: str, lead_in_seconds: float, lead_out_seconds: float, captions_preset: str = "none", caption_position: str = "bottom", base_color: str = "#FFFFFF", active_color: str = "#FFFF00", layout: str = "original", audio_track: int = 1, filename: str = "", outline_enabled: bool = True, outline_color: str = "#000000", glow_enabled: bool = False, opacity: int = 100, font_family: str = "Inter", camera_x: float | None = None, camera_y: float | None = None, camera_width: float | None = None, camera_height: float | None = None, game_x: float | None = None, game_y: float | None = None, game_width: float | None = None, game_height: float | None = None):
    segment = db.row("SELECT s.*, v.path, v.original_name FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?", (segment_id,))
    if not segment:
        not_found("Segment not found")
    if segment["rating"] != "accepted":
        raise HTTPException(409, "Approve this clip before exporting MP4.")
    start = max(0, segment["start_seconds"] - min(10, max(0, lead_in_seconds)))
    end = segment["end_seconds"] + min(10, max(0, lead_out_seconds))
    if captions_preset not in {"none", "clean", "highlight", "minimal", "boxed_pop", "neon_gaming", "cinematic", "karaoke_punch", "minimal_center"}:
        raise HTTPException(400, "Unknown caption preset.")
    if layout not in {"original", "portrait_camera", "portrait_game", "portrait_split"}:
        raise HTTPException(400, "Unknown clip layout.")
    if audio_track not in {1, 2, 3, 4}:
        raise HTTPException(400, "Audio track must be between 1 and 4.")
    if caption_position not in {"top", "two_fifths", "middle", "four_fifths", "bottom"} or font_family not in {"Inter", "Montserrat", "Poppins", "Lato", "Roboto Condensed", "Oswald", "Nunito", "Noto Sans", "Bungee", "Cinzel", "Pixelify Sans"} or not re.fullmatch(r"#[0-9A-Fa-f]{6}", base_color) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", active_color) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", outline_color) or not 20 <= opacity <= 100:
        raise HTTPException(400, "Invalid caption settings.")
    censor_profanity = bool(segment.get("censor_profanity"))
    remove_pauses = bool(segment.get("remove_pauses"))
    suffix = ("" if captions_preset == "none" else f"_captions-{captions_preset}") + ("" if layout == "original" else f"_{layout}") + ("_censored" if censor_profanity else "") + ("_dynamic" if remove_pauses else "")
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
        word_timestamps = json.loads(segment.get("word_timestamps") or "[]")
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
            write_caption_ass(captions_path, segment["transcript"], output_duration, captions_preset, word_timestamps, start, caption_position, base_color, active_color, censor_profanity, outline_enabled, outline_color, glow_enabled, opacity, font_family)
        export_clip(Path(segment["path"]), destination, start, end, captions_path, layout, audio_track, word_timestamps, segment["transcript"], censor_profanity, camera_rect, game_rect, pause_ranges)
    except MediaError as exc:
        raise HTTPException(500, f"Unable to export clip: {exc}") from exc
    finally:
        if captions_path:
            captions_path.unlink(missing_ok=True)
    return FileResponse(destination, media_type="video/mp4", filename=destination.name)


@app.get("/api/segments/{segment_id}/audio-preview")
def audio_preview(segment_id: str, audio_track: int = 1, remove_pauses: bool = False):
    segment = db.row("SELECT s.*, v.path FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?", (segment_id,))
    if not segment:
        not_found("Segment not found")
    if audio_track not in {1, 2, 3, 4}:
        raise HTTPException(400, "Audio track must be between 1 and 4.")
    source = Path(segment["path"])
    if not source.is_file():
        raise HTTPException(404, "The original recording is no longer available.")
    try:
        available_tracks = audio_track_count(source)
    except MediaError as exc:
        raise HTTPException(500, f"Unable to inspect audio tracks: {exc}") from exc
    if audio_track > available_tracks:
        raise HTTPException(400, f"Track {audio_track} does not exist in this recording. Available audio tracks: {available_tracks}.")
    destination = settings.previews_dir / f"{segment_id}-track{audio_track}{'-dynamic' if remove_pauses else ''}.mp3"
    if not destination.is_file():
        try:
            words = json.loads(segment.get("word_timestamps") or "[]")
            pause_ranges = pause_trim_ranges(words, float(segment["end_seconds"]) - float(segment["start_seconds"]), float(segment["start_seconds"])) if remove_pauses else None
            export_audio_preview(source, destination, segment["start_seconds"], segment["end_seconds"], audio_track, pause_ranges)
        except MediaError as exc:
            raise HTTPException(500, f"Unable to prepare audio preview: {exc}") from exc
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
            "UPDATE discovery_defaults SET active_profile=?, pattern_set_id=?, updated_at=? WHERE id=1",
            (body.active_profile, pattern_set_id, db.now()),
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
    query = "SELECT * FROM segments WHERE video_id=? AND embedding IS NOT NULL AND rating != 'rejected'"
    if unrated_only:
        query += " AND rating='unrated'"
    candidates = db.rows(query, (video_id,))
    ranked = suppress_duplicate_groups(score_candidates(candidates, profile=active_profile()))
    ranked = [item for item in ranked if not is_disallowed_reading(item)]
    return db.serialize_segments(ranked[:max(1, min(30, limit))])


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
            "UPDATE caption_defaults SET captions_preset=?, base_color=?, active_color=?, font_family=?, outline_enabled=?, outline_color=?, glow_enabled=?, opacity=?, updated_at=? WHERE id=1",
            (body.captions_preset, body.base_color.upper(), body.active_color.upper(), body.font_family, int(body.outline_enabled), body.outline_color.upper(), int(body.glow_enabled), body.opacity, db.now()),
        )
    return caption_defaults()


@app.get("/api/caption-favorites")
def caption_favorites():
    return db.rows("SELECT * FROM caption_favorites ORDER BY created_at DESC")


@app.post("/api/caption-favorites", status_code=201)
def create_caption_favorite(body: CaptionFavoriteCreate):
    favorite = {"id": str(uuid.uuid4()), "name": body.name.strip(), "captions_preset": body.captions_preset, "base_color": body.base_color.upper(), "active_color": body.active_color.upper(), "font_family": body.font_family, "outline_enabled": int(body.outline_enabled), "outline_color": body.outline_color.upper(), "glow_enabled": int(body.glow_enabled), "opacity": body.opacity, "created_at": db.now()}
    if not favorite["name"]:
        raise HTTPException(400, "Favorite name is required.")
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO caption_favorites (id, name, captions_preset, base_color, active_color, font_family, outline_enabled, outline_color, glow_enabled, opacity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        con.execute(
            "INSERT OR IGNORE INTO collection_examples (collection_id, segment_id, created_at) VALUES (?, ?, ?)",
            (collection_id, body.segment_id, db.now()),
        )
    return {"ok": True}


@app.get("/api/collections/{collection_id}/imports")
def reference_imports(collection_id: str):
    return db.rows("SELECT * FROM reference_imports WHERE collection_id=? ORDER BY created_at DESC LIMIT 10", (collection_id,))


@app.post("/api/collections/{collection_id}/imports", status_code=202)
def import_references(collection_id: str, body: ReferenceFolderImport, background_tasks: BackgroundTasks):
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
    import_id = queue_reference_import(collection_id, str(folder), body.include_subfolders)
    background_tasks.add_task(run_reference_import, collection_id, import_id, str(folder), body.include_subfolders)
    return {"import_id": import_id, "source_id": source_id}


@app.post("/api/collections/{collection_id}/imports/from-url", status_code=202)
def import_reference_url(collection_id: str, body: ReferenceUrlImport, background_tasks: BackgroundTasks):
    if not db.row("SELECT id FROM collections WHERE id=?", (collection_id,)):
        not_found("Collection not found")
    source_url = _supported_reference_url(body.source_url)
    import_id = queue_reference_import(collection_id, source_url, False)
    background_tasks.add_task(run_reference_url_import, collection_id, import_id, source_url)
    return {"import_id": import_id}


def queue_reference_import(collection_id: str, folder_path: str, include_subfolders: bool) -> str:
    import_id, timestamp = str(uuid.uuid4()), db.now()
    with db.connection() as con:
        con.execute(
            """INSERT INTO reference_imports (id, collection_id, folder_path, state, progress, message, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', 0, 'Queued', ?, ?)""",
            (import_id, collection_id, folder_path, timestamp, timestamp),
        )
    return import_id


@app.post("/api/reference-sources/{source_id}/imports", status_code=202)
def reimport_reference_source(source_id: str, background_tasks: BackgroundTasks):
    source = db.row("SELECT * FROM reference_sources WHERE id=?", (source_id,))
    if not source:
        not_found("Reference source not found")
    if not Path(source["folder_path"]).is_dir():
        raise HTTPException(400, "Saved reference folder is no longer available.")
    import_id = queue_reference_import(source["collection_id"], source["folder_path"], bool(source["include_subfolders"]))
    background_tasks.add_task(run_reference_import, source["collection_id"], import_id, source["folder_path"], bool(source["include_subfolders"]))
    return {"import_id": import_id}


def collection_embeddings(collection_id: str) -> list[list[float]]:
    rows = db.rows(
        """SELECT s.embedding AS embedding FROM segments s JOIN collection_examples e ON e.segment_id=s.id
           WHERE e.collection_id=? AND s.embedding IS NOT NULL
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
        """SELECT s.transcript AS transcript, s.embedding AS embedding FROM segments s JOIN collection_examples e ON e.segment_id=s.id
           WHERE e.collection_id=? AND s.embedding IS NOT NULL
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
    candidates = db.rows("SELECT * FROM segments WHERE video_id=? AND embedding IS NOT NULL AND rating != 'rejected'", (video_id,))
    if not candidates:
        raise HTTPException(400, "Selected video does not have completed analysis.")
    ranked = suppress_duplicate_groups(score_candidates(candidates, reference=reference, profile=active_profile()))
    ranked = [item for item in ranked if not is_disallowed_reading(item)]
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
