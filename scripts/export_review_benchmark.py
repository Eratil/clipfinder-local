"""Export reviewed ClipFinder candidates as a privacy-safe benchmark JSONL.

The output contains only anonymous IDs, labels, assigned tag IDs and numeric
features.  It deliberately excludes transcripts, embeddings, paths, source
URLs, chat content and custom rejection text.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--profile", default="", help="Discovery profile used to calculate current predicted scores.")
    return parser.parse_args()


def _load_secret(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        value = path.read_bytes()
        if len(value) >= 32:
            return value
    value = secrets.token_bytes(32)
    path.write_bytes(value)
    return value


def _anonymous_id(secret: bytes, namespace: str, value: str) -> str:
    digest = hmac.new(secret, f"{namespace}:{value}".encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
    return digest[:24]


def _source_fingerprint(path_value: str, fallback: str) -> str:
    path = Path(path_value) if path_value else None
    if not path or not path.is_file():
        return f"missing:{fallback}"
    digest = hashlib.sha256()
    size = path.stat().st_size
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if size > 1024 * 1024:
            handle.seek(max(0, size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def _ascii(value: str) -> str:
    return unicodedata.normalize("NFKD", value.casefold()).encode("ascii", "ignore").decode("ascii")


def _rejection_code(reason: str) -> str:
    normalized = _ascii(reason or "")
    rules = (
        ("reading_game_text", ("czyt", "notat", "dialog", "zadani")),
        ("incomplete_cut", ("urwan", "uciet", "poczatek", "koniec")),
        ("needs_context", ("kontekst", "niezrozum", "samodziel")),
        ("duplicate", ("duplik", "powtor")),
        ("incoherent", ("bez sens", "nielogicz", "chaotycz")),
        ("monotone_delivery", ("monoton", "jednostajn")),
        ("too_long", ("za dlug", "zbyt dlug")),
        ("false_visual_event", ("alert", "raid", "emot", "wizual")),
        ("greeting_housekeeping", ("powitan", "organizacy", "techniczna rozmowa")),
        ("song_copyright", ("muzyk", "piosenk", "copyright")),
        ("profanity", ("wulg", "przekle")),
        ("technical", ("blad", "awaria", "mikrofon", "technicz")),
        ("not_interesting", ("niecieka", "nudn", "brak puenty", "kiepskie")),
    )
    return next((code for code, markers in rules if any(marker in normalized for marker in markers)), "other")


def _duration_bucket(seconds: float) -> str:
    if seconds < 8:
        return "under_8s"
    if seconds <= 16:
        return "8_16s"
    if seconds <= 26:
        return "17_26s"
    if seconds <= 40:
        return "27_40s"
    return "over_40s"


def main() -> int:
    args = _arguments()
    data_dir = args.data_dir.resolve()
    output = (args.output or data_dir / "benchmarks" / "reviewed-clips.jsonl").resolve()
    os.environ["CLIPFINDER_DATA_DIR"] = str(data_dir)

    from app import database as db  # Imported after CLIPFINDER_DATA_DIR is set.
    from app.services.benchmark import FORBIDDEN_PUBLIC_FIELDS, SCHEMA_VERSION, validate_feature_record
    from app.services.discovery import active_profile, score_candidates
    from app.services.tag_taxonomy import canonicalize_tags
    from app.version import __version__

    reviewed_rows = db.rows(
        """SELECT s.id, s.video_id, s.created_at, r.rating, r.review_reason,
                  sr.revision_number AS reviewed_revision_number,
                  sr.payload_json, sr.embedding AS reviewed_embedding,
                  v.path AS source_path, v.analysis_mode, v.source_removed
           FROM segments s
           JOIN videos v ON v.id=s.video_id
           JOIN segment_reviews r ON r.segment_id=s.id
           JOIN segment_revisions sr ON sr.id=r.reviewed_revision_id AND sr.segment_id=s.id
           WHERE r.rating IN ('accepted', 'rejected') AND sr.embedding IS NOT NULL
           ORDER BY s.created_at, s.id"""
    )
    reviewed: list[dict] = []
    for item in reviewed_rows:
        try:
            payload = json.loads(item.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            continue
        reviewed.append({
            **item,
            **payload,
            "embedding": item.get("reviewed_embedding") or payload.get("embedding"),
        })
    if not reviewed:
        raise SystemExit("No reviewed candidates with embeddings were found.")
    selected_profile = args.profile or active_profile()
    scored = score_candidates([dict(item) for item in reviewed], profile=selected_profile)

    segment_ids = [str(item["id"]) for item in reviewed]
    placeholders = ",".join("?" for _ in segment_ids)
    feedback_by_segment: dict[str, dict[str, str]] = defaultdict(dict)
    if segment_ids:
        for item in db.rows(
            f"""SELECT tr.segment_id, tr.canonical_tag AS tag, tr.verdict
                FROM segment_tag_reviews tr
                JOIN segment_reviews r
                  ON r.segment_id=tr.segment_id
                 AND r.reviewed_revision_id=tr.reviewed_revision_id
                WHERE tr.segment_id IN ({placeholders})""",
            tuple(segment_ids),
        ):
            feedback_by_segment[str(item["segment_id"])][str(item["tag"])] = str(item["verdict"])

    profile_by_segment: dict[str, list[str]] = defaultdict(list)
    if segment_ids:
        for item in db.rows(
            f"""SELECT p.segment_id, p.profile
                FROM preference_feedback p
                JOIN segment_reviews r ON r.segment_id=p.segment_id
                JOIN segment_revisions sr ON sr.id=r.reviewed_revision_id
                WHERE p.segment_id IN ({placeholders})
                  AND p.reviewed_revision_number=sr.revision_number
                ORDER BY p.profile""",
            tuple(segment_ids),
        ):
            profile_by_segment[str(item["segment_id"])].append(str(item["profile"]))

    secret = _load_secret(data_dir / "benchmarks" / ".benchmark-secret")
    source_cache: dict[str, str] = {}
    records: list[dict] = []
    skipped = 0
    for item in scored:
        try:
            tags = canonicalize_tags(json.loads(item.get("tags") or "[]"))
        except (TypeError, ValueError):
            tags = []
        segment_id = str(item["id"])
        video_id = str(item["video_id"])
        if video_id not in source_cache:
            source_cache[video_id] = _source_fingerprint(str(item.get("source_path") or ""), video_id)
        group_id = _anonymous_id(secret, "source", source_cache[video_id])
        duplicate_group = str(item.get("duplicate_group") or "")
        moment_key = duplicate_group or f"{video_id}:{round(float(item.get('start_seconds') or 0) / 5)}"
        tag_feedback = feedback_by_segment.get(segment_id, {})
        reading_feedback = [verdict for tag, verdict in tag_feedback.items() if "czyt" in _ascii(tag)]
        rejection_code = _rejection_code(str(item.get("review_reason") or "")) if item["rating"] == "rejected" else ""
        expected_reading = True if "correct" in reading_feedback else False if "incorrect" in reading_feedback else (True if rejection_code == "reading_game_text" else None)
        duration = max(0.0, float(item.get("end_seconds") or 0) - float(item.get("start_seconds") or 0))
        record = {
            "schema_version": SCHEMA_VERSION,
            "sample_id": _anonymous_id(secret, "segment", segment_id),
            "group_id": group_id,
            "moment_group_id": _anonymous_id(secret, "moment", moment_key),
            "decision": str(item["rating"]),
            "rejection_code": rejection_code,
            "analysis_mode": str(item.get("analysis_mode") or "default"),
            "duration_bucket": _duration_bucket(duration),
            "profiles": sorted(set(profile_by_segment.get(segment_id, []))),
            "predicted_score": int(item.get("ranking_score") or 1),
            "expected_reading": expected_reading,
            "tags": [str(tag) for tag in tags],
            "tag_feedback": tag_feedback,
            "features": {
                "quality": int(item.get("quality_score") or 0),
                "short_potential": int(item.get("short_potential_score") if item.get("short_potential_score") is not None else -1),
                "logical_sense": int(item.get("logical_sense_score") if item.get("logical_sense_score") is not None else -1),
                "context": int(item.get("context_score") if item.get("context_score") is not None else -1),
                "self_contained": int(item.get("self_contained_score") if item.get("self_contained_score") is not None else -1),
                "extended_completeness": int(item.get("extended_completeness_score") if item.get("extended_completeness_score") is not None else -1),
                "reading_likelihood": float(item.get("reading_likelihood") or 0),
                "audio_event": int(item.get("audio_event_score") or 0),
                "game_reaction": int(item.get("game_reaction_score") or 0),
                "voice_expression": int(item.get("voice_expression_score") or 0),
                "moment_reaction": int(item.get("moment_reaction_score") or 0),
                "vision": int(item.get("vision_score") or 0),
                "chat_reaction": int(item.get("chat_reaction_score") or 0),
                "chat_joy": int(item.get("chat_joy_score") or 0),
                "chat_question_match": int(item.get("chat_question_match_score") or 0),
            },
        }
        try:
            validate_feature_record(record)
        except Exception as exc:
            skipped += 1
            print(f"[skip] {record['sample_id']}: {exc}")
            continue
        leaked = FORBIDDEN_PUBLIC_FIELDS.intersection(record)
        if leaked:
            raise RuntimeError(f"Privacy audit failed: {sorted(leaked)}")
        records.append(record)

    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "type": "metadata",
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "app_version": __version__,
        "profile": selected_profile,
        "privacy": "feature-only; no transcript, embedding, path, URL, chat or custom rejection text",
        "record_count": len(records),
    }
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    decisions = Counter(item["decision"] for item in records)
    print(f"Benchmark written to: {output}")
    print(f"Privacy audit: PASS ({len(records)} exported, {skipped} skipped; forbidden fields absent)")
    print(f"Labels: {decisions.get('accepted', 0)} accepted / {decisions.get('rejected', 0)} rejected")
    print(f"Anonymous source groups: {len({item['group_id'] for item in records})}")
    if len(records) < 50 or len({item['group_id'] for item in records}) < 3:
        print("Benchmark status: INSUFFICIENT_DATA (collect at least 50 reviews from 3 recordings; production gate target is 300-500 reviews from 10+ recordings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
