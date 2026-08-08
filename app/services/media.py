import json
import os
import re
import subprocess
import sys
from pathlib import Path


class MediaError(RuntimeError):
    pass


_PROFANITY_STEMS = (
    "kurw", "pierdol", "jeba", "jeb", "chuj", "huj", "pizd", "cipa", "kutas",
    "skurwysyn", "wypierd", "odpierd", "spierd", "popierd", "pojeb", "zajeb",
    "dziwk", "rucha", "fuck", "shit", "bitch", "asshole",
)


def is_profanity(word: str) -> bool:
    normalized = re.sub(r"[^a-ząćęłńóśźż]", "", word.casefold())
    return len(normalized) >= 3 and any(stem in normalized for stem in _PROFANITY_STEMS)


def censor_word(word: str) -> str:
    if not is_profanity(word):
        return word
    return "".join("*" if char.isalnum() else char for char in word)


def run(command: list[str]) -> subprocess.CompletedProcess:
    # The desktop build has no parent console.  Without this flag Windows briefly
    # creates a visible CMD window for every ffmpeg/ffprobe operation.
    process_options: dict = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(command, capture_output=True, text=True, **process_options)
    except FileNotFoundError as exc:
        executable = Path(command[0]).name
        raise MediaError(
            f"{executable} was not found. Run 'Configure ClipFinder runtime' from the Start menu, "
            "or install FFmpeg and reopen ClipFinder."
        ) from exc
    if result.returncode:
        detail = result.stderr[-1500:] or result.stdout[-1500:]
        raise MediaError(detail)
    return result


def duration_seconds(video_path: Path) -> float:
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(video_path),
    ])
    return float(json.loads(result.stdout)["format"]["duration"])


def audio_track_count(video_path: Path) -> int:
    """Return the number of selectable audio streams in a recording."""
    result = run([
        "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
        "-of", "json", str(video_path),
    ])
    return len(json.loads(result.stdout).get("streams", []))


def extract_audio(video_path: Path, output_path: Path, audio_track: int = 1, sample_rate: int = 16000) -> None:
    if audio_track < 1:
        raise MediaError("Invalid audio track")
    run([
        "ffmpeg", "-y", "-i", str(video_path), "-map", f"0:a:{audio_track - 1}", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(output_path),
    ])


def extract_audio_range(video_path: Path, output_path: Path, start: float, end: float, audio_track: int = 1) -> None:
    """Extract an exact, small WAV file for re-transcribing an edited clip."""
    if audio_track < 1:
        raise MediaError("Invalid audio track")
    run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(video_path), "-map", f"0:a:{audio_track - 1}",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output_path),
    ])


def _ffmpeg_filter_path(path: Path) -> str:
    """Escape a Windows filename for FFmpeg's filter argument parser."""
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def _ass_time(seconds: float) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours)}:{int(minutes):02d}:{seconds:05.2f}"


def _caption_words(transcript: str, word_timestamps: list[dict], clip_start: float, duration: float) -> list[dict]:
    words = [
        {"start": max(0.0, float(item["start"]) - clip_start), "end": max(0.0, float(item["end"]) - clip_start), "word": str(item["word"]).strip()}
        for item in word_timestamps
        if item.get("word") and item.get("start") is not None and item.get("end") is not None
    ]
    if words:
        return sorted(words, key=lambda item: (item["start"], item["end"]))
    tokens = transcript.split()
    if not tokens:
        return [{"start": 0.0, "end": duration, "word": "..."}]
    weight = sum(max(1, len(token)) for token in tokens)
    position = 0.0
    result = []
    for token in tokens:
        span = duration * max(1, len(token)) / weight
        result.append({"start": position, "end": position + span, "word": token})
        position += span
    return result


def _ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_color(color: str, opacity: int = 100) -> str:
    if not color.startswith("#") or len(color) != 7:
        raise MediaError("Invalid caption colour")
    red, green, blue = color[1:3], color[3:5], color[5:7]
    try:
        int(red + green + blue, 16)
    except ValueError as exc:
        raise MediaError("Invalid caption colour") from exc
    opacity = max(0, min(100, int(opacity)))
    alpha = round(255 * (1 - opacity / 100))
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


CAPTION_PRESETS = {
    "clean": {"size": 46, "outline": 3, "bold": 1, "italic": 0, "word_highlight": False, "scale": 100, "box": False},
    "highlight": {"size": 52, "outline": 4, "bold": 1, "italic": 0, "word_highlight": True, "scale": 112, "box": False},
    "minimal": {"size": 34, "outline": 2, "bold": 0, "italic": 0, "word_highlight": False, "scale": 100, "box": False},
    "boxed_pop": {"size": 46, "outline": 5, "bold": 1, "italic": 0, "word_highlight": False, "scale": 100, "box": True},
    "neon_gaming": {"size": 50, "outline": 2, "bold": 1, "italic": 0, "word_highlight": True, "scale": 116, "box": False},
    "cinematic": {"size": 42, "outline": 10, "bold": 0, "italic": 0, "word_highlight": False, "scale": 100, "box": True},
    "karaoke_punch": {"size": 54, "outline": 4, "bold": 1, "italic": 0, "word_highlight": True, "scale": 124, "box": False},
    "minimal_center": {"size": 32, "outline": 2, "bold": 0, "italic": 0, "word_highlight": False, "scale": 100, "box": False, "alignment": 5},
}

CAPTION_FONT_FAMILIES = {
    "Inter": "Inter",
    "Montserrat": "Montserrat",
    "Poppins": "Poppins",
    "Lato": "Lato",
    "Roboto Condensed": "Roboto Condensed",
    "Oswald": "Oswald",
    "Nunito": "Nunito",
    "Noto Sans": "Noto Sans",
    "Bungee": "Bungee",
    "Cinzel": "Cinzel",
    "Pixelify Sans": "Pixelify Sans",
}


def bundled_fonts_directory() -> Path:
    """Resolve bundled caption fonts in source and PyInstaller builds."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets" / "fonts"
    return Path(__file__).resolve().parents[2] / "assets" / "fonts"


def write_caption_ass(
    target: Path,
    transcript: str,
    duration: float,
    preset: str,
    word_timestamps: list[dict] | None = None,
    clip_start: float = 0,
    position: str = "bottom",
    base_color: str = "#FFFFFF",
    active_color: str = "#FFFF00",
    censor_profanity: bool = False,
    outline_enabled: bool = True,
    outline_color: str = "#000000",
    glow_enabled: bool = False,
    opacity: int = 100,
    font_family: str = "Inter",
    max_lines: int = 2,
) -> None:
    """Create an ASS subtitle track with optional outline, glow and text opacity."""
    # The fractional positions deliberately use a centred anchor, so their
    # visual centre lands at 2/5 or 4/5 of the vertical frame rather than
    # being pushed against a screen edge by ASS's top/bottom margins.
    alignments = {"top": 8, "two_fifths": 5, "middle": 5, "four_fifths": 5, "bottom": 2}
    position_overrides = {
        "two_fifths": r"{\an5\pos(960,432)}",
        "four_fifths": r"{\an5\pos(960,864)}",
    }
    if preset not in CAPTION_PRESETS or position not in alignments or font_family not in CAPTION_FONT_FAMILIES:
        raise MediaError("Unknown caption preset")
    if not 20 <= int(opacity) <= 100 or not 1 <= int(max_lines) <= 4:
        raise MediaError("Caption opacity or line limit is invalid")
    config = CAPTION_PRESETS[preset]
    primary = _ass_color(base_color, opacity)
    secondary = _ass_color(base_color, opacity)
    highlighted = _ass_color(active_color, opacity)
    outline = int(config["outline"]) if outline_enabled else 0
    border_style = 3 if config.get("box") and outline_enabled else 1
    shadow = 3 if glow_enabled else 1
    glow_color = active_color if config.get("word_highlight") else base_color
    back_color = _ass_color(glow_color, max(20, min(55, int(opacity * 0.45)))) if glow_enabled else "&H96000000"
    # Position is controlled per clip, so it must win over a style's old
    # preferred alignment (for example Minimal Center).
    alignment = alignments[position]
    style = f"{CAPTION_FONT_FAMILIES[font_family]},{config['size']},{primary},{secondary},{_ass_color(outline_color, opacity)},{back_color},{config['bold']},{config['italic']},0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},42,42,32,1"
    words = _caption_words(transcript, word_timestamps or [], clip_start, duration)
    sentences: list[list[dict]] = []
    sentence: list[dict] = []
    for word in words:
        sentence.append(word)
        if word["word"].rstrip().endswith((".", "!", "?", "…")):
            sentences.append(sentence)
            sentence = []
    if sentence:
        sentences.append(sentence)
    phrases: list[list[dict]] = []
    index = 0
    while index < len(sentences):
        phrase = list(sentences[index])
        if len(phrase) <= 4 and index + 1 < len(sentences):
            phrase.extend(sentences[index + 1])
            index += 1
        elif len(phrase) <= 4 and phrases:
            phrases[-1].extend(phrase)
            index += 1
            continue
        phrases.append(phrase)
        index += 1

    # ASS will auto-wrap text, but a long sentence could then occupy most of
    # the screen. Create timed caption blocks with explicit line breaks and a
    # strict line cap instead. The character estimate is deliberately a bit
    # conservative so proportional fonts and Polish words do not unexpectedly
    # create an extra fifth line.
    # Vertical 1080x1920 exports enlarge ASS glyphs relative to PlayResY.
    # Keep each explicit line deliberately short enough for the narrow output,
    # rather than relying on the player's automatic wrapping.
    max_characters_per_line = max(14, int(1000 / (float(config["size"]) * 0.78)))

    def split_phrase(phrase_words: list[dict]) -> list[list[list[dict]]]:
        visual_lines: list[list[dict]] = []
        current_line: list[dict] = []
        current_length = 0
        for item in phrase_words:
            word = str(item["word"])
            next_length = current_length + (1 if current_line else 0) + len(word)
            if current_line and next_length > max_characters_per_line:
                visual_lines.append(current_line)
                current_line = [item]
                current_length = len(word)
            else:
                current_line.append(item)
                current_length = next_length
        if current_line:
            visual_lines.append(current_line)
        return [visual_lines[offset:offset + int(max_lines)] for offset in range(0, len(visual_lines), int(max_lines))]

    caption_blocks = [block for phrase in phrases for block in split_phrase(phrase)]
    lines = []
    display_word = lambda word: censor_word(word) if censor_profanity else word
    for caption_lines in caption_blocks:
        phrase = [word for line in caption_lines for word in line]
        phrase_start = max(0, phrase[0]["start"])
        phrase_end = min(duration, max(phrase[-1]["end"], phrase_start + 0.2))
        if config["word_highlight"]:
            for word_index, word in enumerate(phrase):
                next_start = phrase[word_index + 1]["start"] if word_index + 1 < len(phrase) else phrase_end
                current_index = 0
                rendered_lines = []
                for caption_line in caption_lines:
                    rendered_words = []
                    for item in caption_line:
                        rendered_words.append(
                            r"{\c" + highlighted + r"\fscx" + str(config["scale"]) + r"\fscy" + str(config["scale"]) + r"}" + _ass_text(display_word(item["word"])) + r"{\r}"
                            if current_index == word_index else _ass_text(display_word(item["word"]))
                        )
                        current_index += 1
                    rendered_lines.append(" ".join(rendered_words))
                text = position_overrides.get(position, "") + r"\N".join(rendered_lines)
                lines.append(f"Dialogue: 0,{_ass_time(max(phrase_start, word['start']))},{_ass_time(max(next_start, word['end']))},Default,,0,0,0,,{text}")
            continue
        else:
            text = position_overrides.get(position, "") + r"\N".join(" ".join(_ass_text(display_word(word["word"])) for word in caption_line) for caption_line in caption_lines)
        lines.append(f"Dialogue: 0,{_ass_time(phrase_start)},{_ass_time(phrase_end)},Default,,0,0,0,,{text}")
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nPlayResX: 1920\nPlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,{style}\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    target.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _crop_filter(rect: tuple[float, float, float, float]) -> str:
    """Build an even-pixel crop from a normalized rectangle."""
    x, y, width, height = (max(0.0, min(1.0, float(value))) for value in rect)
    width = min(width, 1.0 - x)
    height = min(height, 1.0 - y)
    if width < 0.02 or height < 0.02:
        raise MediaError("Saved camera or gameplay area is too small. Calibrate the layout again.")
    # FFmpeg crop dimensions must be even for the output encoder. Divide by
    # two *before* truncating, then multiply back; doing it in the opposite
    # order doubled the crop and could exceed the source frame dimensions.
    return f"crop=trunc(iw*{width:.6f}/2)*2:trunc(ih*{height:.6f}/2)*2:trunc(iw*{x:.6f}/2)*2:trunc(ih*{y:.6f}/2)*2"


def _portrait_filter(layout: str, camera_rect: tuple[float, float, float, float], game_rect: tuple[float, float, float, float]) -> str:
    """Transforms user-calibrated camera and gameplay areas into vertical formats."""
    camera = _crop_filter(camera_rect)
    if layout == "portrait_camera":
        return f"{camera},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    if layout == "portrait_game":
        return f"{_crop_filter(game_rect)},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    raise MediaError("Unknown portrait layout")


def _censored_audio_ranges(words: list[dict], duration: float) -> list[tuple[float, float]]:
    ranges = []
    for word in words:
        if not is_profanity(str(word.get("word", ""))):
            continue
        word_start = float(word.get("start", 0))
        word_end = float(word.get("end", word_start))
        left = max(0.0, word_start + (word_end - word_start) / 2)
        right = min(duration, word_end)
        if right > left:
            ranges.append((left, right))
    return ranges


def _audio_processing_filter(
    audio_stream: str,
    censor_ranges: list[tuple[float, float]],
    microphone_enhancement: bool,
    normalize_loudness: bool,
    volume_gain_db: float,
) -> str:
    """Return a single audio chain so optional Composer tools can stack safely."""
    filters = ["asetpts=PTS-STARTPTS"]
    if censor_ranges:
        conditions = "+".join(f"between(t,{left:.3f},{right:.3f})" for left, right in censor_ranges)
        filters.append(f"volume=0:enable='{conditions}'")
    if microphone_enhancement:
        # Conservative cleanup for speech: remove rumble, suppress harsh high
        # frequencies and gently reduce very large volume peaks. It is kept
        # optional because an all-sounds track also contains game audio.
        filters.extend(("highpass=f=80", "lowpass=f=14000", "acompressor=threshold=-18dB:ratio=2.2:attack=12:release=180:makeup=2"))
    if normalize_loudness:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if abs(float(volume_gain_db)) >= 0.01:
        filters.append(f"volume={float(volume_gain_db):.2f}dB")
    return f"[{audio_stream}]{','.join(filters)}[aout]"


def pause_trim_ranges(words: list[dict], duration: float, clip_start: float = 0.0, threshold: float = 0.85, padding: float = 0.12) -> list[tuple[float, float]]:
    """Keep speech and short natural gaps; remove only clearly long pauses."""
    normalized = []
    for word in words:
        if word.get("start") is None or word.get("end") is None:
            continue
        left = max(0.0, float(word["start"]) - clip_start)
        right = min(duration, float(word["end"]) - clip_start)
        if right > left:
            normalized.append((left, right))
    if len(normalized) < 2:
        return [(0.0, duration)]
    normalized.sort()
    ranges: list[tuple[float, float]] = []
    kept_start = 0.0
    previous_end = normalized[0][1]
    for next_start, next_end in normalized[1:]:
        if next_start - previous_end >= threshold:
            kept_end = min(duration, previous_end + padding)
            if kept_end - kept_start >= 0.08:
                ranges.append((kept_start, kept_end))
            kept_start = max(0.0, next_start - padding)
        previous_end = max(previous_end, next_end)
    if duration - kept_start >= 0.08:
        ranges.append((kept_start, duration))
    return ranges or [(0.0, duration)]


def remap_words_for_kept_ranges(words: list[dict], ranges: list[tuple[float, float]]) -> list[dict]:
    """Move word timestamps onto the timeline after removed pauses."""
    remapped = []
    offset = 0.0
    for left, right in ranges:
        for word in words:
            if word.get("start") is None or word.get("end") is None:
                continue
            word_start, word_end = float(word["start"]), float(word["end"])
            overlap_start, overlap_end = max(left, word_start), min(right, word_end)
            if overlap_end <= overlap_start:
                continue
            remapped.append({**word, "start": offset + overlap_start - left, "end": offset + overlap_end - left})
        offset += right - left
    return remapped


def _pause_trim_graph(video_stream: str, audio_stream: str, ranges: list[tuple[float, float]]) -> tuple[list[str], str, str]:
    """Return FFmpeg graph stages which concatenate the kept video/audio pieces."""
    stages: list[str] = []
    inputs: list[str] = []
    for index, (left, right) in enumerate(ranges):
        stages.append(f"[{video_stream}]trim=start={left:.3f}:end={right:.3f},setpts=PTS-STARTPTS[v{index}]")
        stages.append(f"[{audio_stream}]atrim=start={left:.3f}:end={right:.3f},asetpts=PTS-STARTPTS[a{index}]")
        inputs.extend((f"[v{index}]", f"[a{index}]"))
    stages.append("".join(inputs) + f"concat=n={len(ranges)}:v=1:a=1[basev][basea]")
    return stages, "basev", "basea"


def _hook_reorder_graph(video_stream: str, audio_stream: str, duration: float, hook_seconds: float) -> tuple[list[str], str, str]:
    """Move the final part of an input clip before its earlier part."""
    split_at = duration - hook_seconds
    stages = [
        f"[{video_stream}]trim=start={split_at:.3f}:end={duration:.3f},setpts=PTS-STARTPTS[vhook]",
        f"[{audio_stream}]atrim=start={split_at:.3f}:end={duration:.3f},asetpts=PTS-STARTPTS[ahook]",
        f"[{video_stream}]trim=start=0:end={split_at:.3f},setpts=PTS-STARTPTS[vbody]",
        f"[{audio_stream}]atrim=start=0:end={split_at:.3f},asetpts=PTS-STARTPTS[abody]",
        "[vhook][ahook][vbody][abody]concat=n=2:v=1:a=1[basev][basea]",
    ]
    return stages, "basev", "basea"


def export_clip(source: Path, target: Path, start: float, end: float, captions_path: Path | None = None, layout: str = "original", audio_track: int = 1, word_timestamps: list[dict] | None = None, transcript: str = "", censor_profanity: bool = False, camera_rect: tuple[float, float, float, float] = (0.78, 0.03, 0.11, 0.11), game_rect: tuple[float, float, float, float] = (0.22, 0.0, 0.56, 1.0), pause_ranges: list[tuple[float, float]] | None = None, microphone_enhancement: bool = False, normalize_loudness: bool = False, volume_gain_db: float = 0, hook_seconds: float = 0) -> None:
    """Export an exact clip in its original format or a 1080x1920 short layout."""
    if audio_track < 1:
        raise MediaError("Invalid audio track")
    command = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source)]
    selected_audio = f"0:a:{audio_track - 1}"
    caption_filter = ""
    if captions_path:
        fonts_dir = bundled_fonts_directory()
        font_option = f":fontsdir='{_ffmpeg_filter_path(fonts_dir)}'" if fonts_dir.is_dir() else ""
        caption_filter = f"ass='{_ffmpeg_filter_path(captions_path)}'{font_option}"
    duration = end - start
    words = _caption_words(transcript, word_timestamps or [], start, duration)
    pause_ranges = pause_ranges or [(0.0, duration)]
    trim_pauses = len(pause_ranges) > 1
    hook_seconds = max(0.0, float(hook_seconds))
    if hook_seconds and (hook_seconds >= duration - 0.49 or trim_pauses):
        raise MediaError("Opening hook requires a clip longer than the hook and cannot be combined with pause removal")
    output_duration = sum(right - left for left, right in pause_ranges) if trim_pauses else duration
    censor_ranges = _censored_audio_ranges(words, output_duration) if censor_profanity else []
    graph_stages: list[str] = []
    video_stream, audio_stream = "0:v", selected_audio
    if trim_pauses:
        graph_stages, video_stream, audio_stream = _pause_trim_graph(video_stream, audio_stream, pause_ranges)
    elif hook_seconds:
        graph_stages, video_stream, audio_stream = _hook_reorder_graph(video_stream, audio_stream, duration, hook_seconds)
    has_timeline_graph = trim_pauses or bool(hook_seconds)
    if not -12 <= float(volume_gain_db) <= 12:
        raise MediaError("Volume correction must be between -12 dB and +12 dB")
    needs_audio_processing = bool(censor_ranges or microphone_enhancement or normalize_loudness or abs(float(volume_gain_db)) >= 0.01)
    audio_filter = _audio_processing_filter(audio_stream, censor_ranges, microphone_enhancement, normalize_loudness, volume_gain_db) if needs_audio_processing else ""
    audio_map = "[aout]" if audio_filter else (f"[{audio_stream}]" if has_timeline_graph else selected_audio)
    if layout == "portrait_split":
        camera = _crop_filter(camera_rect)
        game = f"{_crop_filter(game_rect)},scale=1080:1280:force_original_aspect_ratio=increase,crop=1080:1280"
        top = f"{camera},scale=1080:640:force_original_aspect_ratio=decrease,pad=1080:640:(ow-iw)/2:(oh-ih)/2:color=0x10141d"
        output = "[base]" + (caption_filter + "," if caption_filter else "") + "format=yuv420p,setsar=1[outv]"
        graph_stages.extend((f"[{video_stream}]split=2[camera][game]", f"[camera]{top}[top]", f"[game]{game}[bottom]", "[top][bottom]vstack=inputs=2[base]", output))
        if audio_filter:
            graph_stages.append(audio_filter)
        command.extend(["-filter_complex", ";".join(graph_stages), "-map", "[outv]", "-map", audio_map])
    else:
        filters: list[str] = []
        if layout in {"portrait_camera", "portrait_game"}:
            filters.append(_portrait_filter(layout, camera_rect, game_rect))
        elif layout != "original":
            raise MediaError("Unknown clip layout")
        if caption_filter:
            filters.append(caption_filter)
        if has_timeline_graph or audio_filter:
            graph_stages.append(f"[{video_stream}]{','.join(filters) if filters else 'null'}[outv]")
            if audio_filter:
                graph_stages.append(audio_filter)
            command.extend(["-filter_complex", ";".join(graph_stages), "-map", "[outv]", "-map", audio_map])
        elif filters:
            command.extend(["-vf", ",".join(filters)])
        if not has_timeline_graph and not audio_filter:
            command.extend(["-map", "0:v:0", "-map", selected_audio])
    command.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(target),
    ])
    run(command)


def export_audio_preview(source: Path, target: Path, start: float, end: float, audio_track: int = 1, pause_ranges: list[tuple[float, float]] | None = None, word_timestamps: list[dict] | None = None, transcript: str = "", censor_profanity: bool = False, microphone_enhancement: bool = False, normalize_loudness: bool = False, volume_gain_db: float = 0) -> None:
    """Create a cached MP3 preview, optionally using the same audio tools as export."""
    if audio_track < 1:
        raise MediaError("Invalid audio track")
    if not -12 <= float(volume_gain_db) <= 12:
        raise MediaError("Volume correction must be between -12 dB and +12 dB")
    duration = end - start
    command = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
    selected_audio = f"0:a:{audio_track - 1}"
    pause_ranges = pause_ranges or [(0.0, duration)]
    stages: list[str] = []
    if len(pause_ranges) > 1:
        inputs = []
        for index, (left, right) in enumerate(pause_ranges):
            stages.append(f"[{selected_audio}]atrim=start={left:.3f}:end={right:.3f},asetpts=PTS-STARTPTS[a{index}]")
            inputs.append(f"[a{index}]")
        stages.append("".join(inputs) + f"concat=n={len(pause_ranges)}:v=0:a=1[basea]")
        audio_stream = "basea"
    else:
        audio_stream = selected_audio
    words = _caption_words(transcript, word_timestamps or [], start, duration)
    output_duration = sum(right - left for left, right in pause_ranges)
    censor_ranges = _censored_audio_ranges(words, output_duration) if censor_profanity else []
    needs_processing = bool(censor_ranges or microphone_enhancement or normalize_loudness or abs(float(volume_gain_db)) >= 0.01)
    if needs_processing:
        stages.append(_audio_processing_filter(audio_stream, censor_ranges, microphone_enhancement, normalize_loudness, volume_gain_db))
        command.extend(["-filter_complex", ";".join(stages), "-map", "[aout]"])
    elif stages:
        command.extend(["-filter_complex", ";".join(stages), "-map", "[basea]"])
    else:
        command.extend(["-map", selected_audio])
    command.extend(["-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "96k", str(target)])
    run(command)
