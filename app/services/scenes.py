from pathlib import Path


def detect_boundaries(video_path: Path) -> list[float]:
    """Return likely shot boundaries without generating intermediate image files."""
    from scenedetect import AdaptiveDetector, detect

    scenes = detect(str(video_path), AdaptiveDetector(adaptive_threshold=3.0, min_scene_len=15))
    return [end.get_seconds() for _start, end in scenes]
