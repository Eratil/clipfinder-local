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
from app.services.tagging import enrich_tags, score_moment_reaction


TIME_PATTERN = re.compile(r"^\s*\[?(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\]?\s*$")
TEXT_TIME_PATTERN = re.compile(r"^\s*\[?(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\]?\s*(?:[-|]\s*)?(.*)$")
REACTION_PATTERN = re.compile(r"\b(xd+|lol+|lmao|omg|o+\s*m+\s*g+|haha+|heh+|rip|gg|coo+|niee+|ja+\s*pier|what+)\b|[!?]{2,}", re.I)
JOY_PATTERN = re.compile(r"\b(xd+|lol+|lmao|rofl|kekw|lul+w*|haha+|heh+|bek+a*|śmiesz|zajebist|kocham|dobre+|piękne+)\b|(?:🤣|😂|😄|😆|💀)", re.I)


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
    author_value = next((item.get(key) for key in ("author", "commenter", "user", "username", "display_name", "sender", "name") if item.get(key) is not None), "")
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
        messages = _messages_from_json(text) if suffix == "json" else _messages_from_delimited(text) if suffix in {"csv", "tsv"} else _messages_from_text(text)
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
    """Score chat bursts after the central moment of a candidate clip.

    Most clips are longer than the event or punchline they contain.  Starting
    from their midpoint lets the delayed chat response land inside the latter
    half of a clip instead of missing reactions that happen before its end.
    """
    settings = db.row("SELECT delay_seconds FROM chat_settings WHERE video_id=?", (video_id,))
    messages = db.rows("SELECT seconds, author, message FROM chat_messages WHERE video_id=? ORDER BY seconds", (video_id,))
    if not settings or not messages:
        return 0
    delay = float(settings["delay_seconds"])
    segments = db.rows(
        """SELECT id, start_seconds, end_seconds, tags, logical_sense_score, context_score, self_contained_score, reading_likelihood,
                  game_reaction_score, voice_expression_score, vision_score
           FROM segments WHERE video_id=?""",
        (video_id,),
    )
    message_times = [float(message["seconds"]) for message in messages]
    updates = []
    for segment in segments:
        midpoint = (float(segment["start_seconds"]) + float(segment["end_seconds"])) / 2.0
        response_start = midpoint + max(0.0, delay - 2.0)
        response_end = response_start + 18.0
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
        if count >= 2:
            score += min(7, count * 2)
        if unique >= 2:
            score += min(5, unique)
        if count >= max(4, expected * 1.8):
            score += min(8, round((surge - 1) * 3.5))
        if expressive >= 2:
            score += min(4, expressive)
        score = max(0, min(20, score))
        joy_score = 0
        if joy:
            joy_score += min(6, joy * 2)
        if joy >= 2 and unique >= 2:
            joy_score += min(4, unique)
        if joy and count >= max(3, expected * 1.5):
            joy_score += min(4, round(max(0.0, surge - 1) * 2.5))
        joy_score = max(0, min(14, joy_score))
        moment_score, moment_stage = score_moment_reaction(int(segment.get("game_reaction_score") or 0), score, joy_score)
        previews = [{"author": message["author"], "message": message["message"], "seconds": message["seconds"]} for message in reaction[:4]]
        tags = enrich_tags(
            json.loads(segment.get("tags") or "[]"),
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
        updates.append((json.dumps(tags, ensure_ascii=False), score, joy_score, count, unique, round(surge, 2), json.dumps(previews, ensure_ascii=False), moment_score, moment_stage, segment["id"]))
    with db.connection() as con:
        con.executemany(
            "UPDATE segments SET tags=?, chat_reaction_score=?, chat_joy_score=?, chat_message_count=?, chat_unique_authors=?, chat_surge=?, chat_messages=?, moment_reaction_score=?, moment_reaction_stage=? WHERE id=?",
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
