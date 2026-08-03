"""Import and score delayed live-chat reactions for locally stored recordings."""

from __future__ import annotations

import csv
import io
import json
import re
from bisect import bisect_left, bisect_right
from collections import Counter
from typing import Any

from app import database as db
from app.services.embeddings import cosine, embed_texts
from app.services.tagging import CHAT_QUESTION_ANSWER_TAG, CHAT_QUESTION_TAG, enrich_tags, score_moment_reaction


TIME_PATTERN = re.compile(r"^\s*\[?(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\]?\s*$")
TEXT_TIME_PATTERN = re.compile(r"^\s*\[?(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\]?\s*(?:[-|]\s*)?(.*)$")
REACTION_PATTERN = re.compile(r"\b(xd+|lol+|lmao|omg|o+\s*m+\s*g+|haha+|heh+|rip|gg|coo+|niee+|ja+\s*pier|what+)\b|[!?]{2,}", re.I)
JOY_PATTERN = re.compile(r"\b(xd+|lol+|lmao|rofl|kekw|lul+w*|haha+|heh+|bek+a*|śmiesz|zajebist|kocham|dobre+|piękne+)\b|(?:🤣|😂|😄|😆|💀)", re.I)
VIEWER_QUESTION_PATTERN = re.compile(
    r"(?:\?|\b(?:co\s+sądzisz|co\s+myślisz|jak\s+(?:oceniasz|uważasz|sądzisz)|podoba\s+(?:ci|wam)\s+się|"
    r"czy\s+(?:to|ten|ta|jest|będzie|masz)|dlaczego\b|po\s+co\b|któr\w*\s+(?:wybierasz|polecasz)|"
    r"masz\s+(?:zamiar|plan)|zrobisz\s+|będziesz\s+))",
    re.I,
)
_QUESTION_ANSWER_MIN_WORDS = 5
_QUESTION_STOPWORDS = {
    "co", "czy", "jak", "jaki", "jaka", "jakie", "kto", "gdzie", "kiedy", "dlaczego", "po", "na", "o", "od", "do",
    "to", "ten", "ta", "te", "się", "jest", "będzie", "masz", "mi", "ci", "wam", "was", "dla", "ze", "że",
}
_ANSWER_OPENING_PATTERN = re.compile(
    r"^\s*(?:tak\b|nie\b|chyba\b|zależy\b|moim\s+zdaniem|uważam|myślę|wydaje\s+mi\s+się|według\s+mnie|dla\s+mnie|"
    r"szczerze|powiem\s+ci|powiem\s+wam)",
    re.I,
)


def _viewer_questions_before_answer(messages: list[dict], clip_start: float, clip_end: float, delay: float) -> list[dict]:
    """Find a genuine viewer question shortly before the streamer answers.

    Chat transcript times are delayed relative to the recording, hence the
    delay-adjusted time window. A small allowance at the end handles a message
    appearing on screen while the streamer starts replying to it.
    """
    window_start = max(0.0, clip_start + delay - 75.0)
    window_end = clip_end + delay + 3.0
    candidates = [
        message for message in messages
        if window_start <= float(message["seconds"]) <= window_end
        and VIEWER_QUESTION_PATTERN.search(str(message.get("message") or ""))
    ]
    # Recent messages are the likeliest prompt, but retain a few alternatives
    # for semantic verification in case chat was active at that moment.
    return candidates[-4:]


def _viewer_question_before_answer(messages: list[dict], clip_start: float, clip_end: float, delay: float) -> dict | None:
    candidates = _viewer_questions_before_answer(messages, clip_start, clip_end, delay)
    return candidates[-1] if candidates else None


def _looks_like_answer(transcript: str) -> bool:
    """Avoid tagging a one-word acknowledgement or another bare question."""
    words = re.findall(r"[^\W_]+", transcript or "", re.UNICODE)
    if len(words) < _QUESTION_ANSWER_MIN_WORDS:
        return False
    # A longer answer may contain a rhetorical question, so it is accepted.
    return not (len(words) < 10 and str(transcript or "").strip().endswith("?"))


def _question_answer_match_score(question: str, answer: str, question_vector: list[float], answer_vector: list[float], context_score: int, self_contained_score: int) -> int:
    """Return a conservative semantic match score for question -> answer."""
    question_words = {
        word[:6] for word in re.findall(r"[^\W_]+", question.lower(), re.UNICODE)
        if len(word) >= 4 and word not in _QUESTION_STOPWORDS
    }
    answer_words = {
        word[:6] for word in re.findall(r"[^\W_]+", answer.lower(), re.UNICODE)
        if len(word) >= 4 and word not in _QUESTION_STOPWORDS
    }
    overlap = len(question_words.intersection(answer_words))
    semantic = max(0.0, float(cosine(answer_vector, question_vector)))
    direct_answer = bool(_ANSWER_OPENING_PATTERN.search(answer or ""))
    score = semantic * 55.0
    score += min(20.0, overlap * 10.0)
    if direct_answer:
        score += 18.0
    if context_score >= 65 and self_contained_score >= 60:
        score += 6.0
    # A direct yes/no answer can be brief and share no nouns with the prompt;
    # otherwise require stronger semantic or lexical evidence.
    supported = overlap >= 1 or semantic >= 0.42 or (direct_answer and semantic >= 0.20)
    return max(0, min(99, round(score))) if supported else 0


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1250", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if 0 <= number <= 24 * 60 * 60 else None
    if not isinstance(value, str):
        return None
    try:
        numeric = float(value.strip())
        if 0 <= numeric <= 24 * 60 * 60:
            return numeric
    except ValueError:
        pass
    match = TIME_PATTERN.match(value)
    if not match:
        return None
    hours, minutes, seconds, milliseconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int((milliseconds or "0").ljust(3, "0")) / 1000


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return " ".join(filter(None, (_text_from_value(item) for item in value))).strip()
    if isinstance(value, dict):
        for key in ("text", "message", "content", "body"):
            if value.get(key):
                return _text_from_value(value[key])
    return ""


def _normalise_message(item: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = next((item.get(key) for key in ("content_offset_seconds", "offset", "seconds", "timestamp", "time", "timecode", "elapsed") if item.get(key) is not None), None)
    seconds = _seconds(timestamp)
    if seconds is None:
        return None
    text = _text_from_value(next((item.get(key) for key in ("message", "text", "content", "body", "fragments") if item.get(key) is not None), ""))
    if not text:
        return None
    author_value = next((item.get(key) for key in ("author", "commenter", "user", "username", "userName", "display_name", "displayName", "sender", "name") if item.get(key) is not None), "")
    if isinstance(author_value, dict):
        author_value = next((author_value.get(key) for key in ("name", "display_name", "login", "username") if author_value.get(key)), "")
    return {"seconds": round(seconds, 3), "author": " ".join(str(author_value or "").split())[:80], "message": text[:1000]}


def _messages_from_json(text: str) -> list[dict[str, Any]]:
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        for key in ("messages", "comments", "chat", "data", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("JSON must contain a list of chat messages.")
    return [message for item in parsed if isinstance(item, dict) and (message := _normalise_message(item))]


def _messages_from_json_lines(text: str) -> list[dict[str, Any]]:
    """Read newline-delimited JSON, ideal for a live logger that appends safely."""
    messages = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
        if isinstance(item, dict) and (message := _normalise_message(item)):
            messages.append(message)
    return messages


def _messages_from_delimited(text: str) -> list[dict[str, Any]]:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    known_time = {"time", "timestamp", "offset", "seconds", "timecode", "elapsed"}
    has_header = bool(set(header).intersection(known_time))
    if has_header:
        time_index = next((index for index, name in enumerate(header) if name in known_time), 0)
        text_index = next((index for index, name in enumerate(header) if name in {"message", "text", "content", "body"}), 1)
        author_index = next((index for index, name in enumerate(header) if name in {"author", "user", "username", "name", "sender"}), None)
        rows = rows[1:]
    else:
        time_index, text_index, author_index = 0, 1, 2 if len(rows[0]) > 2 else None
    messages = []
    for row in rows:
        if len(row) <= max(time_index, text_index):
            continue
        seconds = _seconds(row[time_index])
        message = " ".join(row[text_index].split())
        if seconds is None or not message:
            continue
        author = " ".join(row[author_index].split())[:80] if author_index is not None and len(row) > author_index else ""
        messages.append({"seconds": round(seconds, 3), "author": author, "message": message[:1000]})
    return messages


def _messages_from_text(text: str) -> list[dict[str, Any]]:
    messages = []
    for line in text.splitlines():
        match = TEXT_TIME_PATTERN.match(line)
        if not match:
            continue
        hours, minutes, seconds, milliseconds, remainder = match.groups()
        offset = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int((milliseconds or "0").ljust(3, "0")) / 1000
        remainder = remainder.strip()
        author, separator, message = remainder.partition(":")
        message = message.strip() if separator else remainder
        if message:
            messages.append({"seconds": round(offset, 3), "author": author.strip()[:80] if separator else "", "message": message[:1000]})
    return messages


def parse_chat_file(filename: str, raw: bytes) -> list[dict[str, Any]]:
    text = _decode(raw)
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        messages = _messages_from_json(text) if suffix == "json" else _messages_from_json_lines(text) if suffix in {"jsonl", "ndjson"} else _messages_from_delimited(text) if suffix in {"csv", "tsv"} else _messages_from_text(text)
    except (json.JSONDecodeError, csv.Error, ValueError) as exc:
        raise ValueError(f"Could not read this chat file: {exc}") from exc
    messages = sorted(messages, key=lambda item: item["seconds"])
    if not messages:
        raise ValueError("No chat messages with relative timestamps were found. Use timestamps such as 01:23:45 or numeric seconds.")
    return messages


def chat_summary(video_id: str) -> dict[str, Any]:
    saved = db.row("SELECT source_name, delay_seconds, imported_at FROM chat_settings WHERE video_id=?", (video_id,))
    count = db.row("SELECT COUNT(*) AS count, COUNT(DISTINCT NULLIF(author, '')) AS authors FROM chat_messages WHERE video_id=?", (video_id,)) or {}
    result = {"available": bool(saved), "message_count": int(count.get("count") or 0), "unique_authors": int(count.get("authors") or 0)}
    if saved:
        result.update(saved)
    return result


def apply_chat_reactions(video_id: str) -> int:
    """Score chat activity and, in Extended mode, verified viewer Q&A pairs."""
    settings = db.row("SELECT delay_seconds FROM chat_settings WHERE video_id=?", (video_id,))
    messages = db.rows("SELECT seconds, author, message FROM chat_messages WHERE video_id=? ORDER BY seconds", (video_id,))
    if not settings or not messages:
        return 0
    delay = float(settings["delay_seconds"])
    video = db.row("SELECT analysis_mode FROM videos WHERE id=?", (video_id,)) or {}
    extended_mode = str(video.get("analysis_mode") or "default") == "extended"
    segments = db.rows(
        """SELECT id, start_seconds, end_seconds, transcript, embedding, tags, logical_sense_score, context_score, self_contained_score, reading_likelihood,
                  game_reaction_score, voice_expression_score, vision_score
           FROM segments WHERE video_id=?""",
        (video_id,),
    )
    message_times = [float(message["seconds"]) for message in messages]
    question_vectors: dict[str, list[float]] = {}
    if extended_mode:
        question_texts = list(dict.fromkeys(
            str(message.get("message") or "") for message in messages
            if VIEWER_QUESTION_PATTERN.search(str(message.get("message") or ""))
        ))
        if question_texts:
            try:
                question_vectors = dict(zip(question_texts, embed_texts(question_texts)))
            except Exception:
                # Chat reactions must still work when a local embedding model
                # cannot be loaded; only the stricter Q&A label is skipped.
                question_vectors = {}
    updates = []
    for segment in segments:
        clip_start, clip_end = float(segment["start_seconds"]), float(segment["end_seconds"])
        # Evaluate the whole spoken fragment, then retain a small delayed tail
        # for messages that appear after its final line or punchline.
        response_start = clip_start + max(0.0, delay - 2.0)
        response_end = clip_end + delay + 18.0
        baseline_start = max(0.0, response_start - 70.0)
        baseline_end = max(baseline_start, response_start - 8.0)
        reaction = messages[bisect_left(message_times, response_start):bisect_right(message_times, response_end)]
        baseline = messages[bisect_left(message_times, baseline_start):bisect_left(message_times, baseline_end)]
        count = len(reaction)
        unique = len({message["author"].strip().lower() for message in reaction if message["author"].strip()})
        baseline_rate = len(baseline) / max(1.0, baseline_end - baseline_start)
        expected = baseline_rate * (response_end - response_start)
        surge = (count + 1.0) / (expected + 1.0)
        expressive = sum(1 for message in reaction if REACTION_PATTERN.search(message["message"]))
        joy = sum(1 for message in reaction if JOY_PATTERN.search(message["message"]))
        score = 0
        active_chat = count >= max(3, expected * 1.35)
        if active_chat:
            score += min(6, max(1, round((count - expected) * 1.4)))
        if active_chat and unique >= 2:
            score += min(5, unique)
        if count >= max(4, expected * 1.55):
            score += min(8, round((surge - 1) * 3.5))
        if active_chat and expressive >= 2:
            score += min(4, expressive)
        score = max(0, min(20, score))
        joy_score = 0
        if active_chat and joy:
            joy_score += min(6, joy * 2)
        if active_chat and joy >= 2 and unique >= 2:
            joy_score += min(4, unique)
        if active_chat and joy and count >= max(3, expected * 1.5):
            joy_score += min(4, round(max(0.0, surge - 1) * 2.5))
        joy_score = max(0, min(14, joy_score))
        moment_score, moment_stage = score_moment_reaction(int(segment.get("game_reaction_score") or 0), score, joy_score)
        previews = [{"author": message["author"], "message": message["message"], "seconds": message["seconds"]} for message in reaction[:4]]
        answer_text = str(segment.get("transcript") or "")
        question_match_score = 0
        question_text = ""
        if extended_mode and question_vectors and _looks_like_answer(answer_text) and float(segment.get("reading_likelihood") or 0) < 0.48:
            try:
                answer_vector = json.loads(segment.get("embedding") or "[]")
                for viewer_question in _viewer_questions_before_answer(messages, clip_start, clip_end, delay):
                    candidate_text = str(viewer_question.get("message") or "")
                    question_vector = question_vectors.get(candidate_text)
                    if not answer_vector or not question_vector:
                        continue
                    candidate_score = _question_answer_match_score(
                        candidate_text,
                        answer_text,
                        question_vector,
                        answer_vector,
                        int(segment.get("context_score") or 0),
                        int(segment.get("self_contained_score") or 0),
                    )
                    if candidate_score > question_match_score:
                        question_match_score, question_text = candidate_score, candidate_text
            except (TypeError, ValueError):
                question_match_score, question_text = 0, ""
        is_answer = question_match_score >= 40
        # Older versions used both labels for every question-shaped sentence.
        # Remove those stale values before assigning the evidence-based form.
        base_tags = [
            tag for tag in json.loads(segment.get("tags") or "[]")
            if tag not in {CHAT_QUESTION_TAG, "forma: pytanie", CHAT_QUESTION_ANSWER_TAG}
        ]
        if is_answer:
            base_tags.extend((CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG))
        tags = enrich_tags(
            base_tags,
            logical_sense_score=int(segment.get("logical_sense_score") or -1),
            reading_likelihood=float(segment.get("reading_likelihood") or 0),
            game_reaction_score=int(segment.get("game_reaction_score") or 0),
            voice_expression_score=int(segment.get("voice_expression_score") or 0),
            chat_reaction_score=score,
            chat_joy_score=joy_score,
            vision_score=int(segment.get("vision_score") or 0),
            context_score=int(segment.get("context_score") or -1),
            self_contained_score=int(segment.get("self_contained_score") or -1),
            moment_reaction_score=moment_score,
            moment_reaction_stage=moment_stage,
        )
        updates.append((json.dumps(tags, ensure_ascii=False), score, joy_score, count, unique, round(surge, 2), json.dumps(previews, ensure_ascii=False), moment_score, moment_stage, question_match_score if is_answer else 0, question_text if is_answer else "", segment["id"]))
    with db.connection() as con:
        con.executemany(
            "UPDATE segments SET tags=?, chat_reaction_score=?, chat_joy_score=?, chat_message_count=?, chat_unique_authors=?, chat_surge=?, chat_messages=?, moment_reaction_score=?, moment_reaction_stage=?, chat_question_match_score=?, chat_question_text=? WHERE id=?",
            updates,
        )
    return len(updates)


def import_chat(video_id: str, filename: str, raw: bytes, delay_seconds: float) -> dict[str, Any]:
    messages = parse_chat_file(filename, raw)
    with db.connection() as con:
        con.execute("DELETE FROM chat_messages WHERE video_id=?", (video_id,))
        con.executemany(
            "INSERT INTO chat_messages (video_id, seconds, author, message) VALUES (?, ?, ?, ?)",
            [(video_id, item["seconds"], item["author"], item["message"]) for item in messages],
        )
        con.execute(
            "INSERT INTO chat_settings (video_id, source_name, delay_seconds, imported_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET source_name=excluded.source_name, delay_seconds=excluded.delay_seconds, imported_at=excluded.imported_at",
            (video_id, filename[:255], delay_seconds, db.now()),
        )
    scored = apply_chat_reactions(video_id)
    return {**chat_summary(video_id), "scored_segments": scored}


def update_chat_delay(video_id: str, delay_seconds: float) -> dict[str, Any]:
    with db.connection() as con:
        if not con.execute("SELECT video_id FROM chat_settings WHERE video_id=?", (video_id,)).fetchone():
            raise ValueError("Import a chat transcript first.")
        con.execute("UPDATE chat_settings SET delay_seconds=?, imported_at=? WHERE video_id=?", (delay_seconds, db.now(), video_id))
    scored = apply_chat_reactions(video_id)
    return {**chat_summary(video_id), "scored_segments": scored}
