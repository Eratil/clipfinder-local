import csv
import re
import uuid
import wave
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app.config import settings
from app.services.media import run


Progress = Callable[[int, str], None]
_ocr_reader = None


def detect_red_boss_area(image_bytes: bytes) -> tuple[float, float, float, float]:
    """Return a crop in percent from the largest red rectangle in a screenshot."""
    import cv2

    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("The boss-area image could not be read. Use PNG, JPG or WebP.")
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Red wraps around both ends of the OpenCV hue scale.
    low_red = cv2.inRange(hsv, np.array([0, 100, 80]), np.array([12, 255, 255]))
    high_red = cv2.inRange(hsv, np.array([168, 100, 80]), np.array([180, 255, 255]))
    mask = cv2.bitwise_or(low_red, high_red)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = [cv2.boundingRect(contour) for contour in contours]
    candidates = [(x, y, crop_width, crop_height) for x, y, crop_width, crop_height in candidates if crop_width >= width * 0.02 and crop_height >= height * 0.01]
    if not candidates:
        raise ValueError("No red rectangle was found. Draw one clear bright-red outline around the boss-name area.")
    x, y, crop_width, crop_height = max(candidates, key=lambda item: item[2] * item[3])
    return (
        round(x / width * 100, 3),
        round(y / height * 100, 3),
        round(crop_width / width * 100, 3),
        round(crop_height / height * 100, 3),
    )

def _audio_as_wav(source: Path, target: Path, audio_track: int = 1) -> None:
    """Extract a selected one-based audio track as an analysis-friendly WAV."""
    run(["ffmpeg", "-y", "-i", str(source), "-map", f"0:a:{audio_track - 1}", "-vn", "-ac", "1", "-ar", "8000", "-c:a", "pcm_s16le", str(target)])


def _read_samples(handle: wave.Wave_read, count: int) -> np.ndarray:
    return np.frombuffer(handle.readframes(count), dtype="<i2").astype(np.float32)


def _normalised_scores(signal: np.ndarray, sample: np.ndarray) -> np.ndarray:
    from scipy.signal import fftconvolve

    centered_sample = sample - sample.mean()
    sample_norm = np.linalg.norm(centered_sample)
    if sample_norm < 1:
        raise ValueError("The death sound sample is silent or too short.")
    centered_signal = signal - signal.mean()
    correlation = fftconvolve(centered_signal, centered_sample[::-1], mode="valid")
    local_energy = fftconvolve(centered_signal * centered_signal, np.ones(len(centered_sample), dtype=np.float32), mode="valid")
    return correlation / (sample_norm * np.sqrt(np.maximum(local_energy, 1.0)))


def detect_death_sounds(source: Path, sound_sample: Path, threshold: float, minimum_gap_seconds: float, audio_track: int, progress: Progress) -> list[tuple[float, float]]:
    """Find local maxima matching a supplied death-sound sample in a long recording."""
    reference_wav = settings.work_dir / f"boss-reference-{uuid.uuid4()}.wav"
    source_wav = settings.work_dir / f"boss-source-{uuid.uuid4()}.wav"
    try:
        progress(2, "Preparing death sound reference")
        _audio_as_wav(sound_sample, reference_wav)
        _audio_as_wav(source, source_wav, audio_track)
        with wave.open(str(reference_wav), "rb") as handle:
            reference = _read_samples(handle, handle.getnframes())
        if len(reference) < 2_000:
            raise ValueError("Use a death sound sample at least 0.25 seconds long.")
        if len(reference) > 64_000:
            raise ValueError("Use a short death sound sample, up to about 8 seconds.")
        from scipy.signal import find_peaks

        events: list[tuple[float, float]] = []
        with wave.open(str(source_wav), "rb") as handle:
            sample_rate, total = handle.getframerate(), handle.getnframes()
            chunk_size, processed = sample_rate * 120, 0
            overlap = np.empty(0, dtype=np.float32)
            last_event = -int(minimum_gap_seconds * sample_rate)
            while processed < total:
                current = _read_samples(handle, min(chunk_size, total - processed))
                if not len(current):
                    break
                signal = np.concatenate((overlap, current))
                if len(signal) >= len(reference):
                    scores = _normalised_scores(signal, reference)
                    peaks, values = find_peaks(scores, height=threshold, distance=max(1, int(minimum_gap_seconds * sample_rate)))
                    signal_start = processed - len(overlap)
                    for peak, value in zip(peaks, values["peak_heights"], strict=False):
                        absolute = signal_start + int(peak)
                        if absolute - last_event >= minimum_gap_seconds * sample_rate:
                            events.append((absolute / sample_rate, float(value)))
                            last_event = absolute
                overlap = signal[-(len(reference) - 1):]
                processed += len(current)
                progress(5 + int(processed / total * 78), f"Matching death sound: {int(processed / total * 100)}%")
        return events
    finally:
        reference_wav.unlink(missing_ok=True)
        source_wav.unlink(missing_ok=True)


def _ocr_boss_name(source: Path, time_seconds: float, profile: dict) -> str:
    global _ocr_reader
    frame = settings.work_dir / f"boss-frame-{uuid.uuid4()}.jpg"
    try:
        run(["ffmpeg", "-y", "-ss", f"{max(0, time_seconds - 0.45):.3f}", "-i", str(source), "-frames:v", "1", "-update", "1", str(frame)])
        import cv2
        import easyocr

        image = cv2.imread(str(frame))
        if image is None:
            return "Unknown boss"
        height, width = image.shape[:2]
        x = max(0, min(width - 1, int(width * profile["crop_x"] / 100)))
        y = max(0, min(height - 1, int(height * profile["crop_y"] / 100)))
        crop_width = max(1, int(width * profile["crop_width"] / 100))
        crop_height = max(1, int(height * profile["crop_height"] / 100))
        cropped = image[y:min(height, y + crop_height), x:min(width, x + crop_width)]
        if _ocr_reader is None:
            _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        words = _ocr_reader.readtext(cropped, detail=0, paragraph=True)
        text = re.sub(r"\s+", " ", " ".join(words)).strip()
        return text[:160] if text else "Unknown boss"
    except Exception:
        return "Unknown boss"
    finally:
        frame.unlink(missing_ok=True)


def create_report(source: Path, profile: dict, target: Path, progress: Progress) -> int:
    events = detect_death_sounds(source, Path(profile["sound_path"]), float(profile["threshold"]), float(profile["minimum_gap_seconds"]), int(profile.get("audio_track") or 1), progress)
    rows = []
    for index, (time_seconds, score) in enumerate(events, start=1):
        progress(84 + int(index / max(1, len(events)) * 14), f"Reading boss names: {index}/{len(events)}")
        rows.append({"boss_name": _ocr_boss_name(source, time_seconds, profile), "time_seconds": round(time_seconds, 3), "timecode": _timecode(time_seconds), "sound_match": round(score, 4)})
    summary = Counter(row["boss_name"] for row in rows)
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["row_type", "boss_name", "death_count", "timecode", "time_seconds", "sound_match"])
        writer.writeheader()
        for boss, count in sorted(summary.items(), key=lambda item: (-item[1], item[0])):
            writer.writerow({"row_type": "summary", "boss_name": boss, "death_count": count})
        for row in rows:
            writer.writerow({"row_type": "event", "death_count": 1, **row})
    progress(100, f"Report completed: {len(rows)} deaths")
    return len(rows)


def _timecode(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
