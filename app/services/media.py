import json
import re
import subprocess
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
    try:
        result = subprocess.run(command, capture_output=True, text=True)
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
        return words
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


def _ass_color(color: str) -> str:
    if not color.startswith("#") or len(color) != 7:
        raise MediaError("Invalid caption colour")
    red, green, blue = color[1:3], color[3:5], color[5:7]
    try:
        int(red + green + blue, 16)
    except ValueError as exc:
        raise MediaError("Invalid caption colour") from exc
    return f"&H00{blue}{green}{red}".upper()


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
) -> None:
    """Create an ASS subtitle track; the highlight preset follows each spoken word."""
    sizes = {"clean": (46, 3, 1), "highlight": (52, 4, 1), "minimal": (34, 2, 0)}
    alignments = {"top": 8, "middle": 5, "bottom": 2}
    if preset not in sizes or position not in alignments:
        raise MediaError("Unknown caption preset")
    size, outline, bold = sizes[preset]
    primary = _ass_color(base_color)
    secondary = _ass_color(base_color)
    highlighted = _ass_color(active_color)
    style = f"Arial,{size},{primary},{secondary},&HAA000000,&H96000000,{bold},0,0,0,100,100,0,0,1,{outline},1,{alignments[position]},42,42,32,1"
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
    lines = []
    display_word = lambda word: censor_word(word) if censor_profanity else word
    for phrase in phrases:
        phrase_start = max(0, phrase[0]["start"])
        phrase_end = min(duration, max(phrase[-1]["end"], phrase_start + 0.2))
        if preset == "highlight":
            for word_index, word in enumerate(phrase):
                next_start = phrase[word_index + 1]["start"] if word_index + 1 < len(phrase) else phrase_end
                text = " ".join(
                    (r"{\c" + highlighted + r"\fscx112\fscy112}" + _ass_text(display_word(item["word"])) + r"{\r}")
                    if index == word_index else _ass_text(display_word(item["word"]))
                    for index, item in enumerate(phrase)
                )
                lines.append(f"Dialogue: 0,{_ass_time(max(phrase_start, word['start']))},{_ass_time(max(next_start, word['end']))},Default,,0,0,0,,{text}")
            continue
        else:
            text = " ".join(_ass_text(display_word(word["word"])) for word in phrase)
        lines.append(f"Dialogue: 0,{_ass_time(phrase_start)},{_ass_time(phrase_end)},Default,,0,0,0,,{text}")
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,{style}\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    target.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _portrait_filter(layout: str) -> str:
    """Transforms the stable OBS scene: a framed camera in the upper-right and gameplay below it."""
    camera = "crop=trunc(iw*0.11)*2:trunc(ih*0.11)*2:trunc(iw*0.78/2)*2:trunc(ih*0.03/2)*2"
    if layout == "portrait_camera":
        return f"{camera},scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    if layout == "portrait_game":
        return "crop=trunc(ih*9/32)*2:ih:trunc((iw-trunc(ih*9/32)*2)/4)*2:0,scale=1080:1920,setsar=1"
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


def _audio_censor_filter(audio_stream: str, ranges: list[tuple[float, float]], duration: float) -> str:
    conditions = "+".join(f"between(t,{left:.3f},{right:.3f})" for left, right in ranges)
    return f"[{audio_stream}]asetpts=PTS-STARTPTS,volume=0:enable='{conditions}'[aout]"


def export_clip(source: Path, target: Path, start: float, end: float, captions_path: Path | None = None, layout: str = "original", audio_track: int = 1, word_timestamps: list[dict] | None = None, transcript: str = "", censor_profanity: bool = False) -> None:
    """Export an exact clip in its original format or a 1080x1920 short layout."""
    if audio_track < 1:
        raise MediaError("Invalid audio track")
    command = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source)]
    selected_audio = f"0:a:{audio_track - 1}"
    caption_filter = f"ass='{_ffmpeg_filter_path(captions_path)}'" if captions_path else ""
    duration = end - start
    words = _caption_words(transcript, word_timestamps or [], start, duration)
    censor_ranges = _censored_audio_ranges(words, duration) if censor_profanity else []
    audio_filter = _audio_censor_filter(selected_audio, censor_ranges, duration) if censor_ranges else ""
    if layout == "portrait_split":
        camera = "crop=trunc(iw*0.11)*2:trunc(ih*0.11)*2:trunc(iw*0.78/2)*2:trunc(ih*0.03/2)*2"
        game = "crop=trunc(ih*27/64)*2:ih:trunc((iw-trunc(ih*27/64)*2)/4)*2:0,scale=1080:1280"
        top = f"{camera},scale=1080:640:force_original_aspect_ratio=decrease,pad=1080:640:(ow-iw)/2:(oh-ih)/2:color=0x10141d"
        output = "[base]" + (caption_filter + "," if caption_filter else "") + "format=yuv420p,setsar=1[outv]"
        graph = f"[0:v]split=2[camera][game];[camera]{top}[top];[game]{game}[bottom];[top][bottom]vstack=inputs=2[base];{output}"
        if audio_filter:
            graph += ";" + audio_filter
        command.extend(["-filter_complex", graph, "-map", "[outv]", "-map", "[aout]" if audio_filter else selected_audio])
    else:
        filters: list[str] = []
        if layout in {"portrait_camera", "portrait_game"}:
            filters.append(_portrait_filter(layout))
        elif layout != "original":
            raise MediaError("Unknown clip layout")
        if caption_filter:
            filters.append(caption_filter)
        if audio_filter:
            graph = f"[0:v]{','.join(filters) if filters else 'null'}[outv];{audio_filter}"
            command.extend(["-filter_complex", graph, "-map", "[outv]", "-map", "[aout]"])
        elif filters:
            command.extend(["-vf", ",".join(filters)])
        if not audio_filter:
            command.extend(["-map", "0:v:0", "-map", selected_audio])
    command.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(target),
    ])
    run(command)


def export_audio_preview(source: Path, target: Path, start: float, end: float, audio_track: int = 1) -> None:
    """Create a small cached mono MP3 for fast candidate review."""
    if audio_track < 1:
        raise MediaError("Invalid audio track")
    run([
        "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
        "-map", f"0:a:{audio_track - 1}", "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "96k", str(target),
    ])
