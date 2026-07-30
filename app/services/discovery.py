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

PREFERENCE_FEATURES = (
    "quality", "audio", "game_reaction", "voice_expression", "visual", "chat",
    "chat_joy", "logical_sense", "context", "self_contained", "moment_reaction", "reading",
)


def active_profile() -> str:
    saved = db.row("SELECT active_profile FROM discovery_defaults WHERE id=1")
    profile = (saved or {}).get("active_profile", "general")
    return profile if profile in PROFILE_DEFINITIONS else "general"


def profile_payload() -> dict:
    profile = active_profile()
    counts = {item["profile"]: item for item in db.rows(
        "SELECT profile, SUM(decision='accepted') AS accepted, SUM(decision='rejected') AS rejected FROM preference_feedback GROUP BY profile"
    )}
    profiles = []
    for key, value in PROFILE_DEFINITIONS.items():
        feedback = counts.get(key, {})
        profiles.append({"id": key, "name": value["name"], "accepted": int(feedback.get("accepted") or 0), "rejected": int(feedback.get("rejected") or 0)})
    return {"active_profile": profile, "profiles": profiles}


def preference_features(segment: dict) -> dict:
    """Stable, human-readable signals captured when the user makes a decision."""
    tags = json.loads(segment.get("tags") or "[]")
    values = {
        "quality": min(1.0, max(0.0, float(segment.get("quality_score") or 0) / 99)),
        "audio": min(1.0, max(0.0, float(segment.get("audio_event_score") or 0) / 20)),
        "game_reaction": min(1.0, max(0.0, float(segment.get("game_reaction_score") or 0) / 20)),
        "voice_expression": min(1.0, max(0.0, float(segment.get("voice_expression_score") or 0) / 20)),
        "visual": min(1.0, max(0.0, float(segment.get("vision_score") or 0) / 20)),
        "chat": min(1.0, max(0.0, float(segment.get("chat_reaction_score") or 0) / 20)),
        "chat_joy": min(1.0, max(0.0, float(segment.get("chat_joy_score") or 0) / 20)),
        "logical_sense": min(1.0, max(0.0, float(segment.get("logical_sense_score") or 50) / 100)),
        "context": min(1.0, max(0.0, float(segment.get("context_score") or 50) / 100)),
        "self_contained": min(1.0, max(0.0, float(segment.get("self_contained_score") or 50) / 100)),
        "moment_reaction": min(1.0, max(0.0, float(segment.get("moment_reaction_score") or 0) / 30)),
        "reading": min(1.0, max(0.0, float(segment.get("reading_likelihood") or 0))),
    }
    return {"values": values, "tags": tags}


def _vectors(rows: list[dict]) -> np.ndarray:
    if not rows:
        return np.empty((0, 0), dtype=np.float32)
    return np.asarray([json.loads(item["embedding"]) for item in rows], dtype=np.float32)


def _legacy_preference_vectors() -> tuple[np.ndarray, dict[str, np.ndarray]]:
    accepted = db.rows("SELECT embedding FROM segments WHERE rating='accepted' AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 160")
    rejected = db.rows("SELECT embedding, review_reason FROM segments WHERE rating='rejected' AND review_reason != '' AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 200")
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for item in rejected:
        by_reason[item["review_reason"]].append(item)
    return _vectors(accepted), {reason: _vectors(items) for reason, items in by_reason.items()}


def _profile_feedback(profile: str) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray], list[dict], list[dict]]:
    rows = db.rows(
        "SELECT decision, review_reason, embedding, features FROM preference_feedback WHERE profile=? ORDER BY updated_at DESC LIMIT 240",
        (profile,),
    )
    accepted_rows = [item for item in rows if item["decision"] == "accepted"]
    rejected_rows = [item for item in rows if item["decision"] == "rejected"]
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for item in rejected_rows:
        if item.get("review_reason"):
            by_reason[item["review_reason"]].append(item)
    return _vectors(accepted_rows), _vectors(rejected_rows), {reason: _vectors(items) for reason, items in by_reason.items()}, accepted_rows, rejected_rows


def _feature_distance(candidate: dict, examples: list[dict]) -> float | None:
    if not examples:
        return None
    target = preference_features(candidate)["values"]
    example_values = [json.loads(item["features"]).get("values", {}) for item in examples]
    centroid = {key: float(np.mean([float(values.get(key, 0)) for values in example_values])) for key in PREFERENCE_FEATURES}
    return float(np.mean([abs(float(target[key]) - centroid[key]) for key in PREFERENCE_FEATURES]))


def _tag_preference(candidate: dict, accepted: list[dict], rejected: list[dict]) -> float:
    if len(accepted) < 4 or len(rejected) < 4:
        return 0.0
    accepted_tags = [tag for item in accepted for tag in json.loads(item["features"]).get("tags", [])]
    rejected_tags = [tag for item in rejected for tag in json.loads(item["features"]).get("tags", [])]
    score = 0.0
    for tag in preference_features(candidate)["tags"]:
        accepted_rate = (accepted_tags.count(tag) + 1) / (len(accepted) + 4)
        rejected_rate = (rejected_tags.count(tag) + 1) / (len(rejected) + 4)
        score += (accepted_rate - rejected_rate) * 8
    return max(-5.0, min(5.0, score))


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
    accepted, rejected_by_reason = _legacy_preference_vectors()
    profile_accepted, profile_rejected, profile_rejected_by_reason, profile_accepted_rows, profile_rejected_rows = _profile_feedback(profile)
    reference_matrix = np.asarray(reference, dtype=np.float32) if reference else np.empty((0, 0), dtype=np.float32)
    ranked: list[dict] = []
    for candidate in candidates:
        vector = np.asarray(json.loads(candidate["embedding"]), dtype=np.float32)
        prompt_match = _mean_top_similarity(vector, reference_matrix) if reference else 0.0
        approval_match = _mean_top_similarity(vector, accepted)
        rejection_matches = {reason: _mean_top_similarity(vector, values, 4) for reason, values in rejected_by_reason.items()}
        strongest_rejection_reason, strongest_rejection = max(rejection_matches.items(), key=lambda item: item[1], default=("", 0.0))
        profile_approval_match = _mean_top_similarity(vector, profile_accepted)
        profile_rejection_match = _mean_top_similarity(vector, profile_rejected, 4)
        profile_rejection_matches = {reason: _mean_top_similarity(vector, values, 4) for reason, values in profile_rejected_by_reason.items()}
        profile_rejection_reason, profile_reason_match = max(profile_rejection_matches.items(), key=lambda item: item[1], default=("", 0.0))
        quality = int(candidate.get("quality_score") or 0)
        audio = int(candidate.get("audio_event_score") or 0)
        game_reaction = int(candidate.get("game_reaction_score") or 0)
        voice_expression = int(candidate.get("voice_expression_score") or 0)
        visual = int(candidate.get("vision_score") or 0)
        chat = int(candidate.get("chat_reaction_score") or 0)
        chat_joy = int(candidate.get("chat_joy_score") or 0)
        chat_messages = int(candidate.get("chat_message_count") or 0)
        chat_authors = int(candidate.get("chat_unique_authors") or 0)
        reading = float(candidate.get("reading_likelihood") or 0)
        logical_sense = int(candidate.get("logical_sense_score") or 0)
        if logical_sense <= 0:
            logical_sense = 50
        context = int(candidate.get("context_score") or 0)
        if context <= 0:
            context = 50
        self_contained = int(candidate.get("self_contained_score") or 0)
        if self_contained <= 0:
            self_contained = 50
        moment_reaction = int(candidate.get("moment_reaction_score") or 0)
        moment_stage = candidate.get("moment_reaction_stage") or ""
        tags = json.loads(candidate.get("tags") or "[]")
        quality_signals = json.loads(candidate.get("quality_signals") or "[]")
        tag_bonus = sum(weight for tag, weight in definition["tag_weights"].items() if tag in tags)
        if "reakcja na grę" in tags:
            tag_bonus += {"general": 6, "soulslike": 10, "horror": 8}.get(profile, 4)
        score = 22 + quality * 0.46 + audio * 0.75 + visual * 0.65 + chat * 0.85 + moment_reaction * 0.45 + (context - 50) * 0.10 + (self_contained - 50) * 0.12 + tag_bonus - reading * 28
        strong_emotion = bool({"humor", "gniew", "zaskoczenie", "radość", "złość"}.intersection(tags)) or game_reaction >= 7 or voice_expression >= 9
        happy_chat = chat_joy >= 4 and chat >= 5
        context_penalty = 0
        context_bonus = 0
        if not strong_emotion:
            if logical_sense < 42:
                if happy_chat:
                    context_bonus = min(12, 4 + chat_joy)
                else:
                    context_penalty = min(22, 8 + round((42 - logical_sense) * 0.45))
            elif logical_sense >= 68:
                context_bonus = 4
            if (context <= 38 or self_contained <= 35) and not happy_chat:
                context_penalty += 5
        score += context_bonus - context_penalty
        if reference:
            score += prompt_match * 27
        if len(accepted):
            score += approval_match * 13
        if strongest_rejection >= 0.42:
            score -= strongest_rejection * 18
        feedback_bonus = 0.0
        if len(profile_accepted) >= 3:
            feedback_bonus += profile_approval_match * 12
        if len(profile_rejected) >= 3 and profile_rejection_match >= 0.38:
            feedback_bonus -= profile_rejection_match * 15
        accepted_distance = _feature_distance(candidate, profile_accepted_rows) if len(profile_accepted_rows) >= 4 else None
        rejected_distance = _feature_distance(candidate, profile_rejected_rows) if len(profile_rejected_rows) >= 4 else None
        if accepted_distance is not None and rejected_distance is not None:
            feedback_bonus += max(-6.0, min(6.0, (rejected_distance - accepted_distance) * 18))
        tag_feedback = _tag_preference(candidate, profile_accepted_rows, profile_rejected_rows)
        feedback_bonus += tag_feedback
        score += feedback_bonus
        if reference:
            candidate["similarity"] = round(prompt_match, 4)
        candidate["approval_match"] = round(approval_match, 4)
        candidate["profile_feedback_score"] = round(feedback_bonus, 2)
        candidate["ranking_score"] = max(1, min(99, round(score)))
        reasons = [f"quality {quality}/99"]
        if reference:
            reasons.insert(0, f"prompt match {round(max(0, prompt_match) * 100)}%")
        if len(accepted) >= 4 and approval_match >= 0.30:
            reasons.append("matches your approvals")
        if len(profile_accepted) >= 3 and profile_approval_match >= 0.30:
            reasons.append(f"matches your {profile} approvals")
        if len(profile_rejected) >= 3 and profile_rejection_match >= 0.42:
            suffix = f": {profile_rejection_reason}" if profile_reason_match >= 0.42 and profile_rejection_reason else ""
            reasons.append(f"similar to your {profile} rejections{suffix}")
        elif len(profile_accepted_rows) >= 4 and len(profile_rejected_rows) >= 4 and feedback_bonus >= 3:
            reasons.append(f"fits your {profile} review pattern")
        if game_reaction >= 7:
            reasons.append("game event followed by microphone reaction")
        elif voice_expression >= 7 and audio >= 7:
            reasons.append("expressive microphone delivery")
        if visual >= 7:
            reasons.append("visual action")
        if chat >= 7:
            people = f" / {chat_authors} viewers" if chat_authors else ""
            reasons.append(f"chat reacted: {chat_messages} messages{people}")
        if moment_stage == "game -> voice -> chat":
            reasons.append(f"game moment -> voice -> chat {moment_reaction}/30")
        elif moment_reaction >= 7:
            reasons.append(f"game moment -> voice {moment_reaction}/30")
        if context >= 72:
            reasons.append("context confirms a complete thought")
        elif context <= 38 and not happy_chat:
            reasons.append("surrounding speech suggests a cut-off thought")
        if self_contained >= 75:
            reasons.append("works without prior context")
        elif self_contained <= 35 and not happy_chat:
            reasons.append("needs surrounding conversation to make sense")
        boundary_signals = [signal for signal in quality_signals if signal in {"start aligned to sentence", "end aligned to sentence", "extended to punchline"}]
        if boundary_signals:
            reasons.append("smart boundaries: " + ", ".join(boundary_signals))
        if not strong_emotion and logical_sense >= 68:
            reasons.append("clear standalone thought")
        elif not strong_emotion and logical_sense < 42 and happy_chat:
            reasons.append("chat enjoyed an unexpected or absurd moment")
        elif not strong_emotion and logical_sense < 42:
            reasons.append("contextless or incomplete speech without chat reaction")
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
