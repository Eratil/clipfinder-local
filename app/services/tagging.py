import re
from collections import Counter

from app.services.embeddings import cosine, embed_texts

TAG_DEFINITIONS = (
    ("radość", "joy, delight, laughter, enthusiasm, positive happy reaction", ("rado", "super", "wow", "świet", "zajeb", "haha", "lol")),
    ("złość", "anger, irritation, frustration, upset emotional reaction", ("wkur", "złości", "denerw", "dość", "kurw", "cholera")),
    ("gniew", "strong anger, outrage, rage, heated emotional reaction", ("skandal", "nienaw", "masakr", "wściek", "beznadziej")),
    ("smutek", "sadness, disappointment, regret, emotional loss", ("szkoda", "smut", "przykro", "żal", "niestety")),
    ("zaskoczenie", "surprise, shock, unexpected discovery or reaction", ("niemożli", "serio", "co jest", "o kur", "niespodz")),
    ("humor", "joke, funny situation, laughter, comedy or amusement", ("śmiesz", "żart", "bek", "haha", "lol")),
    ("wyrażanie opinii", "expressing an opinion, evaluation, judgement, personal point of view", ("moim zdaniem", "uważam", "według mnie", "myślę", "dla mnie", "sądzę")),
    ("pytanie", "asking a question, seeking an answer or clarification", ("dlaczego", "jak", "czy ", "kto", "gdzie", "kiedy")),
    ("rekomendacja", "recommendation, advice, suggestion or endorsement", ("polecam", "warto", "radzę", "powinien", "najlepsz")),
)

_FILLERS = {"yyy", "eee", "hmm", "um", "jakby", "znaczy"}
_TRAILING_CONNECTORS = {"a", "ale", "bo", "czy", "i", "jak", "że", "żeby", "więc", "to"}
_COHERENCE_CONNECTORS = {"bo", "dlatego", "więc", "ale", "jednak", "potem", "teraz", "jeśli", "gdy", "ponieważ"}
GAME_REACTION_TAG = "reakcja na gr\u0119"

_tag_vectors: list[list[float]] | None = None


def infer_tags(text: str, embedding: list[float], limit: int = 4) -> list[str]:
    global _tag_vectors
    lowered = text.lower()
    lexical: list[str] = []
    for name, _description, markers in TAG_DEFINITIONS:
        if any(marker in lowered for marker in markers):
            lexical.append(name)
    if _tag_vectors is None:
        _tag_vectors = embed_texts([item[1] for item in TAG_DEFINITIONS])
    semantic = [
        (cosine(embedding, vector), TAG_DEFINITIONS[index][0])
        for index, vector in enumerate(_tag_vectors)
    ]
    semantic_tags = [name for score, name in sorted(semantic, reverse=True) if score >= 0.34]
    return list(dict.fromkeys(lexical + semantic_tags))[:limit]


def assess_logical_sense(text: str) -> int:
    """Estimate whether a transcript is understandable as a standalone thought.

    This is a transparent text-structure heuristic, not a claim that the app
    understands every joke or stream reference. Positive chat feedback is
    considered separately by the ranking layer.
    """
    normalized = " ".join((text or "").split())
    tokens = re.findall(r"[^\W_]+", normalized.lower())
    if len(tokens) < 3:
        return 15

    score = 42
    if 7 <= len(tokens) <= 75:
        score += 18
    elif len(tokens) >= 4:
        score += 7

    filler_count = sum(token in _FILLERS for token in tokens)
    score -= min(28, filler_count * 8)
    if filler_count / len(tokens) >= 0.18:
        score -= 12
    if normalized.endswith((".", "!", "?")):
        score += 12
    if any(token in _COHERENCE_CONNECTORS for token in tokens):
        score += 8
    if "?" in normalized and len(tokens) >= 5:
        score += 5
    if normalized.endswith(("...", ",", ";", ":", "-")) or tokens[-1] in _TRAILING_CONNECTORS:
        score -= 18
    if re.search(r"\b(yyy|eee|hmm)\b.*\b(yyy|eee|hmm)\b", normalized, re.I):
        score -= 8
    return max(1, min(99, round(score)))


def assess_clip_quality(text: str, words: list[dict], start: float, end: float, tags: list[str]) -> tuple[int, list[str], float]:
    """Fast local heuristics used to rank clips and flag likely reading aloud."""
    duration = max(1.0, end - start)
    tokens = re.findall(r"[^\W_]+", text.lower())
    word_rate = len(tokens) / duration
    pauses = [float(words[index]["start"]) - float(words[index - 1]["end"]) for index in range(1, len(words)) if words[index].get("start") is not None and words[index - 1].get("end") is not None]
    long_pauses = sum(1 for pause in pauses if pause >= 0.9)
    fillers = sum(token in {"yyy", "eee", "eee", "hmm", "um", "jakby", "znaczy"} for token in tokens)
    reading_words = sum(token in {"notatka", "notatki", "przedmiot", "przedmiotu", "opis", "opisu", "dziennik", "list", "dokument"} for token in tokens)
    reading = 0.0
    if len(tokens) >= 10 and word_rate < 1.55:
        reading += 0.30
    if len(tokens) >= 10 and long_pauses >= max(2, len(tokens) // 9):
        reading += 0.25
    if fillers >= 2:
        reading += min(0.25, fillers * 0.08)
    if reading_words:
        reading += min(0.25, reading_words * 0.12)
    reading = min(1.0, reading)

    signals: list[str] = []
    score = 35
    if 6 <= duration <= 45:
        score += 12
        signals.append("good clip length")
    if 1.4 <= word_rate <= 4.8:
        score += 10
        signals.append("natural speaking pace")
    emotional_tags = {"radoĹ›Ä‡", "zĹ‚oĹ›Ä‡", "gniew", "zaskoczenie", "humor", "wyraĹĽanie opinii"}
    matched_tags = [tag for tag in tags if tag in emotional_tags]
    if matched_tags:
        score += min(24, 8 * len(matched_tags))
        signals.append("emotion or opinion")
    if any(mark in text for mark in ("!", "?")):
        score += 7
        signals.append("expressive delivery")
    if reading >= 0.55:
        score -= 32
        signals.append("possible reading aloud")
    elif reading >= 0.3:
        score -= 12
        signals.append("some reading cues")
    return max(1, min(99, round(score))), signals[:3], round(reading, 3)


def build_reference_prompt(transcripts: list[str], embeddings: list[list[float]]) -> str:
    tags = Counter(tag for text, vector in zip(transcripts, embeddings) for tag in infer_tags(text, vector))
    tags_text = ", ".join(name for name, _count in tags.most_common(4)) or "similar tone and context"
    stopwords = {"jest", "oraz", "tego", "który", "która", "bardzo", "tylko", "przez", "też", "więc", "żeby", "się", "nie", "dla"}
    words = [word.strip(".,!?;:-()[]{}\"'").lower() for text in transcripts for word in text.split()]
    keywords = [word for word, _count in Counter(word for word in words if len(word) >= 5 and word not in stopwords).most_common(6)]
    context = ", ".join(keywords) if keywords else "the tone, topic and structure of reference clips"
    return f"Find clips similar to the reference examples. Prioritize: {tags_text}. Typical context and wording: {context}."
