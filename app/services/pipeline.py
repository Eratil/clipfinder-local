import json
import uuid
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app import database as db
from app.config import settings
from app.services.embeddings import embed_texts
from app.services.cuda_runtime import cuda12_runtime_error
from app.services.discovery import assign_duplicate_groups
from app.services.media import duration_seconds, extract_audio, extract_audio_range
from app.services.scenes import detect_boundaries
from app.services.tagging import assess_clip_quality, infer_tags

Progress = Callable[[int, str], None]
_transcription_model = None


def verify_cuda_runtime() -> None:
    """Fail immediately with a useful diagnosis instead of leaving a job at 0%."""
    if settings.whisper_device.lower() != "cuda":
        return
    error = cuda12_runtime_error()
    if error:
        raise RuntimeError(error + " Or set WHISPER_DEVICE=cpu and WHISPER_COMPUTE_TYPE=int8 in .env.")


def transcribe(
    audio_path: Path,
    progress: Progress,
    duration: float | None = None,
    progress_start: int = 18,
    progress_end: int = 62,
) -> list[dict]:
    global _transcription_model
    if _transcription_model is None:
        from faster_whisper import WhisperModel
        progress(progress_start, "Loading transcription model on GPU")
        verify_cuda_runtime()
        compute_type = "int8" if settings.whisper_device.lower() == "cpu" else settings.whisper_compute_type
        _transcription_model = WhisperModel(settings.whisper_model, device=settings.whisper_device, compute_type=compute_type)
    model = _transcription_model
    parts, _info = model.transcribe(str(audio_path), vad_filter=True, word_timestamps=True)
    result: list[dict] = []
    last_progress = progress_start
    for part in parts:
        words = [
            {"start": float(word.start), "end": float(word.end), "word": word.word.strip()}
            for word in (part.words or [])
            if word.start is not None and word.end is not None and word.word.strip()
        ]
        result.append({"start": float(part.start), "end": float(part.end), "text": part.text.strip(), "words": words})
        if duration and duration > 0:
            current_progress = progress_start + int(min(part.end / duration, 1) * (progress_end - progress_start))
            if current_progress > last_progress:
                progress(current_progress, "Transcribing audio")
                last_progress = current_progress
    progress(progress_end, "Transcription complete")
    return result


def build_candidates(parts: list[dict], duration: float, boundaries: list[float] | None = None) -> list[dict]:
    """Pack neighbouring speech into reviewable 15–60 second candidates."""
    if not parts:
        return [{"start": start, "end": min(start + settings.segment_max_seconds, duration), "text": ""}
                for start in range(0, int(duration), settings.segment_max_seconds)]
    candidates: list[dict] = []
    current: list[dict] = []
    for part in parts:
        if current and part["end"] - current[0]["start"] > settings.segment_max_seconds:
            candidates.append(_candidate(current))
            current = []
        current.append(part)
        if part["end"] - current[0]["start"] >= settings.segment_min_seconds:
            candidates.append(_candidate(current))
            current = []
    if current:
        candidate = _candidate(current)
        if candidate["end"] - candidate["start"] >= 3:
            candidates.append(candidate)
    return [_snap_to_scene(_add_context(candidate, duration), boundaries or [], duration) for candidate in candidates]


def _add_context(candidate: dict, duration: float) -> dict:
    """Keep a little setup and reaction tail around a spoken moment."""
    candidate["start"] = max(0.0, candidate["start"] - 0.6)
    candidate["end"] = min(duration, candidate["end"] + 1.0)
    return candidate


def _snap_to_scene(candidate: dict, boundaries: list[float], duration: float) -> dict:
    """Align a candidate edge to a nearby cut when it preserves its reviewable length."""
    start, end = candidate["start"], candidate["end"]
    nearby_start = [boundary for boundary in boundaries if abs(boundary - start) <= 2.5]
    nearby_end = [boundary for boundary in boundaries if abs(boundary - end) <= 2.5]
    snapped_start = min(nearby_start, key=lambda item: abs(item - start)) if nearby_start else start
    snapped_end = min(nearby_end, key=lambda item: abs(item - end)) if nearby_end else end
    if snapped_end - snapped_start >= 3:
        candidate["start"], candidate["end"] = max(0, snapped_start), min(duration, snapped_end)
    return candidate


def _candidate(parts: list[dict]) -> dict:
    return {
        "start": parts[0]["start"],
        "end": parts[-1]["end"],
        "text": " ".join(part["text"] for part in parts).strip(),
        "words": [word for part in parts for word in part.get("words", [])],
    }


def transcribe_clip_range(source: Path, start: float, end: float, audio_track: int = 1) -> tuple[str, list[dict]]:
    """Rebuild the caption text and word timing after an editor range change."""
    audio_path = settings.work_dir / f"caption-refresh-{uuid.uuid4()}.wav"
    try:
        extract_audio_range(source, audio_path, start, end, audio_track)
        parts = transcribe(audio_path, lambda _progress, _message: None, end - start, 0, 100)
        transcript = " ".join(part["text"] for part in parts).strip()
        words = [
            {"start": start + word["start"], "end": start + word["end"], "word": word["word"]}
            for part in parts
            for word in part.get("words", [])
        ]
        return transcript, words
    finally:
        audio_path.unlink(missing_ok=True)


def audio_event_scores(audio_path: Path, candidates: list[dict]) -> list[int]:
    """Score dynamic non-verbal events from a selected audio track without transcribing it."""
    with wave.open(str(audio_path), "rb") as handle:
        sample_rate = handle.getframerate()
        window_frames = max(1, sample_rate // 2)
        energies = []
        while True:
            raw = handle.readframes(window_frames)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            energies.append(float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0)
    if len(energies) < 3:
        return [0] * len(candidates)
    values = np.array(energies, dtype=np.float32)
    baseline, high, peak = np.percentile(values, [55, 88, 98])
    spread = max(1.0, peak - high)
    scores = []
    for candidate in candidates:
        left = max(0, int(candidate["start"] * 2))
        right = min(len(values), max(left + 1, int(np.ceil(candidate["end"] * 2))))
        section = values[left:right]
        if not len(section):
            scores.append(0)
            continue
        peak_bonus = max(0.0, float(section.max()) - high) / spread
        mean_bonus = max(0.0, float(section.mean()) - baseline) / max(1.0, high - baseline)
        scores.append(max(0, min(16, round(peak_bonus * 12 + mean_bonus * 4))))
    return scores


def visual_interest_scores(video_path: Path, records: list[dict], limit: int = 120) -> dict[str, int]:
    """Inspect only the strongest candidates for scene motion and visual change."""
    try:
        import cv2
    except Exception:
        return {}
    strongest = sorted(records, key=lambda item: item["quality_score"] + item["audio_event_score"], reverse=True)[:limit]
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return {}
    scores: dict[str, int] = {}
    try:
        for record in strongest:
            duration = max(0.8, record["end"] - record["start"])
            moments = [record["start"] + 0.15, record["start"] + duration * 0.5, max(record["start"] + 0.25, record["end"] - 0.15)]
            frames = []
            for moment in moments:
                capture.set(cv2.CAP_PROP_POS_MSEC, moment * 1000)
                ok, frame = capture.read()
                if not ok:
                    continue
                gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY).astype(np.float32)
                frames.append(gray)
            if len(frames) < 2:
                continue
            movement = float(np.mean(np.abs(frames[-1] - frames[0])))
            texture = float(np.mean([frame.std() for frame in frames]))
            score = max(0, min(16, round(max(0.0, movement - 8) * 0.42 + max(0.0, texture - 28) * 0.14)))
            if score:
                scores[record["id"]] = score
    finally:
        capture.release()
    return scores


def analyse(video_id: str, report: Progress) -> None:
    video = db.row("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Nie znaleziono nagrania")
    source = Path(video["path"])
    audio_defaults = db.row("SELECT * FROM analysis_audio_defaults WHERE id=1") or {}
    mode = audio_defaults.get("mode", "single")
    transcript_track = int(audio_defaults.get("single_track", 1) if mode == "single" else audio_defaults.get("microphone_track", 2))
    event_tracks: list[tuple[str, int]] = []
    if mode == "split":
        if audio_defaults.get("use_all_sounds"):
            event_tracks.append(("all-sounds event", int(audio_defaults.get("all_sounds_track", 1))))
        if audio_defaults.get("use_game"):
            event_tracks.append(("game-audio event", int(audio_defaults.get("game_track", 3))))
    report(5, "Reading video metadata")
    duration = duration_seconds(source)
    with db.connection() as con:
        con.execute("UPDATE videos SET duration_seconds=?, status='processing', transcript_audio_track=?, audio_analysis_mode=?, updated_at=? WHERE id=?", (duration, transcript_track, mode, db.now(), video_id))

    audio_path = settings.work_dir / f"{video_id}.wav"
    report(10, "Extracting audio")
    extract_audio(source, audio_path, transcript_track)
    transcript = transcribe(audio_path, report, duration)
    report(66, "Detecting scene changes")
    boundaries = detect_boundaries(source)
    report(72, "Creating clip candidates")
    candidates = build_candidates(transcript, duration, boundaries)
    source_scores: list[tuple[str, list[int]]] = []
    temporary_audio: list[Path] = []
    for label, track in dict(event_tracks).items():
        event_path = audio_path if track == transcript_track else settings.work_dir / f"{video_id}-track{track}-{uuid.uuid4()}.wav"
        if event_path != audio_path:
            report(74, f"Reading {label}")
            extract_audio(source, event_path, track, sample_rate=8000)
            temporary_audio.append(event_path)
        source_scores.append((label, audio_event_scores(event_path, candidates)))
    vectors = embed_texts([item["text"] or "bez wypowiedzi" for item in candidates])
    records: list[dict] = []
    for index, (candidate, vector) in enumerate(zip(candidates, vectors)):
        keywords = [word.strip(".,!?;:").lower() for word in candidate["text"].split() if len(word.strip(".,!?;:")) >= 6][:12]
        tags = infer_tags(candidate["text"], vector)
        quality_score, quality_signals, reading_likelihood = assess_clip_quality(candidate["text"], candidate.get("words", []), candidate["start"], candidate["end"], tags)
        event_score = 0
        for label, scores in source_scores:
            source_score = scores[index]
            event_score = max(event_score, source_score)
            if source_score >= 7:
                quality_score = min(99, quality_score + source_score)
                quality_signals.append(label)
        if reading_likelihood >= 0.55:
            tags = list(dict.fromkeys(tags + ["reading"]))
        records.append({"id": str(uuid.uuid4()), "start": candidate["start"], "end": candidate["end"], "text": candidate["text"], "words": candidate.get("words", []), "vector": vector, "keywords": keywords, "tags": tags, "quality_score": quality_score, "quality_signals": quality_signals, "reading_likelihood": reading_likelihood, "audio_event_score": event_score, "duplicate_group": ""})
    assign_duplicate_groups(records)
    report(82, "Checking visual action in the strongest candidates")
    visual_scores = visual_interest_scores(source, records)
    for record in records:
        record["vision_score"] = visual_scores.get(record["id"], 0)
        if record["vision_score"] >= 7:
            record["quality_signals"].append("visual action")
    with db.connection() as con:
        con.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
        for record in records:
            con.execute(
                "INSERT INTO segments (id, video_id, start_seconds, end_seconds, transcript, keywords, tags, word_timestamps, embedding, quality_score, quality_signals, reading_likelihood, audio_event_score, vision_score, duplicate_group, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["id"], video_id, record["start"], record["end"], record["text"], json.dumps(record["keywords"], ensure_ascii=False), json.dumps(record["tags"], ensure_ascii=False), json.dumps(record["words"], ensure_ascii=False), json.dumps(record["vector"]), record["quality_score"], json.dumps(record["quality_signals"]), record["reading_likelihood"], record["audio_event_score"], record["vision_score"], record["duplicate_group"], db.now()),
            )
        con.execute("UPDATE videos SET status='ready', updated_at=? WHERE id=?", (db.now(), video_id))
    audio_path.unlink(missing_ok=True)
    for path in temporary_audio:
        path.unlink(missing_ok=True)
    report(100, f"Ready: {len(candidates)} candidates")


def import_reference_folder(collection_id: str, folder_path: str, include_subfolders: bool, report: Progress) -> int:
    """Transcribe local reference clips and store their text embeddings in a collection."""
    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Reference folder does not exist or is not a folder")
    allowed = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
    iterator = root.rglob("*") if include_subfolders else root.glob("*")
    files = [item for item in iterator if item.is_file() and item.suffix.lower() in allowed]
    if not files:
        raise ValueError("No supported video files were found in the reference folder")

    imported = 0
    for index, source in enumerate(files, start=1):
        start_progress = int((index - 1) / len(files) * 96)
        report(start_progress, f"Reference {index}/{len(files)}: {source.name}")
        audio_path = settings.work_dir / f"reference-{uuid.uuid4()}.wav"
        try:
            extract_audio(source, audio_path)
            clip_duration = duration_seconds(source)
            span = max(1, int(96 / len(files)))
            parts = transcribe(
                audio_path,
                lambda clip_progress, _message: report(min(96, start_progress + int(clip_progress / 100 * span)), f"Reference {index}/{len(files)}: transcribing"),
                clip_duration,
                0,
                100,
            )
            transcript = " ".join(part["text"] for part in parts).strip()
            embedding = embed_texts([transcript or "no speech"])[0]
            with db.connection() as con:
                con.execute(
                    """INSERT INTO external_examples (id, collection_id, source_path, original_name, transcript, embedding, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(collection_id, source_path) DO UPDATE SET transcript=excluded.transcript, embedding=excluded.embedding, created_at=excluded.created_at""",
                    (str(uuid.uuid4()), collection_id, str(source), source.name, transcript, json.dumps(embedding), db.now()),
                )
            imported += 1
        finally:
            audio_path.unlink(missing_ok=True)
        report(int(index / len(files) * 96), f"Imported {index}/{len(files)} references")
    report(100, f"Ready: {imported} reference clips")
    return imported
