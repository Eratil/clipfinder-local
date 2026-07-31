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
from app.services.discovery import assign_duplicate_groups
from app.services.media import audio_track_count, duration_seconds, extract_audio, extract_audio_range
from app.services.scenes import detect_boundaries
from app.services.tagging import GAME_REACTION_TAG, assess_clip_quality, assess_context, assess_logical_sense, assess_self_containment, enrich_tags, infer_tags, score_moment_reaction
from app.services.chat import apply_chat_reactions

Progress = Callable[[int, str], None]
_transcription_model = None


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
) -> list[dict]:
    global _transcription_model
    if _transcription_model is None:
        from faster_whisper import WhisperModel
        device, compute_type, fallback_reason = transcription_runtime()
        if fallback_reason:
            progress(progress_start, "CUDA unavailable - switching transcription to CPU")
        else:
            progress(progress_start, f"Loading transcription model on {device.upper()}")
        try:
            _transcription_model = WhisperModel(settings.whisper_model, device=device, compute_type=compute_type)
        except Exception:
            if device != "cuda":
                raise
            # A driver/runtime can look valid but still fail when CTranslate2
            # creates the model. Preserve analysis by retrying on CPU.
            progress(progress_start, "GPU model failed - retrying transcription on CPU")
            _transcription_model = WhisperModel(settings.whisper_model, device="cpu", compute_type="int8")
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
    adjusted = []
    for candidate in candidates:
        sentence_aligned = _smart_sentence_bounds(candidate, parts, duration)
        # A scene cut is useful for an already clean candidate, but it must not
        # move an intelligently aligned edge back into the middle of speech.
        if sentence_aligned.get("boundary_signals"):
            adjusted.append(_add_context(sentence_aligned, duration))
        else:
            adjusted.append(_snap_to_scene(_add_context(sentence_aligned, duration), boundaries or [], duration))
    return _attach_speech_context(adjusted, parts)


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
    audio_defaults = db.row("SELECT * FROM analysis_audio_defaults WHERE id=1") or {}
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
    if mode == "split":
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
        con.execute("UPDATE videos SET duration_seconds=?, status='processing', transcript_audio_track=?, audio_analysis_mode=?, updated_at=? WHERE id=?", (duration, transcript_track, mode, db.now(), video_id))

    audio_path = settings.work_dir / f"{video_id}.wav"
    if skipped_tracks:
        report(8, f"Using available audio track {transcript_track}; skipped unavailable/separate tracks")
    report(10, "Extracting audio")
    extract_audio(source, audio_path, transcript_track)
    transcript = transcribe(audio_path, report, duration)
    report(66, "Detecting scene changes")
    boundaries = detect_boundaries(source)
    report(72, "Creating clip candidates")
    candidates = build_candidates(transcript, duration, boundaries)
    microphone_energies = audio_energy_windows(audio_path)
    # A single mixed track cannot distinguish a loud game event from the voice.
    # In that legacy mode we keep audio neutral instead of inventing a reaction.
    microphone_expression_scores = dynamic_audio_scores(microphone_energies, candidates) if dedicated_microphone_track else [0] * len(candidates)
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
        records.append({"id": str(uuid.uuid4()), "start": candidate["start"], "end": candidate["end"], "text": candidate["text"], "words": candidate.get("words", []), "vector": vector, "keywords": keywords, "tags": tags, "quality_score": quality_score, "quality_signals": quality_signals, "logical_sense_score": logical_sense_score, "context_score": context_score, "self_contained_score": self_contained_score, "context_before": candidate.get("context_before", ""), "context_after": candidate.get("context_after", ""), "reading_likelihood": reading_likelihood, "audio_event_score": event_score, "game_reaction_score": game_reaction_score, "voice_expression_score": voice_expression_score, "moment_reaction_score": moment_reaction_score, "moment_reaction_stage": moment_reaction_stage, "duplicate_group": ""})
    assign_duplicate_groups(records)
    report(82, "Checking visual action and text-heavy game screens")
    visual_scores = visual_interest_scores(source, records)
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
    with db.connection() as con:
        con.execute("DELETE FROM segments WHERE video_id = ?", (video_id,))
        for record in records:
            con.execute(
                "INSERT INTO segments (id, video_id, start_seconds, end_seconds, transcript, keywords, tags, word_timestamps, embedding, quality_score, quality_signals, logical_sense_score, context_score, self_contained_score, context_before, context_after, reading_likelihood, audio_event_score, game_reaction_score, voice_expression_score, moment_reaction_score, moment_reaction_stage, vision_score, duplicate_group, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["id"], video_id, record["start"], record["end"], record["text"], json.dumps(record["keywords"], ensure_ascii=False), json.dumps(record["tags"], ensure_ascii=False), json.dumps(record["words"], ensure_ascii=False), json.dumps(record["vector"]), record["quality_score"], json.dumps(record["quality_signals"]), record["logical_sense_score"], record["context_score"], record["self_contained_score"], record["context_before"], record["context_after"], record["reading_likelihood"], record["audio_event_score"], record["game_reaction_score"], record["voice_expression_score"], record["moment_reaction_score"], record["moment_reaction_stage"], record["vision_score"], record["duplicate_group"], db.now()),
            )
        con.execute("UPDATE videos SET status='ready', updated_at=? WHERE id=?", (db.now(), video_id))
    # A chat transcript may have been imported before a reanalysis. Reapply it
    # after replacing segments so its delayed reaction score is never stale.
    apply_chat_reactions(video_id)
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
