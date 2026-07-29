import json
import os
import re
import shutil
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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import database as db
from app.config import settings
from app.models import (
    CaptionDefaultsUpdate,
    CaptionFavoriteCreate,
    AnalysisAudioDefaultsUpdate,
    ChatDelayUpdate,
    DiscoveryDefaultsUpdate,
    CollectionCreate,
    DescriptionSearch,
    ExampleCreate,
    ExportDefaultsUpdate,
    ExportRequest,
    RatingUpdate,
    RejectionReasonCreate,
    ReferenceFolderImport,
    RemoteVideoCreate,
    SavedPromptCreate,
    SegmentTimingUpdate,
    SegmentCensorUpdate,
    SegmentTranscriptUpdate,
    SimilaritySearch,
)
from app.services.embeddings import cosine, embed_texts
from app.services.chat import apply_chat_reactions, chat_summary, import_chat, update_chat_delay
from app.services.discovery import (
    active_profile,
    assign_duplicate_groups,
    profile_payload,
    score_candidates,
    suppress_duplicate_groups,
)
from app.services.media import MediaError, export_audio_preview, export_clip, write_caption_ass
from app.services.pipeline import analyse, import_reference_folder, transcribe_clip_range
from app.services.tagging import GAME_REACTION_TAG, assess_clip_quality, assess_logical_sense, build_reference_prompt, infer_tags
from app.services.updates import update_status
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
            if reading >= 0.55:
                tags = list(dict.fromkeys(tags + ["reading"]))
            con.execute(
                "UPDATE segments SET tags=?, quality_score=?, quality_signals=?, reading_likelihood=? WHERE id=?",
                (json.dumps(tags, ensure_ascii=False), score, json.dumps(signals), reading, item["id"]),
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
        items = db.rows("SELECT id, start_seconds, embedding FROM segments WHERE video_id=? AND embedding IS NOT NULL", (video["video_id"],))
        records = [{"id": item["id"], "start": item["start_seconds"], "vector": json.loads(item["embedding"]), "duplicate_group": ""} for item in items]
        assign_duplicate_groups(records)
        with db.connection() as con:
            for record in records:
                con.execute("UPDATE segments SET duplicate_group=? WHERE id=?", (record["duplicate_group"], record["id"]))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    backfill_segment_quality()
    backfill_context_signals()
    remove_legacy_game_audio_bonus()
    backfill_duplicate_groups()
    for item in db.rows("SELECT video_id FROM chat_settings"):
        apply_chat_reactions(item["video_id"])
    yield


app = FastAPI(title="ClipFinder Local", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def prevent_frontend_cache(request: Request, call_next):
    response = await call_next(request)
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
    try:
        update_job(job_id, 1, "Waiting for worker", "running")
        analyse(video_id, lambda progress, message: update_job(job_id, progress, message))
        update_job(job_id, 100, "Analysis completed", "completed")
    except Exception as exc:
        with db.connection() as con:
            con.execute("UPDATE videos SET status='failed', error_message=?, updated_at=? WHERE id=?", (str(exc), db.now(), video_id))
        update_job(job_id, 100, str(exc), "failed")


def _supported_remote_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    supported = ("youtube.com", "youtu.be", "twitch.tv")
    if parsed.scheme not in {"http", "https"} or not host or not any(host == domain or host.endswith("." + domain) for domain in supported):
        raise HTTPException(400, "Use a public YouTube link or a Twitch VOD link.")
    return url


def _downloaded_video_path(video_id: str) -> Path:
    candidates = [
        path for path in settings.incoming_dir.glob(f"{video_id}.*")
        if path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"} and path.is_file()
    ]
    if not candidates:
        raise RuntimeError("The download finished without a usable video file.")
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
    try:
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
        update_job(job_id, 100, "Analysis completed", "completed")
    except ModuleNotFoundError as exc:
        detail = "Remote import requires yt-dlp. Run: python -m pip install -r requirements.txt" if exc.name == "yt_dlp" else str(exc)
        with db.connection() as con:
            con.execute("UPDATE videos SET status='failed', error_message=?, updated_at=? WHERE id=?", (detail, db.now(), video_id))
        update_job(job_id, 100, detail, "failed")
    except Exception as exc:
        detail = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail.lower():
            detail = (
                "HTTPS certificate verification failed while contacting YouTube/Twitch. "
                "Update/reinstall ClipFinder so its certificate bundle is refreshed, then try again."
            )
        with db.connection() as con:
            con.execute("UPDATE videos SET status='failed', error_message=?, updated_at=? WHERE id=?", (detail, db.now(), video_id))
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


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "data_dir": str(settings.clipfinder_data_dir), "version": __version__}


@app.get("/api/update-status")
def app_update_status():
    return update_status()


@app.post("/api/videos", status_code=201)
async def upload_video(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename or Path(file.filename).suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm"}:
        raise HTTPException(400, "Add MP4, MKV, MOV or WebM video file.")
    video_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    destination = settings.incoming_dir / f"{video_id}{Path(file.filename).suffix.lower()}"
    with destination.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    timestamp = db.now()
    with db.connection() as con:
        con.execute(
            "INSERT INTO videos (id, original_name, path, status, created_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
            (video_id, file.filename, str(destination), timestamp, timestamp),
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
            "INSERT INTO videos (id, original_name, path, source_url, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (video_id, "YouTube/Twitch download", str(placeholder), source_url, timestamp, timestamp),
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
        con.execute("UPDATE videos SET status='queued', error_message=NULL, updated_at=? WHERE id=?", (timestamp, video_id))
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
def video_segments(video_id: str, q: str = "", rating: str = "", tag: str = "", hide_reading: bool = False, show_duplicates: bool = False):
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
    return [db.serialize_segment(item) for item in ranked]


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
    with db.connection() as con:
        if not con.execute("SELECT id FROM segments WHERE id=?", (segment_id,)).fetchone():
            not_found("Segment not found")
        reason = " ".join(body.review_reason.split()) if body.rating == "rejected" else ""
        con.execute("UPDATE segments SET rating=?, review_reason=? WHERE id=?", (body.rating, reason, segment_id))
        if reason:
            con.execute("INSERT OR IGNORE INTO rejection_reasons (reason, created_at) VALUES (?, ?)", (reason, db.now()))
    return {"ok": True, "review_reason": reason}


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
        if reading_likelihood >= 0.55:
            tags = list(dict.fromkeys(tags + ["reading"]))
    except Exception as exc:
        raise HTTPException(500, f"Unable to update captions for the new range: {exc}") from exc
    with db.connection() as con:
        con.execute(
            "UPDATE segments SET start_seconds=?, end_seconds=?, transcript=?, keywords=?, tags=?, word_timestamps=?, embedding=?, quality_score=?, quality_signals=?, logical_sense_score=?, reading_likelihood=?, audio_event_score=0, game_reaction_score=0, voice_expression_score=0 WHERE id=?",
            (body.start_seconds, body.end_seconds, transcript, json.dumps(keywords, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), json.dumps(words, ensure_ascii=False), json.dumps(vector), quality_score, json.dumps(quality_signals), logical_sense_score, reading_likelihood, segment_id),
        )
    apply_chat_reactions(segment["video_id"])
    (settings.previews_dir / f"{segment_id}.mp3").unlink(missing_ok=True)
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
    if reading_likelihood >= 0.55:
        tags = list(dict.fromkeys(tags + ["reading"]))
    with db.connection() as con:
        con.execute(
            "UPDATE segments SET transcript=?, keywords=?, tags=?, word_timestamps=?, embedding=?, quality_score=?, quality_signals=?, logical_sense_score=?, reading_likelihood=? WHERE id=?",
            (transcript, json.dumps(keywords, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), json.dumps(words, ensure_ascii=False), json.dumps(vector), quality_score, json.dumps(quality_signals), logical_sense_score, reading_likelihood, segment_id),
        )
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


@app.post("/api/segments/{segment_id}/export")
def export_segment(segment_id: str, body: ExportRequest):
    return _export_segment(segment_id, body.lead_in_seconds, body.lead_out_seconds, body.captions_preset, body.caption_position, body.base_color, body.active_color, body.layout, body.audio_track, body.filename)


@app.get("/api/segments/{segment_id}/export")
def download_segment(segment_id: str, lead_in_seconds: float = 0, lead_out_seconds: float = 0, captions_preset: str = "none", caption_position: str = "bottom", base_color: str = "#FFFFFF", active_color: str = "#FFFF00", layout: str = "original", audio_track: int = 1, filename: str = ""):
    return _export_segment(segment_id, lead_in_seconds, lead_out_seconds, captions_preset, caption_position, base_color, active_color, layout, audio_track, filename)


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


def _export_segment(segment_id: str, lead_in_seconds: float, lead_out_seconds: float, captions_preset: str = "none", caption_position: str = "bottom", base_color: str = "#FFFFFF", active_color: str = "#FFFF00", layout: str = "original", audio_track: int = 1, filename: str = ""):
    segment = db.row("SELECT s.*, v.path, v.original_name FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?", (segment_id,))
    if not segment:
        not_found("Segment not found")
    if segment["rating"] != "accepted":
        raise HTTPException(409, "Approve this clip before exporting MP4.")
    start = max(0, segment["start_seconds"] - min(10, max(0, lead_in_seconds)))
    end = segment["end_seconds"] + min(10, max(0, lead_out_seconds))
    if captions_preset not in {"none", "clean", "highlight", "minimal"}:
        raise HTTPException(400, "Unknown caption preset.")
    if layout not in {"original", "portrait_camera", "portrait_game", "portrait_split"}:
        raise HTTPException(400, "Unknown clip layout.")
    if audio_track not in {1, 2, 3, 4}:
        raise HTTPException(400, "Audio track must be between 1 and 4.")
    if caption_position not in {"top", "middle", "bottom"} or not re.fullmatch(r"#[0-9A-Fa-f]{6}", base_color) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", active_color):
        raise HTTPException(400, "Invalid caption settings.")
    censor_profanity = bool(segment.get("censor_profanity"))
    suffix = ("" if captions_preset == "none" else f"_captions-{captions_preset}") + ("" if layout == "original" else f"_{layout}") + ("_censored" if censor_profanity else "")
    fallback = f"{Path(segment['original_name']).stem}_{start:.0f}-{end:.0f}{suffix}"
    destination = _available_export_path(_safe_export_name(filename, fallback))
    captions_path = None
    try:
        word_timestamps = json.loads(segment.get("word_timestamps") or "[]")
        if captions_preset != "none":
            captions_path = settings.work_dir / f"{segment_id}-{captions_preset}.ass"
            write_caption_ass(captions_path, segment["transcript"], end - start, captions_preset, word_timestamps, start, caption_position, base_color, active_color, censor_profanity)
        export_clip(Path(segment["path"]), destination, start, end, captions_path, layout, audio_track, word_timestamps, segment["transcript"], censor_profanity)
    except MediaError as exc:
        raise HTTPException(500, f"Unable to export clip: {exc}") from exc
    finally:
        if captions_path:
            captions_path.unlink(missing_ok=True)
    return FileResponse(destination, media_type="video/mp4", filename=destination.name)


@app.get("/api/segments/{segment_id}/audio-preview")
def audio_preview(segment_id: str, audio_track: int = 1):
    segment = db.row("SELECT s.*, v.path FROM segments s JOIN videos v ON v.id=s.video_id WHERE s.id=?", (segment_id,))
    if not segment:
        not_found("Segment not found")
    if audio_track not in {1, 2, 3, 4}:
        raise HTTPException(400, "Audio track must be between 1 and 4.")
    destination = settings.previews_dir / f"{segment_id}-track{audio_track}.mp3"
    if not destination.is_file():
        try:
            export_audio_preview(Path(segment["path"]), destination, segment["start_seconds"], segment["end_seconds"], audio_track)
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


@app.get("/api/caption-defaults")
def caption_defaults():
    return db.row("SELECT * FROM caption_defaults WHERE id=1")


@app.get("/api/export-defaults")
def export_defaults():
    return db.row("SELECT * FROM export_defaults WHERE id=1")


@app.get("/api/analysis-audio-defaults")
def analysis_audio_defaults():
    return db.row("SELECT * FROM analysis_audio_defaults WHERE id=1")


@app.get("/api/discovery-defaults")
def discovery_defaults():
    return profile_payload()


@app.put("/api/discovery-defaults")
def update_discovery_defaults(body: DiscoveryDefaultsUpdate):
    with db.connection() as con:
        con.execute("UPDATE discovery_defaults SET active_profile=?, updated_at=? WHERE id=1", (body.active_profile, db.now()))
    return profile_payload()


@app.get("/api/videos/{video_id}/top-clips")
def top_clips(video_id: str, limit: int = 10):
    if not db.row("SELECT id FROM videos WHERE id=?", (video_id,)):
        not_found("Video not found")
    candidates = db.rows("SELECT * FROM segments WHERE video_id=? AND embedding IS NOT NULL AND rating != 'rejected'", (video_id,))
    ranked = suppress_duplicate_groups(score_candidates(candidates, profile=active_profile()))
    return [db.serialize_segment(item) for item in ranked[:max(1, min(30, limit))]]


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
    with db.connection() as con:
        con.execute(
            "UPDATE export_defaults SET layout=?, audio_track=?, updated_at=? WHERE id=1",
            (body.layout, body.audio_track, db.now()),
        )
    return export_defaults()


@app.put("/api/caption-defaults")
def update_caption_defaults(body: CaptionDefaultsUpdate):
    with db.connection() as con:
        con.execute(
            "UPDATE caption_defaults SET captions_preset=?, base_color=?, active_color=?, updated_at=? WHERE id=1",
            (body.captions_preset, body.base_color.upper(), body.active_color.upper(), db.now()),
        )
    return caption_defaults()


@app.get("/api/caption-favorites")
def caption_favorites():
    return db.rows("SELECT * FROM caption_favorites ORDER BY created_at DESC")


@app.post("/api/caption-favorites", status_code=201)
def create_caption_favorite(body: CaptionFavoriteCreate):
    favorite = {"id": str(uuid.uuid4()), "name": body.name.strip(), "captions_preset": body.captions_preset, "base_color": body.base_color.upper(), "active_color": body.active_color.upper(), "created_at": db.now()}
    if not favorite["name"]:
        raise HTTPException(400, "Favorite name is required.")
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO caption_favorites (id, name, captions_preset, base_color, active_color, created_at) VALUES (?, ?, ?, ?, ?, ?)",
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
    return [db.serialize_segment(item) for item in ranked[:limit]]


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
