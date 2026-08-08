import json
import hashlib
import importlib.metadata
import threading
import unicodedata
import uuid
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app import database as db
from app.config import settings
from app.services.embeddings import EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_REVISION, embed_texts
from app.services.cuda_runtime import cuda12_runtime_error
from app.services import diagnostics
from app.services.discovery import assign_duplicate_groups
from app.services.feature_graph import recompute_segment_features
from app.services.media import audio_track_count, duration_seconds, extract_audio, extract_audio_range
from app.services.pipeline_cache import PipelineCache, canonical_json, fingerprint_source_file
from app.services.model_catalog import whisper_identity, whisper_model_source
from app.services.scenes import detect_boundaries
from app.services.tag_taxonomy import GAME_REACTION_MIN_SCORE
from app.services.tagging import infer_tags
from app.services.chat import apply_chat_reactions
from app.services.analysis_store import (
    persist_analysis_results,
    start_analysis_run,
    update_analysis_run_inputs,
)

Progress = Callable[[int, str], None]
_transcription_models: dict[tuple[str, str, str, str], object] = {}
_failed_transcription_runtimes: set[tuple[str, str, str, str]] = set()
_transcription_lock = threading.RLock()

TRANSCRIPTION_CACHE_VERSION = "1"
MEDIA_METADATA_CACHE_VERSION = "1"
SCENE_BOUNDARY_CACHE_VERSION = "1"
AUDIO_FEATURE_CACHE_VERSION = "1"
EMBEDDING_CACHE_VERSION = "1"
VISUAL_INTEREST_CACHE_VERSION = "1"
VISUAL_READING_CACHE_VERSION = "1"
REFERENCE_AUDIO_TRACK = 1
REFERENCE_AUDIO_SAMPLE_RATE = 16000


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


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


def resolved_transcription_runtime(model_name: str) -> tuple[str, str, str | None]:
    """Return the runtime this process can currently use for one model."""
    device, compute_type, fallback_reason = transcription_runtime()
    model_id, revision = whisper_identity(model_name)
    if (model_id, revision or "custom", device, compute_type) in _failed_transcription_runtimes:
        return "cpu", "int8", "CUDA model initialization failed earlier in this process"
    return device, compute_type, fallback_reason


def transcription_cache_parameters(model_name: str, device: str, compute_type: str) -> dict:
    model_id, revision = whisper_identity(model_name)
    return {
        "stage_version": TRANSCRIPTION_CACHE_VERSION,
        "model": model_name,
        "model_id": model_id,
        "model_revision": revision or "custom",
        "device": device,
        "compute_type": compute_type,
        "faster_whisper_version": _distribution_version("faster-whisper"),
        "ctranslate2_version": _distribution_version("ctranslate2"),
        "vad_filter": True,
        "word_timestamps": True,
        "language": "auto",
        "audio_extract_contract": "ffmpeg-pcm-s16le-mono-v1",
        "sample_rate": 16000,
        "channels": 1,
    }


def transcribe(
    audio_path: Path,
    progress: Progress,
    duration: float | None = None,
    progress_start: int = 18,
    progress_end: int = 62,
    model_name: str | None = None,
    runtime_info: dict | None = None,
) -> list[dict]:
    # Whisper model creation and the returned segment iterator both touch the
    # same native CPU/GPU runtime. Serialize the whole operation: keeping only
    # construction under a lock would still allow concurrent iteration over
    # one model instance and can lead to native crashes or GPU OOM errors.
    with _transcription_lock:
        return _transcribe_locked(
            audio_path,
            progress,
            duration,
            progress_start,
            progress_end,
            model_name,
            runtime_info,
        )


def _transcribe_locked(
    audio_path: Path,
    progress: Progress,
    duration: float | None,
    progress_start: int,
    progress_end: int,
    model_name: str | None,
    runtime_info: dict | None,
) -> list[dict]:
    selected_model = model_name or settings.whisper_model
    device, compute_type, fallback_reason = resolved_transcription_runtime(selected_model)
    model_id, model_revision = whisper_identity(selected_model)
    last_progress = progress_start
    resolved_model_source: str | None = None

    def resolve_model_source() -> str:
        nonlocal resolved_model_source
        if resolved_model_source is None:
            resolved_model_source = whisper_model_source(selected_model)
        return resolved_model_source

    def model_for(target_device: str, target_compute_type: str):
        key = (model_id, model_revision or "custom", target_device, target_compute_type)
        if key not in _transcription_models:
            from faster_whisper import WhisperModel

            # Resolve/download the model outside the GPU fallback handler. A
            # network or model-file error is not evidence of a broken CUDA
            # runtime and must not poison the process-wide failed-runtime set.
            source = resolve_model_source()
            _transcription_models[key] = WhisperModel(
                source, device=target_device, compute_type=target_compute_type,
            )
        return key, _transcription_models[key]

    def consume(model) -> list[dict]:
        nonlocal last_progress
        parts, _info = model.transcribe(str(audio_path), vad_filter=True, word_timestamps=True)
        result: list[dict] = []
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
        return result

    if fallback_reason:
        progress(progress_start, "CUDA unavailable - switching transcription to CPU")
    else:
        progress(progress_start, f"Loading {selected_model} transcription model on {device.upper()}")

    gpu_key = (model_id, model_revision or "custom", device, compute_type)
    if gpu_key not in _transcription_models:
        # Do this before entering the CUDA-specific exception handler.
        resolve_model_source()
    try:
        model_key, model = model_for(device, compute_type)
        # faster-whisper returns a lazy iterator. CUDA/cuDNN/OOM errors can be
        # raised here during iteration rather than in WhisperModel(...).
        result = consume(model)
    except Exception as exc:
        if device != "cuda":
            raise
        diagnostics.log_failure(
            f"GPU transcription failed: model={selected_model} compute_type={compute_type}",
            exc,
        )
        detail = " ".join(str(exc).split())[:180]
        progress(
            last_progress,
            f"GPU transcription failed{(': ' + detail) if detail else ''} - retrying on CPU",
        )
        _failed_transcription_runtimes.add(gpu_key)
        device, compute_type = "cpu", "int8"
        model_key, model = model_for(device, compute_type)
        result = consume(model)

    if runtime_info is not None:
        runtime_info.clear()
        runtime_info.update(transcription_cache_parameters(selected_model, device, compute_type))
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
    """Attach raw neighbouring speech for the central feature graph."""
    for candidate in candidates:
        start, end = float(candidate["start"]), float(candidate["end"])
        before_parts = [part["text"].strip() for part in parts if start - window_seconds <= float(part["end"]) <= start and part.get("text")]
        after_parts = [part["text"].strip() for part in parts if end <= float(part["start"]) <= end + window_seconds and part.get("text")]
        candidate["context_before"] = " ".join(before_parts)[-700:]
        candidate["context_after"] = " ".join(after_parts)[:700]
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


def audio_window_features(audio_path: Path, window_seconds: float = 0.25) -> tuple[np.ndarray, np.ndarray]:
    """Return energy and a lightweight voiced-spectrum-change proxy per window.

    The second timeline is not a pitch detector.  It is a deliberately cheap
    zero-crossing proxy that helps distinguish a changing vocal delivery from
    speech with a nearly unchanged tone.  It keeps full-recording analysis
    local and avoids loading another heavyweight audio model.
    """
    with wave.open(str(audio_path), "rb") as handle:
        sample_rate = handle.getframerate()
        window_frames = max(1, round(sample_rate * window_seconds))
        energies = []
        tone_proxies = []
        while True:
            raw = handle.readframes(window_frames)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            if not len(samples):
                energies.append(0.0)
                tone_proxies.append(0.0)
                continue
            energies.append(float(np.sqrt(np.mean(samples * samples))))
            # Ignore low-level noise when measuring sign changes.  We do not
            # need an exact fundamental frequency here; only a stable signal
            # that changes when a speaker changes tone/timbre.
            gate = max(80.0, float(np.percentile(np.abs(samples), 55)) * 0.25)
            active = (np.abs(samples[:-1]) >= gate) & (np.abs(samples[1:]) >= gate)
            if not np.any(active):
                tone_proxies.append(0.0)
            else:
                crossings = (samples[:-1] * samples[1:] < 0) & active
                tone_proxies.append(float(np.count_nonzero(crossings)) / float(np.count_nonzero(active)))
    return np.asarray(energies, dtype=np.float32), np.asarray(tone_proxies, dtype=np.float32)


def audio_energy_windows(audio_path: Path, window_seconds: float = 0.25) -> np.ndarray:
    """Return a compact RMS timeline used to compare game and microphone timing."""
    energies: list[float] = []
    with wave.open(str(audio_path), "rb") as handle:
        window_frames = max(1, round(handle.getframerate() * window_seconds))
        while True:
            raw = handle.readframes(window_frames)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            energies.append(float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0)
    return np.asarray(energies, dtype=np.float32)


def _tempo_variation(candidate: dict) -> float | None:
    """Return relative speaking-rate variation from Whisper word timestamps."""
    words = [
        item for item in (candidate.get("words") or [])
        if item.get("start") is not None and item.get("end") is not None
    ]
    start, end = float(candidate["start"]), float(candidate["end"])
    duration = end - start
    if len(words) < 8 or duration < 5.0:
        return None
    # Count words in three comparable portions of the clip.  A moving rate is
    # more useful than raw words-per-minute, which varies naturally by person.
    edges = np.linspace(start, end, 4)
    rates = []
    for left, right in zip(edges[:-1], edges[1:]):
        count = sum(left <= float(item["start"]) < right for item in words)
        rates.append(count / max(0.5, right - left))
    if min(rates) <= 0:
        return None
    return float(np.std(rates) / max(0.1, np.mean(rates)))


def voice_delivery_scores(
    energies: np.ndarray,
    tone_proxies: np.ndarray,
    candidates: list[dict],
    window_seconds: float = 0.25,
) -> list[int]:
    """Score expressive vocal delivery, not raw loudness.

    Positive values require at least two independent changes: volume contour,
    speaking tempo, or the lightweight tone proxy.  Long spoken clips with all
    three dimensions flat receive a small negative value, used as a quality
    penalty rather than a misleading ``strong voice`` tag.
    """
    if len(energies) < 8 or len(tone_proxies) != len(energies):
        return [0] * len(candidates)
    speech_floor = max(40.0, float(np.percentile(energies, 55)) * 0.55)
    scores = []
    for candidate in candidates:
        left = max(0, int(candidate["start"] / window_seconds))
        right = min(len(energies), max(left + 1, int(np.ceil(candidate["end"] / window_seconds))))
        section = energies[left:right]
        tone_section = tone_proxies[left:right]
        voiced = section >= speech_floor
        if len(section) < 8 or int(np.count_nonzero(voiced)) < 5:
            scores.append(0)
            continue
        voiced_levels = np.log1p(section[voiced])
        # Log-energy span captures an intentional rise/fall without treating a
        # consistently loud microphone as expressive.
        level_span = float(np.percentile(voiced_levels, 85) - np.percentile(voiced_levels, 25))
        tone_values = tone_section[voiced]
        tone_cv = float(np.std(tone_values) / max(0.002, np.mean(tone_values))) if len(tone_values) >= 5 and float(np.mean(tone_values)) > 0 else 0.0
        tempo_cv = _tempo_variation(candidate)

        # Each dimension is deliberately conservative.  Normal intelligible
        # speech should be neutral; an expressive delivery changes in at least
        # two ways instead of merely being louder than the stream average.
        level_change = min(1.0, max(0.0, (level_span - 0.28) / 0.72))
        tone_change = min(1.0, max(0.0, (tone_cv - 0.13) / 0.22))
        tempo_change = None if tempo_cv is None else min(1.0, max(0.0, (tempo_cv - 0.24) / 0.46))
        dimensions = [level_change, tone_change] + ([] if tempo_change is None else [tempo_change])
        changed_dimensions = sum(value >= 0.48 for value in dimensions)
        combined = (level_change * 0.42) + (tone_change * 0.34) + ((tempo_change or 0.0) * 0.24)
        duration = float(candidate["end"]) - float(candidate["start"])
        # This is intentionally strict: changing volume and tone alone is
        # common in ordinary speech.  A clip earns the expressive label only
        # when tempo changes too, which makes it a useful attention signal.
        if changed_dimensions >= 3 and combined >= 0.70:
            scores.append(max(7, min(16, round(7 + combined * 9))))
        # The tone proxy is intentionally not used for the monotony verdict:
        # microphone noise/compression can make it look variable.  For a long
        # clip we instead require both the audible level contour and timestamp
        # based speaking tempo to stay flat.
        elif duration >= 15.0 and tempo_change is not None and level_change <= 0.30 and tempo_change <= 0.20:
            scores.append(-8)
        else:
            scores.append(0)
    return scores


def game_reaction_scores(game_energies: np.ndarray, microphone_energies: np.ndarray, candidates: list[dict], window_seconds: float = 0.25, lead_seconds: float = 0.0) -> list[int]:
    """Reward a game/stream event only when a stronger microphone response follows it.

    A roar, alert or jumpscare by itself gets no boost. A dynamic game sound must
    happen immediately before, or at the beginning of, a candidate and be
    followed by a clear rise in the microphone track. Video activity is never
    an input to this calculation.
    """
    if len(game_energies) < 4 or len(microphone_energies) < 4:
        return [0] * len(candidates)
    length = min(len(game_energies), len(microphone_energies))
    game_energies, microphone_energies = game_energies[:length], microphone_energies[:length]
    _game_base, game_high, game_peak = np.percentile(game_energies, [55, 95, 99])
    _microphone_base, microphone_high, microphone_peak = np.percentile(microphone_energies, [55, 93, 99])
    game_spread = max(1.0, game_peak - game_high)
    microphone_spread = max(1.0, microphone_peak - microphone_high)
    scores: list[int] = []
    response_limit = max(1, round(2.0 / window_seconds))
    event_after_start_limit = max(1, round(3.0 / window_seconds))
    for candidate in candidates:
        response_left = max(0, int(candidate["start"] / window_seconds))
        left = max(0, response_left - round(lead_seconds / window_seconds))
        right = min(length, max(left + 1, int(np.ceil(candidate["end"] / window_seconds))))
        # A long clip must not receive the tag merely because an unrelated game
        # sound happened much later. The event needs to be close to the start
        # of the spoken reaction.
        event_right = min(right, response_left + event_after_start_limit)
        game_section = game_energies[left:event_right]
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
            # The microphone rise itself must occur inside this candidate. An
            # event shortly before it is valid only when the reaction begins
            # in the clip, not in preceding context.
            response_start = max(response_left, event_index + 1)
            response_end = min(right, response_start + response_limit)
            if response_end <= response_start or response_end <= response_left:
                continue
            response_peak = float(microphone_energies[response_start:response_end].max())
            before_start = max(left, event_index - 4)
            before_level = float(np.median(microphone_energies[before_start:event_index + 1]))
            if response_peak < microphone_high or response_peak <= before_level * 1.16:
                continue
            game_strength = max(0.0, (event_energy - game_high) / game_spread)
            response_strength = max(0.0, (response_peak - microphone_high) / microphone_spread)
            rise_strength = max(0.0, (response_peak - before_level) / microphone_spread)
            best = max(best, game_strength * 5 + response_strength * 7 + rise_strength * 6)
        scores.append(max(0, min(16, round(best))))
    return scores


def _visual_scan_priority(record: dict) -> float:
    """Prioritize useful frames without depending on derived quality scores.

    Visual evidence is an input to the feature graph, so using ``quality_score``
    to decide which candidates receive that evidence creates a circular and
    entry-point-dependent calculation. Raw audio response and spoken content
    are sufficient to keep the bounded visual scan focused.
    """
    spoken_words = len(record.get("words") or [])
    duration = max(0.0, float(record.get("end") or 0.0) - float(record.get("start") or 0.0))
    return (
        float(record.get("audio_event_score") or 0) * 1.5
        + float(record.get("game_reaction_score") or 0)
        + max(0.0, float(record.get("voice_expression_score") or 0)) * 0.5
        + min(12.0, spoken_words / 5.0)
        + min(4.0, duration / 10.0)
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
    strongest = sorted(records, key=_visual_scan_priority, reverse=True)[:limit]
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
    strongest = sorted(records, key=_visual_scan_priority, reverse=True)[:limit]
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


def _cache_lookup(
    cache: PipelineCache | None,
    video_id: str,
    source_fingerprint: str,
    stage: str,
    parameters: dict,
    validator,
) -> tuple[bool, object | None]:
    if cache is None or not source_fingerprint:
        return False, None
    try:
        lookup = cache.get(
            video_id=video_id,
            source_fingerprint=source_fingerprint,
            stage=stage,
            parameters=parameters,
        )
        if lookup.hit and validator(lookup.value):
            diagnostics.logger().info(
                "Pipeline cache hit: video_id=%s stage=%s key=%s", video_id, stage, lookup.key,
            )
            return True, lookup.value
        if lookup.hit:
            diagnostics.logger().warning(
                "Pipeline cache payload rejected: video_id=%s stage=%s key=%s",
                video_id, stage, lookup.key,
            )
    except Exception as exc:
        diagnostics.log_failure(f"Pipeline cache read bypassed: video_id={video_id} stage={stage}", exc)
    return False, None


def _cache_store(
    cache: PipelineCache | None,
    video_id: str,
    source_fingerprint: str,
    stage: str,
    parameters: dict,
    value,
) -> None:
    if cache is None or not source_fingerprint:
        return
    try:
        cache.put(
            video_id=video_id,
            source_fingerprint=source_fingerprint,
            stage=stage,
            parameters=parameters,
            value=value,
        )
    except Exception as exc:
        # Cache is an optimization. A full disk, antivirus lock or corrupt
        # cache tree must never turn a valid recording into a failed analysis.
        diagnostics.log_failure(f"Pipeline cache write skipped: video_id={video_id} stage={stage}", exc)


def _valid_transcript_cache(value) -> bool:
    if not isinstance(value, list):
        return False
    for part in value:
        if not isinstance(part, dict) or not isinstance(part.get("text", ""), str):
            return False
        try:
            float(part["start"])
            float(part["end"])
        except (KeyError, TypeError, ValueError):
            return False
        words = part.get("words", [])
        if not isinstance(words, list) or any(not isinstance(word, dict) for word in words):
            return False
    return True


def _valid_number_list(value) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, (int, float)) and np.isfinite(float(item)) for item in value
    )


def _valid_vector_list(value) -> bool:
    return isinstance(value, list) and all(_valid_number_list(item) for item in value)


def _payload_digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _analyse(
    video_id: str,
    report: Progress,
    cleanup_paths: list[Path],
    analysis_audio: dict | None = None,
) -> None:
    video = db.row("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Nie znaleziono nagrania")
    source = Path(video["path"])
    analysis_mode = str(video.get("analysis_mode") or "default")
    if analysis_mode not in {"fast", "default", "extended"}:
        analysis_mode = "default"
    # The previous successful run remains current while all expensive work is
    # performed.  Only the final atomic persistence step activates this run.
    run_id = start_analysis_run(video_id, analysis_mode)
    cache: PipelineCache | None = None
    source_fingerprint = ""
    report(3, "Checking reusable analysis stages")
    try:
        cache = PipelineCache(settings.pipeline_cache_dir)
        source_fingerprint = fingerprint_source_file(source)
    except Exception as exc:
        diagnostics.log_failure(f"Pipeline cache bypassed: video_id={video_id}", exc)
    fast_mode = analysis_mode == "fast"
    extended_mode = analysis_mode == "extended"
    audio_defaults = analysis_audio or db.row("SELECT * FROM analysis_audio_defaults WHERE id=1") or {}
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
    metadata_parameters = {
        "stage_version": MEDIA_METADATA_CACHE_VERSION,
        "ffprobe_contract": "duration-and-audio-stream-count",
    }
    metadata_hit, cached_metadata = _cache_lookup(
        cache,
        video_id,
        source_fingerprint,
        "media-metadata",
        metadata_parameters,
        lambda value: isinstance(value, dict)
        and isinstance(value.get("duration_seconds"), (int, float))
        and float(value["duration_seconds"]) >= 0
        and isinstance(value.get("audio_track_count"), int)
        and int(value["audio_track_count"]) >= 0,
    )
    if metadata_hit:
        duration = float(cached_metadata["duration_seconds"])
        available_audio_tracks = int(cached_metadata["audio_track_count"])
    else:
        duration = duration_seconds(source)
        available_audio_tracks = audio_track_count(source)
        _cache_store(
            cache,
            video_id,
            source_fingerprint,
            "media-metadata",
            metadata_parameters,
            {"duration_seconds": duration, "audio_track_count": available_audio_tracks},
        )
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

    extracted_audio: dict[tuple[int, int], Path] = {}

    def ensure_audio(track: int, sample_rate: int) -> Path:
        key = (int(track), int(sample_rate))
        existing = extracted_audio.get(key)
        if existing is not None:
            return existing
        output = settings.work_dir / f"{video_id}-track{track}-{sample_rate}-{uuid.uuid4()}.wav"
        cleanup_paths.append(output)
        extract_audio(source, output, track, sample_rate=sample_rate)
        extracted_audio[key] = output
        return output

    if skipped_tracks:
        report(8, f"Using available audio track {transcript_track}; skipped unavailable/separate tracks")
    selected_transcription_model = "small" if fast_mode else settings.whisper_model
    _transcription_model_id, transcription_model_revision = whisper_identity(
        selected_transcription_model
    )
    transcription_cache = cache if transcription_model_revision else None
    preferred_device, preferred_compute_type, _fallback_reason = resolved_transcription_runtime(
        selected_transcription_model
    )
    transcript_parameters = {
        **transcription_cache_parameters(
            selected_transcription_model, preferred_device, preferred_compute_type,
        ),
        "audio_track": transcript_track,
    }
    transcript_hit, cached_transcript = _cache_lookup(
        transcription_cache,
        video_id,
        source_fingerprint,
        "transcription",
        transcript_parameters,
        _valid_transcript_cache,
    )
    # A CUDA probe can succeed at the next process start even when model
    # initialization previously fell back to CPU. The transcript is independent
    # of the accelerator, so reuse the verified CPU entry before decoding a
    # multi-hour recording again merely because the process-local failure set
    # was reset.
    if not transcript_hit and preferred_device == "cuda":
        cpu_parameters = {
            **transcription_cache_parameters(selected_transcription_model, "cpu", "int8"),
            "audio_track": transcript_track,
        }
        transcript_hit, cached_transcript = _cache_lookup(
            transcription_cache,
            video_id,
            source_fingerprint,
            "transcription",
            cpu_parameters,
            _valid_transcript_cache,
        )
        if transcript_hit:
            transcript_parameters = cpu_parameters
    if transcript_hit:
        transcript = cached_transcript
        effective_transcription_parameters = transcript_parameters
        report(62, "Using cached transcription")
    else:
        report(10, "Extracting microphone audio" if mode == "split" else "Extracting audio")
        transcript_audio = ensure_audio(transcript_track, 16000)
        actual_runtime: dict = {}
        transcript = transcribe(
            transcript_audio,
            report,
            duration,
            model_name=selected_transcription_model,
            runtime_info=actual_runtime,
        )
        # Third-party adapters and tests may not populate ``runtime_info``.
        # Preserve complete provenance and a valid cache key by falling back to
        # the runtime resolved immediately before transcription.
        actual_parameters = {
            **transcript_parameters,
            **actual_runtime,
            "audio_track": transcript_track,
        }
        effective_transcription_parameters = actual_parameters
        _cache_store(
            transcription_cache,
            video_id,
            source_fingerprint,
            "transcription",
            actual_parameters,
            transcript,
        )
    update_analysis_run_inputs(
        run_id,
        whisper_model=str(effective_transcription_parameters["model"]),
        whisper_device=str(effective_transcription_parameters["device"]),
        whisper_compute_type=str(effective_transcription_parameters["compute_type"]),
        transcript_audio_track=transcript_track,
        audio_analysis_mode=str(mode),
    )
    boundaries: list[float] = []
    if fast_mode:
        report(66, "Fast mode: creating text candidates")
    elif not extended_mode:
        # SceneDetect decodes every frame of the source. In the default mode
        # those boundaries only nudged an otherwise clean edge by at most 2.5
        # seconds; sentence and pause alignment already provide the useful
        # signal. Reserve the full scan for an explicitly Extended analysis.
        report(66, "Default mode: using speech-aligned boundaries")
    else:
        scene_parameters = {
            "stage_version": SCENE_BOUNDARY_CACHE_VERSION,
            "scenedetect_version": _distribution_version("scenedetect"),
            "opencv_version": _distribution_version("opencv-python-headless"),
            "detector": "AdaptiveDetector",
            "adaptive_threshold": 3.0,
            "min_scene_len": 15,
        }
        scene_hit, cached_boundaries = _cache_lookup(
            cache,
            video_id,
            source_fingerprint,
            "scene-boundaries",
            scene_parameters,
            _valid_number_list,
        )
        if scene_hit:
            boundaries = [float(value) for value in cached_boundaries]
            report(66, "Using cached scene boundaries")
        else:
            report(66, "Detecting scene changes")
            boundaries = detect_boundaries(source)
            _cache_store(
                cache,
                video_id,
                source_fingerprint,
                "scene-boundaries",
                scene_parameters,
                boundaries,
            )
    report(72, "Creating clip candidates")
    candidates = build_candidates(
        transcript,
        duration,
        boundaries,
        include_context=not fast_mode,
        context_window_seconds=20.0 if extended_mode else 12.0,
    )

    def cached_audio_features(track: int, sample_rate: int, include_tone: bool) -> tuple[np.ndarray, np.ndarray]:
        parameters = {
            "stage_version": AUDIO_FEATURE_CACHE_VERSION,
            "audio_track": int(track),
            "sample_rate": int(sample_rate),
            "channels": 1,
            "window_seconds": 0.25,
            "include_tone_proxy": bool(include_tone),
        }

        def valid(value) -> bool:
            return (
                isinstance(value, dict)
                and _valid_number_list(value.get("energies"))
                and _valid_number_list(value.get("tone_proxies"))
                and (
                    not include_tone
                    or len(value.get("tone_proxies", [])) == len(value.get("energies", []))
                )
            )

        hit, cached = _cache_lookup(
            cache,
            video_id,
            source_fingerprint,
            "audio-features",
            parameters,
            valid,
        )
        if hit:
            return (
                np.asarray(cached["energies"], dtype=np.float32),
                np.asarray(cached["tone_proxies"], dtype=np.float32),
            )
        audio = ensure_audio(track, sample_rate)
        if include_tone:
            energies, tones = audio_window_features(audio)
        else:
            energies = audio_energy_windows(audio)
            tones = np.asarray([], dtype=np.float32)
        _cache_store(
            cache,
            video_id,
            source_fingerprint,
            "audio-features",
            parameters,
            {
                "energies": energies.astype(float).tolist(),
                "tone_proxies": tones.astype(float).tolist(),
            },
        )
        return energies, tones

    if not fast_mode and dedicated_microphone_track:
        microphone_energies, microphone_tone_proxies = cached_audio_features(
            transcript_track, 16000, True,
        )
    elif not fast_mode and event_tracks:
        microphone_energies, _unused_tones = cached_audio_features(
            transcript_track, 16000, False,
        )
        microphone_tone_proxies = np.asarray([], dtype=np.float32)
    else:
        # A single mixed stream has no independent event source, so its raw
        # energy cannot establish a game -> microphone sequence.
        microphone_energies = np.asarray([], dtype=np.float32)
        microphone_tone_proxies = np.asarray([], dtype=np.float32)
    # A single mixed track cannot distinguish a loud game event from the voice.
    # In that legacy mode we keep audio neutral instead of inventing a reaction.
    microphone_expression_scores = (
        voice_delivery_scores(microphone_energies, microphone_tone_proxies, candidates)
        if not fast_mode and dedicated_microphone_track else [0] * len(candidates)
    )
    event_energies: list[tuple[str, np.ndarray]] = []
    for label, track in event_tracks:
        report(74, f"Reading {label}")
        energies, _tones = cached_audio_features(track, 8000, False)
        event_energies.append((label, energies))
    # Only a dedicated game track can establish a game -> microphone sequence.
    # The all-sounds mix also contains alerts, music and browser audio, so it
    # must never create reaction-to-game evidence by itself.
    reaction_sources = [item for item in event_energies if item[0] == "game-audio event"]
    reaction_scores = [0] * len(candidates)
    for _label, energies in reaction_sources:
        reaction_scores = [max(current, incoming) for current, incoming in zip(reaction_scores, game_reaction_scores(energies, microphone_energies, candidates, lead_seconds=2.5))]
    embedding_texts = [item["text"] or "bez wypowiedzi" for item in candidates]
    embedding_parameters = {
        "stage_version": EMBEDDING_CACHE_VERSION,
        "model": EMBEDDING_MODEL_NAME,
        "model_revision": EMBEDDING_MODEL_REVISION,
        "sentence_transformers_version": _distribution_version("sentence-transformers"),
        "normalize_embeddings": True,
        "texts_sha256": _payload_digest(embedding_texts),
        "text_count": len(embedding_texts),
    }
    embedding_hit, cached_vectors = _cache_lookup(
        cache,
        video_id,
        source_fingerprint,
        "text-embeddings",
        embedding_parameters,
        lambda value: _valid_vector_list(value) and len(value) == len(embedding_texts),
    )
    if embedding_hit:
        vectors = cached_vectors
    else:
        vectors = embed_texts(embedding_texts)
        _cache_store(
            cache,
            video_id,
            source_fingerprint,
            "text-embeddings",
            embedding_parameters,
            vectors,
        )
    records: list[dict] = []
    for index, (candidate, vector) in enumerate(zip(candidates, vectors)):
        keywords = [word.strip(".,!?;:").lower() for word in candidate["text"].split() if len(word.strip(".,!?;:")) >= 6][:12]
        semantic_tags = infer_tags(candidate["text"], vector)
        game_reaction_score = reaction_scores[index]
        voice_expression_score = microphone_expression_scores[index]
        records.append({
            "id": str(uuid.uuid4()),
            "start": candidate["start"],
            "end": candidate["end"],
            "text": candidate["text"],
            "words": candidate.get("words", []),
            "vector": vector,
            "keywords": keywords,
            # Keep semantic input separate from the final enriched tags.  The
            # graph owns every evidence-based/dynamic label.
            "semantic_tags": semantic_tags,
            "tags": semantic_tags,
            "context_before": candidate.get("context_before", ""),
            "context_after": candidate.get("context_after", ""),
            "boundary_signals": candidate.get("boundary_signals") or [],
            "analysis_mode": analysis_mode,
            "extended_completeness_score": -1,
            "audio_event_score": game_reaction_score if game_reaction_score >= GAME_REACTION_MIN_SCORE else 0,
            "game_reaction_score": game_reaction_score,
            "voice_expression_score": voice_expression_score,
            "vision_score": 0,
            "reading_screen_score": 0,
            "chat_reaction_score": 0,
            "chat_joy_score": 0,
            "chat_question_match_score": 0,
            "duplicate_group": "",
        })
    visual_scores: dict[str, int] = {}
    reading_screens: dict[str, int] = {}
    if fast_mode:
        report(82, "Fast mode: skipping visual and game-audio checks")
    else:
        report(
            82,
            "Extended mode: checking complete thoughts and context"
            if extended_mode else "Checking visual action and text-heavy game screens",
        )
        visual_inputs = [
            {
                "start": float(record["start"]),
                "end": float(record["end"]),
                "word_count": len(record.get("words") or []),
                "audio_event_score": int(record.get("audio_event_score") or 0),
                "game_reaction_score": int(record.get("game_reaction_score") or 0),
                "voice_expression_score": int(record.get("voice_expression_score") or 0),
            }
            for record in records
        ]
        visual_input_digest = _payload_digest(visual_inputs)
        interest_parameters = {
            "stage_version": VISUAL_INTEREST_CACHE_VERSION,
            "opencv_version": _distribution_version("opencv-python-headless"),
            "candidate_inputs_sha256": visual_input_digest,
            "candidate_count": len(records),
            "limit": 120,
            "gameplay_rect": [float(value) for value in gameplay_rect],
        }
        interest_hit, cached_interest = _cache_lookup(
            cache,
            video_id,
            source_fingerprint,
            "visual-interest",
            interest_parameters,
            lambda value: _valid_number_list(value) and len(value) == len(records),
        )
        if interest_hit:
            visual_scores = {
                record["id"]: int(score)
                for record, score in zip(records, cached_interest)
                if int(score)
            }
        else:
            visual_scores = visual_interest_scores(source, records, gameplay_rect=gameplay_rect)
            _cache_store(
                cache,
                video_id,
                source_fingerprint,
                "visual-interest",
                interest_parameters,
                [int(visual_scores.get(record["id"], 0)) for record in records],
            )

        reading_parameters = {
            "stage_version": VISUAL_READING_CACHE_VERSION,
            "opencv_version": _distribution_version("opencv-python-headless"),
            "candidate_inputs_sha256": visual_input_digest,
            "candidate_count": len(records),
            "limit": 120,
            "crop_policy": "central-gameplay-v1",
        }
        reading_hit, cached_reading = _cache_lookup(
            cache,
            video_id,
            source_fingerprint,
            "visual-reading",
            reading_parameters,
            lambda value: _valid_number_list(value) and len(value) == len(records),
        )
        if reading_hit:
            reading_screens = {
                record["id"]: int(score)
                for record, score in zip(records, cached_reading)
                if int(score)
            }
        else:
            reading_screens = visual_reading_scores(source, records)
            _cache_store(
                cache,
                video_id,
                source_fingerprint,
                "visual-reading",
                reading_parameters,
                [int(reading_screens.get(record["id"], 0)) for record in records],
            )
    for record in records:
        reading_screen = reading_screens.get(record["id"], 0)
        record["vision_score"] = 0 if reading_screen else visual_scores.get(record["id"], 0)
        record["reading_screen_score"] = reading_screen
        # This is the single authoritative derivation pass.  Extended reading,
        # story shape and completeness are deterministic graph nodes selected
        # through ``analysis_mode``; the pipeline supplies only raw evidence.
        record.update(recompute_segment_features(record).updates)
    # Do this after all Extended checks.  The normal list suppression then
    # keeps the highest-ranked variant, which naturally favours a concise
    # clip with a clear hook and a resolved ending.
    if extended_mode:
        assign_duplicate_groups(records, threshold=0.84, overlap_similarity=0.64, overlap_ratio=0.45)
    else:
        assign_duplicate_groups(records)
    report(94, "Saving a versioned analysis without deleting earlier reviews")
    persistence = persist_analysis_results(video_id, run_id, records)
    # A chat transcript may have been imported before a reanalysis. Reapply it
    # to the newly active moments. Chat scoring synchronizes each immutable
    # revision payload atomically. A malformed legacy chat import remains
    # optional evidence and must not discard an otherwise valid analysis run.
    try:
        apply_chat_reactions(video_id)
    except Exception as exc:
        diagnostics.log_failure(f"Chat rescore skipped after analysis: video_id={video_id}", exc)
    mode_label = {"fast": "Fast scan", "default": "Default analysis", "extended": "Extended analysis"}[analysis_mode]
    history_message = (
        f"; matched {persistence['matched']} stable moments"
        f"; retained {persistence['retired']} earlier moments in history"
    )
    report(100, f"{mode_label} ready: {len(candidates)} candidates{history_message}")


def analyse(video_id: str, report: Progress, analysis_audio: dict | None = None) -> None:
    """Run analysis and always remove its large intermediate WAV files."""
    cleanup_paths: list[Path] = []
    try:
        _analyse(video_id, report, cleanup_paths, analysis_audio)
    finally:
        # Extraction of a multi-hour recording can create gigabyte-sized WAV
        # files. Cancellation, decoder errors and model failures must not leave
        # them behind for a retry or until the user notices a full disk.
        for path in reversed(tuple(dict.fromkeys(cleanup_paths))):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                diagnostics.log_failure(f"Could not remove temporary analysis audio: {path}", exc)


def import_reference_files(collection_id: str, files: list[Path], report: Progress, source_keys: dict[Path, str] | None = None) -> int:
    """Transcribe selected local or downloaded reference clips into a collection."""
    files = [item.resolve() for item in files if item.is_file()]
    if not files:
        raise ValueError("No supported reference video files were found")

    cache: PipelineCache | None = None
    try:
        cache = PipelineCache(settings.pipeline_cache_dir)
    except Exception as exc:
        # Importing a reference must keep working when the cache directory is
        # unavailable, full or temporarily locked by antivirus software.
        diagnostics.log_failure("Reference pipeline cache bypassed", exc)

    imported = 0
    for index, source in enumerate(files, start=1):
        start_progress = int((index - 1) / len(files) * 96)
        report(start_progress, f"Reference {index}/{len(files)}: {source.name}")
        audio_path = settings.work_dir / f"reference-{uuid.uuid4()}.wav"
        source_fingerprint = ""
        try:
            source_fingerprint = fingerprint_source_file(source)
        except Exception as exc:
            diagnostics.log_failure(f"Reference cache fingerprint failed: {source}", exc)

        # A content-derived namespace lets the same reference be reused across
        # collections without putting a local filename or URL in the cache.
        cache_namespace = f"reference:{source_fingerprint}" if source_fingerprint else ""
        selected_model = settings.whisper_model
        _model_id, model_revision = whisper_identity(selected_model)
        transcription_cache = cache if model_revision else None
        preferred_device, preferred_compute_type, _fallback_reason = resolved_transcription_runtime(
            selected_model
        )
        transcript_parameters = {
            **transcription_cache_parameters(
                selected_model, preferred_device, preferred_compute_type,
            ),
            "audio_track": REFERENCE_AUDIO_TRACK,
            "reference_import": True,
        }
        try:
            span = max(1, int(96 / len(files)))
            transcript_hit, cached_parts = _cache_lookup(
                transcription_cache,
                cache_namespace,
                source_fingerprint,
                "reference-transcription",
                transcript_parameters,
                _valid_transcript_cache,
            )
            if transcript_hit:
                parts = cached_parts
                effective_transcription_parameters = transcript_parameters
                report(
                    min(96, start_progress + span // 2),
                    f"Reference {index}/{len(files)}: using cached transcription",
                )
            else:
                extract_audio(
                    source,
                    audio_path,
                    REFERENCE_AUDIO_TRACK,
                    sample_rate=REFERENCE_AUDIO_SAMPLE_RATE,
                )
                clip_duration = duration_seconds(source)
                actual_runtime: dict = {}
                parts = transcribe(
                    audio_path,
                    lambda clip_progress, _message: report(min(96, start_progress + int(clip_progress / 100 * span)), f"Reference {index}/{len(files)}: transcribing"),
                    clip_duration,
                    0,
                    100,
                    model_name=selected_model,
                    runtime_info=actual_runtime,
                )
                actual_parameters = {
                    **transcript_parameters,
                    **actual_runtime,
                    "audio_track": REFERENCE_AUDIO_TRACK,
                    "reference_import": True,
                }
                effective_transcription_parameters = actual_parameters
                _cache_store(
                    transcription_cache,
                    cache_namespace,
                    source_fingerprint,
                    "reference-transcription",
                    actual_parameters,
                    parts,
                )
            transcript = " ".join(part["text"] for part in parts).strip()
            embedding_text = transcript or "no speech"
            embedding_parameters = {
                "stage_version": EMBEDDING_CACHE_VERSION,
                "model": EMBEDDING_MODEL_NAME,
                "model_revision": EMBEDDING_MODEL_REVISION,
                "sentence_transformers_version": _distribution_version("sentence-transformers"),
                "normalize_embeddings": True,
                "text_sha256": _payload_digest(embedding_text),
            }
            embedding_hit, cached_embedding = _cache_lookup(
                cache,
                cache_namespace,
                source_fingerprint,
                "reference-text-embedding",
                embedding_parameters,
                lambda value: bool(value) and _valid_number_list(value),
            )
            if embedding_hit:
                embedding = cached_embedding
            else:
                embedding = embed_texts([embedding_text])[0]
                _cache_store(
                    cache,
                    cache_namespace,
                    source_fingerprint,
                    "reference-text-embedding",
                    embedding_parameters,
                    embedding,
                )
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
