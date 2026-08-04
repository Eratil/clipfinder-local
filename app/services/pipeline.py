import json
import unicodedata
import uuid
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app import database as db
from app.config import settings
from app.services.embeddings import embed_texts
from app.services.cuda_runtime import cuda12_runtime_error
from app.services import diagnostics
from app.services.discovery import assign_duplicate_groups
from app.services.media import audio_track_count, duration_seconds, extract_audio, extract_audio_range
from app.services.scenes import detect_boundaries
from app.services.tagging import GAME_REACTION_TAG, assess_clip_quality, assess_context, assess_extended_completeness, assess_logical_sense, assess_self_containment, assess_short_potential, enrich_tags, infer_tags, score_moment_reaction
from app.services.chat import apply_chat_reactions

Progress = Callable[[int, str], None]
_transcription_models: dict[tuple[str, str, str], object] = {}


def transcription_runtime() -> tuple[str, str, str | None]:
    """Return a usable Whisper runtime, falling back to CPU when CUDA is absent.

    A machine can have an NVIDIA driver but not the CUDA/cuDNN libraries needed
    by CTranslate2.  Such a machine must stay usable in the normal installer.
    """
    if settings.whisper_device.lower() != "cuda":
        return "cpu", "int8", None
    error = cuda12_runtime_error()
    if error:
        return "cpu", "int8", error
    return "cuda", settings.whisper_compute_type, None


def transcribe(
    audio_path: Path,
    progress: Progress,
    duration: float | None = None,
    progress_start: int = 18,
    progress_end: int = 62,
    model_name: str | None = None,
) -> list[dict]:
    selected_model = model_name or settings.whisper_model
    device, compute_type, fallback_reason = transcription_runtime()
    model_key = (selected_model, device, compute_type)
    if model_key not in _transcription_models:
        from faster_whisper import WhisperModel
        if fallback_reason:
            progress(progress_start, "CUDA unavailable - switching transcription to CPU")
        else:
            progress(progress_start, f"Loading {selected_model} transcription model on {device.upper()}")
        try:
            _transcription_models[model_key] = WhisperModel(selected_model, device=device, compute_type=compute_type)
        except Exception as exc:
            if device != "cuda":
                raise
            # A driver/runtime can look valid but still fail when CTranslate2
            # creates the model. Preserve analysis by retrying on CPU.
            diagnostics.log_failure(
                f"GPU transcription model failed: model={selected_model} compute_type={compute_type}",
                exc,
            )
            detail = " ".join(str(exc).split())[:180]
            progress(
                progress_start,
                f"GPU model failed{(': ' + detail) if detail else ''} - retrying transcription on CPU",
            )
            device, compute_type = "cpu", "int8"
            model_key = (selected_model, device, compute_type)
            _transcription_models[model_key] = WhisperModel(selected_model, device=device, compute_type=compute_type)
    model = _transcription_models[model_key]
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


def build_candidates(
    parts: list[dict],
    duration: float,
    boundaries: list[float] | None = None,
    include_context: bool = True,
    context_window_seconds: float = 12.0,
) -> list[dict]:
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
    adjusted = []
    for candidate in candidates:
        sentence_aligned = _smart_sentence_bounds(candidate, parts, duration)
        # A scene cut is useful for an already clean candidate, but it must not
        # move an intelligently aligned edge back into the middle of speech.
        if sentence_aligned.get("boundary_signals"):
            adjusted.append(_add_context(sentence_aligned, duration))
        else:
            adjusted.append(_snap_to_scene(_add_context(sentence_aligned, duration), boundaries or [], duration))
    return _attach_speech_context(adjusted, parts, window_seconds=context_window_seconds) if include_context else adjusted


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


_SENTENCE_ENDING = (".", "!", "?")
_PUNCHLINE_START = (
    "i wtedy", "a wtedy", "i nagle", "a nagle", "okazalo sie", "okazuje sie",
    "a najlepsze", "ale najlepsze", "a najgorsze", "ale najgorsze", "wiec jednak",
)


def _ends_sentence(part: dict) -> bool:
    return str(part.get("text") or "").strip().endswith(_SENTENCE_ENDING)


def _continuous(previous: dict, following: dict, max_gap: float = 2.5) -> bool:
    return float(following["start"]) - float(previous["end"]) <= max_gap


def _starts_punchline(part: dict) -> bool:
    text = unicodedata.normalize("NFKD", str(part.get("text") or "").strip().lower()).encode("ascii", "ignore").decode("ascii")
    return any(text.startswith(prefix) for prefix in _PUNCHLINE_START)


def _smart_sentence_bounds(candidate: dict, parts: list[dict], duration: float) -> dict:
    """Extend a candidate only across one unfinished thought or a clear punchline.

    Whisper parts often end mid-sentence.  We retain the reviewable candidate
    length but allow up to 15 extra seconds when that prevents a clipped start
    or missing ending.
    """
    if not parts:
        return candidate
    maximum = min(duration, settings.segment_max_seconds + 15)
    first = next((index for index, part in enumerate(parts) if float(part["end"]) >= float(candidate["start"])), 0)
    last = max(index for index, part in enumerate(parts) if float(part["start"]) <= float(candidate["end"]))
    original_first, original_last = first, last
    notes: list[str] = []

    while first > 0:
        previous, current = parts[first - 1], parts[first]
        if _ends_sentence(previous) or not _continuous(previous, current):
            break
        if float(parts[last]["end"]) - float(previous["start"]) > maximum:
            break
        first -= 1
    if first != original_first:
        notes.append("start aligned to sentence")

    # First finish an unfinished sentence, then optionally include one nearby
    # punchline that begins immediately after it.
    while last < len(parts) - 1 and not _ends_sentence(parts[last]):
        following = parts[last + 1]
        if not _continuous(parts[last], following) or float(following["end"]) - float(parts[first]["start"]) > maximum:
            break
        last += 1
    if last != original_last:
        notes.append("end aligned to sentence")

    if last < len(parts) - 1 and _ends_sentence(parts[last]):
        following = parts[last + 1]
        if _starts_punchline(following) and _continuous(parts[last], following) and float(following["end"]) - float(parts[first]["start"]) <= maximum:
            last += 1
            while last < len(parts) - 1 and not _ends_sentence(parts[last]):
                next_part = parts[last + 1]
                if not _continuous(parts[last], next_part) or float(next_part["end"]) - float(parts[first]["start"]) > maximum:
                    break
                last += 1
            notes.append("extended to punchline")

    adjusted = _candidate(parts[first:last + 1])
    adjusted["boundary_signals"] = notes
    return adjusted


def _attach_speech_context(candidates: list[dict], parts: list[dict], window_seconds: float = 12.0) -> list[dict]:
    """Store a compact setup and follow-up transcript around each candidate."""
    for candidate in candidates:
        start, end = float(candidate["start"]), float(candidate["end"])
        before_parts = [part["text"].strip() for part in parts if start - window_seconds <= float(part["end"]) <= start and part.get("text")]
        after_parts = [part["text"].strip() for part in parts if end <= float(part["start"]) <= end + window_seconds and part.get("text")]
        before = " ".join(before_parts)[-700:]
        after = " ".join(after_parts)[:700]
        context_score, context_signals = assess_context(candidate.get("text", ""), before, after)
        self_contained_score = assess_self_containment(candidate.get("text", ""), before, after)
        candidate["context_before"] = before
        candidate["context_after"] = after
        candidate["context_score"] = context_score
        candidate["self_contained_score"] = self_contained_score
        candidate["context_signals"] = context_signals
    return candidates


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


def audio_energy_windows(audio_path: Path, window_seconds: float = 0.25) -> np.ndarray:
    """Return a compact RMS timeline used to compare game and microphone timing."""
    with wave.open(str(audio_path), "rb") as handle:
        sample_rate = handle.getframerate()
        window_frames = max(1, round(sample_rate * window_seconds))
        energies = []
        while True:
            raw = handle.readframes(window_frames)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            energies.append(float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0)
    return np.asarray(energies, dtype=np.float32)


def dynamic_audio_scores(energies: np.ndarray, candidates: list[dict], window_seconds: float = 0.25) -> list[int]:
    """Find unusual energy changes in a track; this alone is not a clip-quality boost."""
    if len(energies) < 3:
        return [0] * len(candidates)
    baseline, high, peak = np.percentile(energies, [55, 88, 98])
    spread = max(1.0, peak - high)
    scores = []
    for candidate in candidates:
        left = max(0, int(candidate["start"] / window_seconds))
        right = min(len(energies), max(left + 1, int(np.ceil(candidate["end"] / window_seconds))))
        section = energies[left:right]
        if not len(section):
            scores.append(0)
            continue
        peak_bonus = max(0.0, float(section.max()) - high) / spread
        mean_bonus = max(0.0, float(section.mean()) - baseline) / max(1.0, high - baseline)
        scores.append(max(0, min(16, round(peak_bonus * 12 + mean_bonus * 4))))
    return scores


def game_reaction_scores(game_energies: np.ndarray, microphone_energies: np.ndarray, candidates: list[dict], window_seconds: float = 0.25, lead_seconds: float = 0.0) -> list[int]:
    """Reward a game/stream event only when a stronger microphone response follows it.

    A roar, alert or jumpscare by itself gets no boost. A dynamic game sound must
    happen first and be followed within three seconds by a clear rise in the
    microphone track.
    """
    if len(game_energies) < 4 or len(microphone_energies) < 4:
        return [0] * len(candidates)
    length = min(len(game_energies), len(microphone_energies))
    game_energies, microphone_energies = game_energies[:length], microphone_energies[:length]
    _game_base, game_high, game_peak = np.percentile(game_energies, [55, 92, 99])
    microphone_base, microphone_high, microphone_peak = np.percentile(microphone_energies, [55, 90, 99])
    game_spread = max(1.0, game_peak - game_high)
    microphone_spread = max(1.0, microphone_peak - microphone_high)
    scores: list[int] = []
    response_limit = max(1, round(3.0 / window_seconds))
    for candidate in candidates:
        response_left = max(0, int(candidate["start"] / window_seconds))
        left = max(0, response_left - round(lead_seconds / window_seconds))
        right = min(length, max(left + 1, int(np.ceil(candidate["end"] / window_seconds))))
        game_section = game_energies[left:right]
        if not len(game_section) or float(game_section.max()) < game_high:
            scores.append(0)
            continue
        best = 0.0
        # Checking a few strongest events avoids rewarding a loud sound that is
        # unrelated to a later reaction in the same candidate.
        event_indices = np.argsort(game_section)[-3:][::-1] + left
        for event_index in event_indices:
            event_energy = float(game_energies[event_index])
            if event_energy < game_high:
                continue
            response_start = event_index + 1
            response_end = min(right, response_start + response_limit)
            if response_end <= response_start or response_end <= response_left:
                continue
            response_peak = float(microphone_energies[response_start:response_end].max())
            before_start = max(left, event_index - 4)
            before_level = float(np.median(microphone_energies[before_start:event_index + 1]))
            if response_peak < microphone_high or response_peak <= before_level * 1.08:
                continue
            game_strength = max(0.0, (event_energy - game_high) / game_spread)
            response_strength = max(0.0, (response_peak - microphone_high) / microphone_spread)
            rise_strength = max(0.0, (response_peak - before_level) / microphone_spread)
            best = max(best, game_strength * 5 + response_strength * 7 + rise_strength * 6)
        scores.append(max(0, min(16, round(best))))
    return scores


def voice_led_content(tags: list[str]) -> bool:
    """Opinions and humour should prefer expressive delivery over game loudness."""
    return any(
        tag in {"humor", "gniew", "zaskoczenie"}
        or tag.startswith("wyra")
        or tag.startswith("rado")
        or tag.startswith("zło")
        for tag in tags
    )


def _gameplay_crop(frame: np.ndarray, gameplay_rect: tuple[float, float, float, float] | None) -> np.ndarray:
    """Use the calibrated gameplay area and exclude facecam/stream overlay space."""
    if not gameplay_rect:
        return frame
    x, y, width, height = gameplay_rect
    source_height, source_width = frame.shape[:2]
    left = max(0, min(source_width - 1, round(float(x) * source_width)))
    top = max(0, min(source_height - 1, round(float(y) * source_height)))
    right = max(left + 1, min(source_width, round((float(x) + float(width)) * source_width)))
    bottom = max(top + 1, min(source_height, round((float(y) + float(height)) * source_height)))
    crop = frame[top:bottom, left:right]
    return crop if crop.shape[0] >= 24 and crop.shape[1] >= 24 else frame


def _coherent_motion_metrics(before: np.ndarray, after: np.ndarray, cv2) -> tuple[float, float, float]:
    """Measure large, connected game motion rather than small overlay particles."""
    difference = cv2.absdiff(before, after)
    smoothed = cv2.GaussianBlur(difference, (5, 5), 0)
    movement = float(np.mean(difference))
    mask = (smoothed >= 18).astype(np.uint8)
    # Fill ordinary gameplay motion into a coherent region. Independent alert
    # emotes remain small components even when many appear on screen.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    _count, _labels, stats, _centres = cv2.connectedComponentsWithStats(mask, connectivity=8)
    active_ratio = float(np.mean(mask > 0))
    largest_ratio = float(np.max(stats[1:, cv2.CC_STAT_AREA]) / mask.size) if len(stats) > 1 else 0.0
    return movement, active_ratio, largest_ratio


def visual_interest_scores(
    video_path: Path,
    records: list[dict],
    limit: int = 120,
    gameplay_rect: tuple[float, float, float, float] | None = None,
) -> dict[str, int]:
    """Score only coherent gameplay movement, excluding overlays and facecam."""
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
                gameplay = _gameplay_crop(frame, gameplay_rect)
                gray = cv2.cvtColor(cv2.resize(gameplay, (200, 112)), cv2.COLOR_BGR2GRAY).astype(np.uint8)
                frames.append(gray)
            if len(frames) < 2:
                continue
            comparisons = [_coherent_motion_metrics(frames[index], frames[index + 1], cv2) for index in range(len(frames) - 1)]
            movement = float(np.mean([item[0] for item in comparisons]))
            active_ratio = float(np.mean([item[1] for item in comparisons]))
            coherent_ratio = float(np.mean([item[2] for item in comparisons]))
            # Alerts such as falling emotes are visually busy but are made of
            # many tiny, disconnected components. They must not be tagged as
            # gameplay action or earn a ranking bonus.
            if coherent_ratio < 0.035 or (coherent_ratio < 0.065 and active_ratio < 0.14):
                continue
            score = max(0, min(16, round(
                max(0.0, movement - 10) * 0.24
                + max(0.0, active_ratio - 0.08) * 32
                + max(0.0, coherent_ratio - 0.03) * 62
            )))
            if score:
                scores[record["id"]] = score
    finally:
        capture.release()
    return scores


def visual_reading_scores(video_path: Path, records: list[dict], limit: int = 120) -> dict[str, int]:
    """Flag static, text-heavy game screens such as notes and objective lists.

    This intentionally does not use OCR: it is a lightweight visual signal that
    needs to agree with the speech-based reading heuristics before a clip is
    suppressed.  Cropping the central gameplay area avoids the facecam and
    chat overlays that are common in recordings.
    """
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
            moments = [record["start"] + duration * 0.42, record["start"] + duration * 0.70, max(record["start"] + 0.25, record["end"] - 0.15)]
            frames, edge_densities, bright_ratios = [], [], []
            for moment in moments:
                capture.set(cv2.CAP_PROP_POS_MSEC, moment * 1000)
                ok, frame = capture.read()
                if not ok:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                height, width = gray.shape
                center = gray[int(height * 0.13):int(height * 0.86), int(width * 0.18):int(width * 0.82)]
                small = cv2.resize(center, (240, 135)).astype(np.float32)
                edges = cv2.Canny(small.astype(np.uint8), 60, 150)
                frames.append(small)
                edge_densities.append(float(np.mean(edges > 0)))
                bright_ratios.append(float(np.mean(small > 145)))
            if len(frames) < 3:
                continue
            terminal_motion = float(np.mean(np.abs(frames[-1] - frames[1])))
            text_density = float(np.mean(edge_densities[1:]))
            bright_ratio = float(np.mean(bright_ratios[1:]))
            if terminal_motion <= 8.0 and text_density >= 0.065:
                score = 6
                if bright_ratio >= 0.025:
                    score += 3
                if text_density >= 0.095:
                    score += 2
                scores[record["id"]] = min(12, score)
    finally:
        capture.release()
    return scores


def analyse(video_id: str, report: Progress) -> None:
    video = db.row("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Nie znaleziono nagrania")
    source = Path(video["path"])
    analysis_mode = str(video.get("analysis_mode") or "default")
    if analysis_mode not in {"fast", "default", "extended"}:
        analysis_mode = "default"
    fast_mode = analysis_mode == "fast"
    extended_mode = analysis_mode == "extended"
    audio_defaults = db.row("SELECT * FROM analysis_audio_defaults WHERE id=1") or {}
    export_defaults = db.row("SELECT game_x, game_y, game_width, game_height FROM export_defaults WHERE id=1") or {}
    gameplay_rect = (
        float(export_defaults.get("game_x", 0.12)),
        float(export_defaults.get("game_y", 0.08)),
        float(export_defaults.get("game_width", 0.76)),
        float(export_defaults.get("game_height", 0.84)),
    )
    # Downloaded YouTube/Twitch material normally contains one mixed audio
    # stream.  Keep it independent from the user's multi-track OBS defaults.
    mode = "single" if video.get("source_url") else audio_defaults.get("mode", "single")
    report(5, "Reading video metadata")
    duration = duration_seconds(source)
    available_audio_tracks = audio_track_count(source)
    if available_audio_tracks < 1:
        raise ValueError("The recording does not contain an audio track.")

    requested_transcript_track = int(
        audio_defaults.get("single_track", 1) if mode == "single" else audio_defaults.get("microphone_track", 2)
    )
    transcript_track = requested_transcript_track if requested_transcript_track <= available_audio_tracks else 1
    skipped_tracks: list[str] = []
    if transcript_track != requested_transcript_track:
        skipped_tracks.append(f"transcription track {requested_transcript_track}")

    event_tracks: list[tuple[str, int]] = []
    if not fast_mode and mode == "split":
        configured_events = []
        if audio_defaults.get("use_all_sounds"):
            configured_events.append(("all-sounds event", int(audio_defaults.get("all_sounds_track", 1))))
        if audio_defaults.get("use_game"):
            configured_events.append(("game-audio event", int(audio_defaults.get("game_track", 3))))
        for label, track in configured_events:
            if track > available_audio_tracks:
                skipped_tracks.append(f"{label} track {track}")
            elif track == transcript_track:
                # This is the transcription source, not a separate game/event source.
                skipped_tracks.append(f"{label} track {track} (same as transcription)")
            else:
                event_tracks.append((label, track))

    dedicated_microphone_track = mode == "split" and requested_transcript_track == transcript_track
    with db.connection() as con:
        con.execute("UPDATE videos SET duration_seconds=?, status='processing', transcript_audio_track=?, audio_analysis_mode=?, analysis_mode=?, updated_at=? WHERE id=?", (duration, transcript_track, mode, analysis_mode, db.now(), video_id))

    audio_path = settings.work_dir / f"{video_id}.wav"
    if skipped_tracks:
        report(8, f"Using available audio track {transcript_track}; skipped unavailable/separate tracks")
    report(10, "Extracting microphone audio" if mode == "split" else "Extracting audio")
    extract_audio(source, audio_path, transcript_track)
    transcript = transcribe(audio_path, report, duration, model_name="small" if fast_mode else None)
    boundaries: list[float] = []
    if fast_mode:
        report(66, "Fast mode: creating text candidates")
    else:
        report(66, "Detecting scene changes")
        boundaries = detect_boundaries(source)
    report(72, "Creating clip candidates")
    candidates = build_candidates(
        transcript,
        duration,
        boundaries,
        include_context=not fast_mode,
        context_window_seconds=20.0 if extended_mode else 12.0,
    )
    microphone_energies = audio_energy_windows(audio_path) if not fast_mode else np.asarray([], dtype=np.float32)
    # A single mixed track cannot distinguish a loud game event from the voice.
    # In that legacy mode we keep audio neutral instead of inventing a reaction.
    microphone_expression_scores = dynamic_audio_scores(microphone_energies, candidates) if not fast_mode and dedicated_microphone_track else [0] * len(candidates)
    event_energies: list[tuple[str, np.ndarray]] = []
    temporary_audio: list[Path] = []
    for label, track in event_tracks:
        event_path = audio_path if track == transcript_track else settings.work_dir / f"{video_id}-track{track}-{uuid.uuid4()}.wav"
        if event_path != audio_path:
            report(74, f"Reading {label}")
            extract_audio(source, event_path, track, sample_rate=8000)
            temporary_audio.append(event_path)
        event_energies.append((label, audio_energy_windows(event_path)))
    # Prefer the clean game track. The all-sounds mix is a fallback for users
    # who only have one combined stream track.
    reaction_sources = [item for item in event_energies if item[0] == "game-audio event"] or event_energies
    reaction_scores = [0] * len(candidates)
    for _label, energies in reaction_sources:
        reaction_scores = [max(current, incoming) for current, incoming in zip(reaction_scores, game_reaction_scores(energies, microphone_energies, candidates, lead_seconds=12.0))]
    vectors = embed_texts([item["text"] or "bez wypowiedzi" for item in candidates])
    records: list[dict] = []
    for index, (candidate, vector) in enumerate(zip(candidates, vectors)):
        keywords = [word.strip(".,!?;:").lower() for word in candidate["text"].split() if len(word.strip(".,!?;:")) >= 6][:12]
        tags = infer_tags(candidate["text"], vector)
        quality_score, quality_signals, reading_likelihood = assess_clip_quality(candidate["text"], candidate.get("words", []), candidate["start"], candidate["end"], tags)
        quality_signals.extend(candidate.get("boundary_signals") or [])
        logical_sense_score = assess_logical_sense(candidate["text"])
        context_score = int(candidate.get("context_score") or 50)
        self_contained_score = int(candidate.get("self_contained_score") or 50)
        context_signals = candidate.get("context_signals") or []
        # A task, note or NPC line can be grammatically complete, but is not a
        # standalone creator moment.  Do not label it as such before chat has
        # a chance to prove that a short viewer-comment reply was interesting.
        if reading_likelihood >= 0.48:
            logical_sense_score = min(logical_sense_score, 35)
            context_score = min(context_score, 35)
            self_contained_score = min(self_contained_score, 35)
        game_reaction_score = reaction_scores[index]
        voice_expression_score = microphone_expression_scores[index]
        moment_reaction_score, moment_reaction_stage = score_moment_reaction(game_reaction_score)
        event_score = 0
        if game_reaction_score >= 7:
            tags = list(dict.fromkeys(tags + [GAME_REACTION_TAG]))
            event_score = game_reaction_score
            quality_score = min(99, quality_score + min(10, game_reaction_score))
            quality_signals.append("game sound followed by microphone reaction")
        elif voice_led_content(tags) and voice_expression_score >= 7:
            event_score = voice_expression_score
            quality_score = min(99, quality_score + min(8, voice_expression_score))
            quality_signals.append("expressive microphone delivery")
        if context_score >= 72:
            quality_score = min(99, quality_score + 3)
            quality_signals.extend(context_signals)
        elif context_score <= 38:
            quality_score = max(1, quality_score - 6)
            quality_signals.extend(context_signals)
        if reading_likelihood >= 0.48:
            tags = list(dict.fromkeys(tags + ["reading"]))
        tags = enrich_tags(
            tags,
            logical_sense_score=logical_sense_score,
            reading_likelihood=reading_likelihood,
            game_reaction_score=game_reaction_score,
            voice_expression_score=voice_expression_score,
            context_score=context_score,
            self_contained_score=self_contained_score,
            moment_reaction_score=moment_reaction_score,
            moment_reaction_stage=moment_reaction_stage,
        )
        records.append({"id": str(uuid.uuid4()), "start": candidate["start"], "end": candidate["end"], "text": candidate["text"], "words": candidate.get("words", []), "vector": vector, "keywords": keywords, "tags": tags, "quality_score": quality_score, "quality_signals": quality_signals, "logical_sense_score": logical_sense_score, "context_score": context_score, "self_contained_score": self_contained_score, "extended_completeness_score": -1, "context_before": candidate.get("context_before", ""), "context_after": candidate.get("context_after", ""), "reading_likelihood": reading_likelihood, "audio_event_score": event_score, "game_reaction_score": game_reaction_score, "voice_expression_score": voice_expression_score, "moment_reaction_score": moment_reaction_score, "moment_reaction_stage": moment_reaction_stage, "duplicate_group": ""})
    assign_duplicate_groups(records)
    visual_scores: dict[str, int] = {}
    reading_screens: dict[str, int] = {}
    if fast_mode:
        report(82, "Fast mode: skipping visual and game-audio checks")
    elif extended_mode:
        # Extended mode spends its extra work on the thing that makes a clip
        # useful on its own: a complete thought. Visual analysis remains the
        # same calibrated gameplay scan used by the default workflow.
        report(82, "Extended mode: checking complete thoughts and context")
        visual_scores = visual_interest_scores(source, records, gameplay_rect=gameplay_rect)
        reading_screens = visual_reading_scores(source, records)
    else:
        report(82, "Checking visual action and text-heavy game screens")
        visual_scores = visual_interest_scores(source, records, gameplay_rect=gameplay_rect)
        reading_screens = visual_reading_scores(source, records)
    for record in records:
        reading_screen = reading_screens.get(record["id"], 0)
        if reading_screen:
            # A static screen full of text confirms that apparently coherent
            # speech is being read from the game rather than authored live.
            record["reading_likelihood"] = min(1.0, max(record["reading_likelihood"], 0.52) + reading_screen * 0.035)
            record["quality_score"] = max(1, record["quality_score"] - 28)
            record["logical_sense_score"] = min(record["logical_sense_score"], 35)
            record["context_score"] = min(record["context_score"], 35)
            record["self_contained_score"] = min(record["self_contained_score"], 35)
            record["quality_signals"].append("static text-heavy game screen")
            record["tags"] = list(dict.fromkeys(record["tags"] + ["reading"]))
        record["vision_score"] = 0 if reading_screen else visual_scores.get(record["id"], 0)
        if record["vision_score"] >= 7:
            record["quality_signals"].append("visual action")
        if extended_mode:
            completeness = assess_extended_completeness(
                record["text"], record["context_before"], record["context_after"], record["quality_signals"],
            )
            record["extended_completeness_score"] = completeness
            if completeness >= 76:
                record["quality_score"] = min(99, record["quality_score"] + 6)
                record["quality_signals"].append("extended complete-thought verification")
            elif completeness <= 43:
                record["quality_score"] = max(1, record["quality_score"] - 14)
                record["quality_signals"].append("extended incomplete-thought warning")
        record["tags"] = enrich_tags(
            record["tags"],
            logical_sense_score=record["logical_sense_score"],
            reading_likelihood=record["reading_likelihood"],
            game_reaction_score=record["game_reaction_score"],
            voice_expression_score=record["voice_expression_score"],
            vision_score=record["vision_score"],
            context_score=record["context_score"],
            self_contained_score=record["self_contained_score"],
            moment_reaction_score=record["moment_reaction_score"],
            moment_reaction_stage=record["moment_reaction_stage"],
        )
        record["short_potential_score"], record["short_potential_signals"] = assess_short_potential(
            record["text"], record["start"], record["end"], record["tags"],
            quality_score=record["quality_score"], reading_likelihood=record["reading_likelihood"],
            logical_sense_score=record["logical_sense_score"], context_score=record["context_score"],
            self_contained_score=record["self_contained_score"], extended_completeness_score=record["extended_completeness_score"],
            game_reaction_score=record["game_reaction_score"], voice_expression_score=record["voice_expression_score"],
            moment_reaction_score=record["moment_reaction_score"],
        )
    with db.connection() as con:
        con.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
        for record in records:
            con.execute(
                "INSERT INTO segments (id, video_id, start_seconds, end_seconds, transcript, keywords, tags, word_timestamps, embedding, quality_score, quality_signals, short_potential_score, short_potential_signals, logical_sense_score, context_score, self_contained_score, extended_completeness_score, context_before, context_after, reading_likelihood, audio_event_score, game_reaction_score, voice_expression_score, moment_reaction_score, moment_reaction_stage, vision_score, duplicate_group, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["id"], video_id, record["start"], record["end"], record["text"], json.dumps(record["keywords"], ensure_ascii=False), json.dumps(record["tags"], ensure_ascii=False), json.dumps(record["words"], ensure_ascii=False), json.dumps(record["vector"]), record["quality_score"], json.dumps(record["quality_signals"]), record["short_potential_score"], json.dumps(record["short_potential_signals"]), record["logical_sense_score"], record["context_score"], record["self_contained_score"], record["extended_completeness_score"], record["context_before"], record["context_after"], record["reading_likelihood"], record["audio_event_score"], record["game_reaction_score"], record["voice_expression_score"], record["moment_reaction_score"], record["moment_reaction_stage"], record["vision_score"], record["duplicate_group"], db.now()),
            )
        con.execute("UPDATE videos SET status='ready', updated_at=? WHERE id=?", (db.now(), video_id))
    # A chat transcript may have been imported before a reanalysis. Reapply it
    # after replacing segments so its delayed reaction score is never stale.
    apply_chat_reactions(video_id)
    audio_path.unlink(missing_ok=True)
    for path in temporary_audio:
        path.unlink(missing_ok=True)
    mode_label = {"fast": "Fast scan", "default": "Default analysis", "extended": "Extended analysis"}[analysis_mode]
    report(100, f"{mode_label} ready: {len(candidates)} candidates")


def import_reference_files(collection_id: str, files: list[Path], report: Progress, source_keys: dict[Path, str] | None = None) -> int:
    """Transcribe selected local or downloaded reference clips into a collection."""
    files = [item.resolve() for item in files if item.is_file()]
    if not files:
        raise ValueError("No supported reference video files were found")

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
                    (str(uuid.uuid4()), collection_id, (source_keys or {}).get(source, str(source)), source.name, transcript, json.dumps(embedding), db.now()),
                )
            imported += 1
        finally:
            audio_path.unlink(missing_ok=True)
        report(int(index / len(files) * 96), f"Imported {index}/{len(files)} references")
    report(100, f"Ready: {imported} reference clips")
    return imported


def import_reference_folder(collection_id: str, folder_path: str, include_subfolders: bool, report: Progress) -> int:
    """Transcribe all supported local reference clips in a folder."""
    root = Path(folder_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Reference folder does not exist or is not a folder")
    allowed = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
    iterator = root.rglob("*") if include_subfolders else root.glob("*")
    return import_reference_files(collection_id, [item for item in iterator if item.is_file() and item.suffix.lower() in allowed], report)
