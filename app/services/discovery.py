"""Local candidate ranking, preference learning and duplicate suppression."""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from app import database as db
from app.services.media import is_profanity


PROFILE_DEFINITIONS = {
    "general": {"name": "General - best mixed clips", "tag_weights": {"humor": 5, "zaskoczenie": 5, "gniew": 4, "wyrażanie opinii": 4, "pytanie": 3, "radość": 3, "złość": 3}},
    "soulslike": {"name": "Soulslike - reactions, fails and wins", "tag_weights": {"gniew": 8, "złość": 7, "zaskoczenie": 7, "humor": 5, "radość": 5}},
    "conversation": {"name": "Conversation - stories and opinions", "tag_weights": {"wyrażanie opinii": 9, "rekomendacja": 7, "pytanie": 8, "humor": 5}},
    "horror": {"name": "Horror - surprise and tension", "tag_weights": {"zaskoczenie": 10, "gniew": 5, "złość": 5, "humor": 3}},
    "game_quote_reaction": {"name": "Game quote/event -> your reaction", "tag_weights": {"reakcja na grę": 16, "zaskoczenie": 7, "humor": 5, "radość": 4}},
}

PREFERENCE_FEATURES = (
    "quality", "audio", "game_reaction", "voice_expression", "visual", "chat",
    "chat_joy", "logical_sense", "context", "self_contained", "moment_reaction", "reading",
)


def active_profile() -> str:
    saved = db.row("SELECT active_profile FROM discovery_defaults WHERE id=1")
    profile = (saved or {}).get("active_profile", "general")
    return profile if profile in PROFILE_DEFINITIONS else "general"


PROFANITY_FILTERS = {"allow", "one", "none"}


def active_profanity_filter() -> str:
    saved = db.row("SELECT profanity_filter FROM discovery_defaults WHERE id=1") or {}
    value = str(saved.get("profanity_filter") or "allow")
    return value if value in PROFANITY_FILTERS else "allow"


def profanity_count(segment: dict) -> int:
    """Count profane words once, preferring the timestamped transcript tokens."""
    raw_words = segment.get("word_timestamps") or "[]"
    try:
        words = json.loads(raw_words) if isinstance(raw_words, str) else raw_words
    except (TypeError, ValueError):
        words = []
    if isinstance(words, list) and words:
        tokens = [str(item.get("word", "")) for item in words if isinstance(item, dict)]
    else:
        tokens = str(segment.get("transcript") or "").split()
    return sum(1 for token in tokens if is_profanity(token))


def filter_profanity(candidates: list[dict], profanity_filter: str | None = None) -> list[dict]:
    selected = profanity_filter or active_profanity_filter()
    if selected == "allow":
        return candidates
    maximum = 1 if selected == "one" else 0
    return [candidate for candidate in candidates if profanity_count(candidate) <= maximum]


def active_pattern_set(profile: str | None = None) -> dict | None:
    """Return the optional analytical pattern set for the active discovery profile."""
    selected_profile = profile if profile in PROFILE_DEFINITIONS else active_profile()
    saved = db.row("SELECT pattern_set_id FROM discovery_defaults WHERE id=1") or {}
    pattern_set_id = str(saved.get("pattern_set_id") or "").strip()
    if not pattern_set_id:
        return None
    return db.row(
        "SELECT id, name, profile FROM discovery_pattern_sets WHERE id=? AND profile=?",
        (pattern_set_id, selected_profile),
    )


def _pattern_rows(pattern_set_id: str) -> list[dict]:
    return db.rows(
        "SELECT tags, quality_score, logical_sense_score, reading_likelihood, embedding FROM discovery_pattern_examples WHERE pattern_set_id=?",
        (pattern_set_id,),
    )


def profile_payload() -> dict:
    profile = active_profile()
    pattern_set = active_pattern_set(profile)
    counts = {item["profile"]: item for item in db.rows(
        "SELECT profile, SUM(decision='accepted') AS accepted, SUM(decision='rejected') AS rejected FROM preference_feedback GROUP BY profile"
    )}
    profiles = []
    for key, value in PROFILE_DEFINITIONS.items():
        feedback = counts.get(key, {})
        profiles.append({"id": key, "name": value["name"], "accepted": int(feedback.get("accepted") or 0), "rejected": int(feedback.get("rejected") or 0)})
    return {
        "active_profile": profile,
        "profanity_filter": active_profanity_filter(),
        "pattern_set_id": pattern_set["id"] if pattern_set else "",
        "pattern_set_name": pattern_set["name"] if pattern_set else "",
        "profiles": profiles,
        "pattern_sets": db.rows(
            """SELECT s.id, s.name, s.profile, COUNT(e.id) AS examples
               FROM discovery_pattern_sets s LEFT JOIN discovery_pattern_examples e ON e.pattern_set_id=s.id
               GROUP BY s.id, s.name, s.profile ORDER BY lower(s.name)"""
        ),
    }


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
        "extended_completeness": min(1.0, max(0.0, float(segment.get("extended_completeness_score") or 50) / 100)),
        "chat_question_match": min(1.0, max(0.0, float(segment.get("chat_question_match_score") or 0) / 100)),
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


def is_disallowed_reading(candidate: dict) -> bool:
    """Hide likely game/note reading unless chat proves a short reply worked."""
    reading = float(candidate.get("reading_likelihood") or 0)
    if reading < 0.48:
        return False
    duration = max(0.0, float(candidate.get("end_seconds", candidate.get("end", 0)) or 0) - float(candidate.get("start_seconds", candidate.get("start", 0)) or 0))
    chat = int(candidate.get("chat_reaction_score") or 0)
    joy = int(candidate.get("chat_joy_score") or 0)
    # Intentional exception: a short quoted viewer comment may remain only
    # when the following answer clearly entertained the chat.
    return not (duration <= 24 and chat >= 7 and joy >= 3)


def _duration_adjustment(candidate: dict, tags: list[str], chat_score: int) -> tuple[float, str]:
    """Prefer concise clips while allowing complete opinions and answers."""
    duration = max(0.0, float(candidate.get("end_seconds", candidate.get("end", 0)) or 0) - float(candidate.get("start_seconds", candidate.get("start", 0)) or 0))
    long_form = bool({"forma: opinia", "forma: rada", "forma: krytyka", "forma: historia", "forma: puenta"}.intersection(tags))
    long_form = long_form or (chat_score >= 8 and int(candidate.get("logical_sense_score") or 0) >= 60)
    if 8 <= duration <= 26:
        return 8.0, "concise length"
    if 26 < duration <= 32:
        return 4.0, "reviewable length"
    if 32 < duration <= 42:
        return (1.0, "long complete answer") if long_form else (-11.0, "too long for a focused clip")
    if duration > 42:
        return (-8.0, "extended opinion or answer") if long_form else (-25.0, "far too long for a focused clip")
    return -4.0, "very short clip"


def score_candidates(candidates: list[dict], reference: list[list[float]] | None = None, profile: str | None = None) -> list[dict]:
    """Add a transparent 0-99 score using local preferences and content signals."""
    if not candidates:
        return []
    profile = profile if profile in PROFILE_DEFINITIONS else active_profile()
    pattern_rows: list[dict] = []
    pattern_label = ""
    if reference is None:
        pattern_set = active_pattern_set(profile)
        if pattern_set:
            pattern_rows = _pattern_rows(pattern_set["id"])
            reference = [json.loads(item["embedding"]) for item in pattern_rows]
            pattern_label = pattern_set["name"]
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
        completeness = int(candidate.get("extended_completeness_score") or 0)
        if completeness <= 0:
            completeness = context
        question_match = int(candidate.get("chat_question_match_score") or 0)
        moment_reaction = int(candidate.get("moment_reaction_score") or 0)
        moment_stage = candidate.get("moment_reaction_stage") or ""
        tags = json.loads(candidate.get("tags") or "[]")
        quality_signals = json.loads(candidate.get("quality_signals") or "[]")
        tag_bonus = sum(weight for tag, weight in definition["tag_weights"].items() if tag in tags)
        if "reakcja na grę" in tags:
            tag_bonus += {"general": 6, "soulslike": 10, "horror": 8}.get(profile, 4)
        # Tags are useful labels, not independent proof of quality.  Several
        # overlapping labels previously pushed ordinary clips to the 99 cap.
        tag_bonus = min(8.0, float(tag_bonus))
        excluded_reading = is_disallowed_reading(candidate)
        duration_adjustment, duration_reason = _duration_adjustment(candidate, tags, chat)
        reading_penalty = reading * (52 if excluded_reading else 10)
        # Suggested score is deliberately calibrated in bounded groups rather
        # than as an open-ended sum.  A score of 99 should be exceptional, not
        # the result of a normal clip collecting several correlated bonuses.
        quality_component = quality * 0.28                         # 0..27.7
        reaction_component = min(15.0, audio * 0.20 + visual * 0.22 + chat * 0.30 + moment_reaction * 0.20 + max(game_reaction, voice_expression) * 0.20)
        logic_component = max(-6.0, min(4.0, (logical_sense - 50) * 0.09))
        context_component = max(-4.0, min(4.0, (context - 50) * 0.08))
        standalone_component = max(-6.0, min(6.0, (self_contained - 50) * 0.12))
        completeness_component = max(-4.0, min(3.0, (completeness - 50) * 0.06))
        question_component = min(4.0, question_match * 0.04)
        score = 10 + quality_component + reaction_component + logic_component + context_component + standalone_component + completeness_component + question_component + tag_bonus + duration_adjustment - reading_penalty
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
        if profile == "game_quote_reaction":
            # The current local analysis identifies a game-audio cue followed
            # by speech on the microphone track. Downrank clips without that
            # order, so unconnected loud game audio does not dominate here.
            if moment_stage in {"game -> voice", "game -> voice -> chat"}:
                score += 7 + min(4, game_reaction * 0.25)
            else:
                score -= 12
        if reference:
            score += prompt_match * 10
        if pattern_rows:
            pattern_tags = {tag for item in pattern_rows for tag in json.loads(item.get("tags") or "[]")}
            overlap = len(set(tags).intersection(pattern_tags))
            score += min(3, overlap)
        if len(accepted):
            score += approval_match * 4
        if strongest_rejection >= 0.42:
            score -= strongest_rejection * 18
        feedback_bonus = 0.0
        if len(profile_accepted) >= 3:
            feedback_bonus += profile_approval_match * 5
        if len(profile_rejected) >= 3 and profile_rejection_match >= 0.38:
            feedback_bonus -= profile_rejection_match * 7
        accepted_distance = _feature_distance(candidate, profile_accepted_rows) if len(profile_accepted_rows) >= 4 else None
        rejected_distance = _feature_distance(candidate, profile_rejected_rows) if len(profile_rejected_rows) >= 4 else None
        if accepted_distance is not None and rejected_distance is not None:
            feedback_bonus += max(-4.0, min(4.0, (rejected_distance - accepted_distance) * 12))
        tag_feedback = _tag_preference(candidate, profile_accepted_rows, profile_rejected_rows)
        feedback_bonus += tag_feedback
        feedback_bonus = max(-9.0, min(7.0, feedback_bonus))
        score += feedback_bonus
        if excluded_reading:
            # It remains accessible under the reading tag for manual checking,
            # but cannot become a suggested best clip.
            score = min(score, 18)
        if reference:
            candidate["similarity"] = round(prompt_match, 4)
        candidate["approval_match"] = round(approval_match, 4)
        candidate["profile_feedback_score"] = round(feedback_bonus, 2)
        candidate["excluded_from_discovery"] = excluded_reading
        candidate["ranking_score"] = max(1, min(99, round(score)))
        reasons = [f"quality {quality}/99"]
        reasons.append(duration_reason)
        if reference:
            source = f"discovery patterns: {pattern_label}" if pattern_label else "prompt"
            reasons.insert(0, f"matches {source} {round(max(0, prompt_match) * 100)}%")
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
        if "pytanie" in tags:
            reasons.append("answers a viewer question")
        if question_match >= 40:
            reasons.append(f"viewer question matched {question_match}/99")
        if moment_stage == "game -> voice -> chat":
            reasons.append(f"game moment -> voice -> chat {moment_reaction}/30")
        elif moment_reaction >= 7:
            reasons.append(f"game moment -> voice {moment_reaction}/30")
        if profile == "game_quote_reaction":
            reasons.append("game cue before microphone reaction" if moment_stage in {"game -> voice", "game -> voice -> chat"} else "no game cue -> microphone reaction sequence")
        if context >= 72:
            reasons.append("context confirms a complete thought")
        elif context <= 38 and not happy_chat:
            reasons.append("surrounding speech suggests a cut-off thought")
        if self_contained >= 75:
            reasons.append("works without prior context")
        elif self_contained <= 35 and not happy_chat:
            reasons.append("needs surrounding conversation to make sense")
        if int(candidate.get("extended_completeness_score") or -1) >= 0:
            reasons.append(f"extended completeness {completeness}/99")
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
        if excluded_reading:
            reasons.append("likely task/note reading - excluded from best clips")
        elif reading >= 0.48:
            reasons.append("short reading kept because chat reacted")
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


def _best_of_tags(candidate: dict) -> set[str]:
    """Return the meaningful tag types used to keep a Best of list varied."""
    try:
        tags = json.loads(candidate.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    return {
        str(tag) for tag in tags
        if str(tag).startswith(("forma:", "emocja:", "reakcja:", "kontekst:", "moment:"))
        or str(tag) in {"humor", "zaskoczenie", "radość", "złość", "gniew", "smutek", "pytanie", "wyrażanie opinii", "rekomendacja", "reakcja na grę"}
    }


def _same_best_of_moment(left: dict, right: dict, padding_seconds: float = 35.0) -> bool:
    """Treat nearby candidates as one stream moment, even if their text differs."""
    left_start, left_end = float(left.get("start_seconds", 0)), float(left.get("end_seconds", 0))
    right_start, right_end = float(right.get("start_seconds", 0)), float(right.get("end_seconds", 0))
    return left_start <= right_end + padding_seconds and right_start <= left_end + padding_seconds


def best_of_stream(candidates: list[dict], limit: int = 10) -> list[dict]:
    """Pick strong but varied moments from one stream for fast review.

    Duplicate groups remove near-identical text.  This second pass also keeps a
    time buffer around each chosen moment and lightly prefers fresh content
    tags, so a single long conversation, fail or game reaction cannot fill the
    whole Best of list.
    """
    remaining = suppress_duplicate_groups(candidates)
    selected: list[dict] = []
    used_tags: set[str] = set()
    while remaining and len(selected) < max(1, limit):
        eligible = [item for item in remaining if not any(_same_best_of_moment(item, chosen) for chosen in selected)]
        if not eligible:
            break
        def diversity_score(item: dict) -> tuple[float, float, float]:
            overlap = len(_best_of_tags(item) & used_tags)
            ranking = float(item.get("ranking_score") or 0)
            short_potential = max(0.0, float(item.get("short_potential_score") or 0))
            # Best of stream is meant for producing short-form material, so
            # keep the user's discovery preference as the main signal while
            # giving a meaningful boost to a concise, standalone candidate.
            score = (ranking * 0.65) + (short_potential * 0.35) - min(12.0, overlap * 3.0)
            return score, short_potential, ranking
        chosen = max(eligible, key=diversity_score)
        chosen["ranking_reason"] = f"{chosen.get('ranking_reason', '')}; Best of stream: distinct moment, short potential {int(chosen.get('short_potential_score') or 0)}/99".strip("; ")
        selected.append(chosen)
        used_tags.update(_best_of_tags(chosen))
        remaining = [item for item in remaining if item.get("id") != chosen.get("id")]
    return selected


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
            left_start, left_end = float(records[left]["start"]), float(records[left].get("end", records[left]["start"]))
            right_start, right_end = float(records[right]["start"]), float(records[right].get("end", records[right]["start"]))
            overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
            shorter = max(0.1, min(left_end - left_start, right_end - right_start))
            # Generated candidates for one sentence often share the same
            # start.  They are alternatives, not three different moments.
            overlapping_variant = overlap / shorter >= 0.55 and similarities[left, right] >= 0.70
            if overlapping_variant or similarities[left, right] >= threshold:
                union(left, right)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        groups[find(index)].append(index)
    for group_number, indices in enumerate((items for items in groups.values() if len(items) > 1), start=1):
        group_id = f"repeat-{group_number}"
        for index in indices:
            records[index]["duplicate_group"] = group_id
