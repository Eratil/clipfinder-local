"""Local candidate ranking, preference learning and duplicate suppression."""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from app import database as db


PROFILE_DEFINITIONS = {
    "general": {"name": "General - best mixed clips", "tag_weights": {"humor": 5, "zaskoczenie": 5, "gniew": 4, "wyrażanie opinii": 4, "radość": 3, "złość": 3}},
    "soulslike": {"name": "Soulslike - reactions, fails and wins", "tag_weights": {"gniew": 8, "złość": 7, "zaskoczenie": 7, "humor": 5, "radość": 5}},
    "conversation": {"name": "Conversation - stories and opinions", "tag_weights": {"wyrażanie opinii": 9, "rekomendacja": 7, "humor": 5, "pytanie": 3}},
    "horror": {"name": "Horror - surprise and tension", "tag_weights": {"zaskoczenie": 10, "gniew": 5, "złość": 5, "humor": 3}},
}


def active_profile() -> str:
    saved = db.row("SELECT active_profile FROM discovery_defaults WHERE id=1")
    profile = (saved or {}).get("active_profile", "general")
    return profile if profile in PROFILE_DEFINITIONS else "general"


def profile_payload() -> dict:
    profile = active_profile()
    return {"active_profile": profile, "profiles": [{"id": key, "name": value["name"]} for key, value in PROFILE_DEFINITIONS.items()]}


def _vectors(rows: list[dict]) -> np.ndarray:
    if not rows:
        return np.empty((0, 0), dtype=np.float32)
    return np.asarray([json.loads(item["embedding"]) for item in rows], dtype=np.float32)


def _preference_vectors() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    accepted = db.rows("SELECT embedding FROM segments WHERE rating='accepted' AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 160")
    rejected = db.rows("SELECT embedding, review_reason FROM segments WHERE rating='rejected' AND review_reason != '' AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 200")
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for item in rejected:
        by_reason[item["review_reason"]].append(item)
    return _vectors(accepted), {reason: _vectors(items) for reason, items in by_reason.items()}


def _mean_top_similarity(vector: np.ndarray, examples: np.ndarray, count: int = 8) -> float:
    if examples.size == 0:
        return 0.0
    scores = examples @ vector
    best = np.partition(scores, -min(count, len(scores)))[-min(count, len(scores)):]
    return float(np.mean(best))


def score_candidates(candidates: list[dict], reference: list[list[float]] | None = None, profile: str | None = None) -> list[dict]:
    """Add a transparent 0-99 score using local preferences and content signals."""
    if not candidates:
        return []
    profile = profile if profile in PROFILE_DEFINITIONS else active_profile()
    definition = PROFILE_DEFINITIONS[profile]
    accepted, rejected_by_reason = _preference_vectors()
    reference_matrix = np.asarray(reference, dtype=np.float32) if reference else np.empty((0, 0), dtype=np.float32)
    ranked: list[dict] = []
    for candidate in candidates:
        vector = np.asarray(json.loads(candidate["embedding"]), dtype=np.float32)
        prompt_match = _mean_top_similarity(vector, reference_matrix) if reference else 0.0
        approval_match = _mean_top_similarity(vector, accepted)
        rejection_matches = {reason: _mean_top_similarity(vector, values, 4) for reason, values in rejected_by_reason.items()}
        strongest_rejection_reason, strongest_rejection = max(rejection_matches.items(), key=lambda item: item[1], default=("", 0.0))
        quality = int(candidate.get("quality_score") or 0)
        audio = int(candidate.get("audio_event_score") or 0)
        game_reaction = int(candidate.get("game_reaction_score") or 0)
        voice_expression = int(candidate.get("voice_expression_score") or 0)
        visual = int(candidate.get("vision_score") or 0)
        reading = float(candidate.get("reading_likelihood") or 0)
        tag_bonus = sum(weight for tag, weight in definition["tag_weights"].items() if tag in json.loads(candidate.get("tags") or "[]"))
        score = 22 + quality * 0.46 + audio * 0.75 + visual * 0.65 + tag_bonus - reading * 28
        if reference:
            score += prompt_match * 27
        if len(accepted):
            score += approval_match * 13
        if strongest_rejection >= 0.42:
            score -= strongest_rejection * 18
        if reference:
            candidate["similarity"] = round(prompt_match, 4)
        candidate["approval_match"] = round(approval_match, 4)
        candidate["ranking_score"] = max(1, min(99, round(score)))
        reasons = [f"quality {quality}/99"]
        if reference:
            reasons.insert(0, f"prompt match {round(max(0, prompt_match) * 100)}%")
        if len(accepted) >= 4 and approval_match >= 0.30:
            reasons.append("matches your approvals")
        if game_reaction >= 7:
            reasons.append("game event followed by microphone reaction")
        elif voice_expression >= 7 and audio >= 7:
            reasons.append("expressive microphone delivery")
        if visual >= 7:
            reasons.append("visual action")
        if tag_bonus >= 7:
            reasons.append("matches active content profile")
        if strongest_rejection >= 0.42:
            reasons.append(f"similar to rejection: {strongest_rejection_reason}")
        if reading >= 0.55:
            reasons.append("possible reading")
        candidate["ranking_reason"] = "; ".join(reasons)
        ranked.append(candidate)
    return ranked


def suppress_duplicate_groups(candidates: list[dict], keep_alternatives: bool = False) -> list[dict]:
    """Keep the best clip from a repeated moment while retaining a way to show alternatives."""
    ordered = sorted(candidates, key=lambda item: item.get("ranking_score", 0), reverse=True)
    seen: set[str] = set()
    result = []
    for item in ordered:
        group = item.get("duplicate_group") or ""
        item["duplicate_alternative"] = bool(group and group in seen)
        if group:
            seen.add(group)
        if keep_alternatives or not item["duplicate_alternative"]:
            result.append(item)
    return result


def assign_duplicate_groups(records: list[dict], threshold: float = 0.88) -> None:
    """Assign groups of semantically near-identical moments from one recording."""
    if len(records) < 2:
        return
    matrix = np.asarray([record["vector"] for record in records], dtype=np.float32)
    similarities = matrix @ matrix.T  # embeddings are already normalized
    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    for left in range(len(records)):
        for right in range(left + 1, len(records)):
            if abs(records[left]["start"] - records[right]["start"]) < 18:
                continue
            if similarities[left, right] >= threshold:
                union(left, right)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        groups[find(index)].append(index)
    for group_number, indices in enumerate((items for items in groups.values() if len(items) > 1), start=1):
        group_id = f"repeat-{group_number}"
        for index in indices:
            records[index]["duplicate_group"] = group_id
