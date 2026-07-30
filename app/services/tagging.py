import re
from collections import Counter

from app.services.embeddings import cosine, embed_texts

# Broad tags remain stable for saved searches created in earlier versions.
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

# Specific tags are lexical on purpose. A loose semantic match should not mark
# every candidate as a story, punchline, or criticism.
DETAILED_TAG_DEFINITIONS = (
    ("forma: opinia", ("moim zdaniem", "uważam", "według mnie", "dla mnie", "sądzę", "myślę że", "wydaje mi się")),
    ("forma: pytanie", ("dlaczego", "po co", "co to jest", "jak to", "czy to", "kto to", "gdzie to", "kiedy to", "?")),
    ("forma: rada", ("polecam", "warto", "radzę", "powinieneś", "powinniście", "trzeba", "najlepiej")),
    ("forma: krytyka", ("beznadziejn", "słabe", "kiepskie", "najgors", "głupie", "nie podoba", "absurd")),
    ("forma: porównanie", ("lepsze niż", "gorsze niż", "tak jak", "w porównaniu", "bardziej niż", "mniej niż")),
    ("forma: decyzja", ("robię to", "zostajemy", "wybieram", "idziemy", "spróbuję", "nie będę", "dobra to")),
    ("forma: przewidywanie", ("chyba będzie", "pewnie", "obstawiam", "wydaje mi się", "zaraz będzie", "na pewno będzie")),
    ("forma: historia", ("pamiętam jak", "kiedyś", "ostatnio", "wyobraź sobie", "wczoraj", "była taka sytuacja")),
    ("forma: puenta", ("i wtedy", "okazało się", "najlepsze jest", "najgorsze jest", "więc jednak", "a jednak")),
    ("emocja: śmiech", ("haha", "hehe", "hahaha", "lol", "śmiesz", "beka", "parsk")),
    ("emocja: frustracja", ("wkur", "kurw", "dość tego", "cholera", "pierdol", "ja nie mogę")),
    ("emocja: zachwyt", ("ale super", "zajebiste", "genialne", "piękne", "kocham to", "ale dobre")),
    ("emocja: rozczarowanie", ("szkoda", "niestety", "rozczar", "no nie", "serio?", "to boli")),
    ("emocja: szok", ("co jest", "niemożliwe", "o kur", "nie wierzę", "serio", "ja pierdolę")),
)

EMOTION_OR_OPINION_TAGS = {
    "radość", "złość", "gniew", "smutek", "zaskoczenie", "humor", "wyrażanie opinii",
    "emocja: śmiech", "emocja: frustracja", "emocja: zachwyt", "emocja: rozczarowanie", "emocja: szok",
    "forma: opinia", "forma: krytyka", "forma: puenta",
}
CONTEXT_TAG_PREFIXES = ("reakcja: ", "kontekst: ", "struktura: ", "format: ", "moment: ")

_FILLERS = {"yyy", "eee", "hmm", "um", "jakby", "znaczy"}
_TRAILING_CONNECTORS = {"a", "ale", "bo", "czy", "i", "jak", "że", "żeby", "więc", "to"}
_COHERENCE_CONNECTORS = {"bo", "dlatego", "więc", "ale", "jednak", "potem", "teraz", "jeśli", "gdy", "ponieważ"}
GAME_REACTION_TAG = "reakcja na grę"

_tag_vectors: list[list[float]] | None = None


def infer_tags(text: str, embedding: list[float], limit: int = 6) -> list[str]:
    global _tag_vectors
    lowered = text.lower()
    lexical = [name for name, _description, markers in TAG_DEFINITIONS if any(marker in lowered for marker in markers)]
    detailed = detailed_lexical_tags(text)
    if _tag_vectors is None:
        _tag_vectors = embed_texts([item[1] for item in TAG_DEFINITIONS])
    semantic = [(cosine(embedding, vector), TAG_DEFINITIONS[index][0]) for index, vector in enumerate(_tag_vectors)]
    semantic_tags = [name for score, name in sorted(semantic, reverse=True) if score >= 0.34]
    return list(dict.fromkeys(detailed + lexical + semantic_tags))[:limit]


def detailed_lexical_tags(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [name for name, markers in DETAILED_TAG_DEFINITIONS if any(marker in lowered for marker in markers)]


def enrich_tags(
    tags: list[str],
    *,
    logical_sense_score: int = -1,
    reading_likelihood: float = 0.0,
    game_reaction_score: int = 0,
    voice_expression_score: int = 0,
    chat_reaction_score: int = 0,
    chat_joy_score: int = 0,
    vision_score: int = 0,
    context_score: int = -1,
    self_contained_score: int = -1,
    moment_reaction_score: int = 0,
    moment_reaction_stage: str = "",
) -> list[str]:
    """Attach evidence-based context tags and replace stale dynamic values."""
    cleaned = [tag for tag in tags if not tag.startswith(CONTEXT_TAG_PREFIXES)]
    context: list[str] = []
    if reading_likelihood >= 0.55:
        context.append("format: czytanie")
    if logical_sense_score >= 68:
        context.append("struktura: samodzielna myśl")
    elif 0 <= logical_sense_score <= 35:
        context.append("struktura: urwana wypowiedź")
    if context_score >= 72:
        context.append("kontekst: pełna myśl")
    elif 0 <= context_score <= 38:
        context.append("struktura: wymaga kontekstu")
    if self_contained_score >= 75:
        context.append("struktura: samowystarczalny")
    elif 0 <= self_contained_score <= 35:
        context.append("struktura: zależny od kontekstu")
    if game_reaction_score >= 7:
        context.append("reakcja: gra")
    if voice_expression_score >= 7:
        context.append("reakcja: mocny głos")
    if chat_reaction_score >= 8:
        context.append("reakcja: czat")
    if chat_joy_score >= 4:
        context.append("reakcja: radość czatu")
    if vision_score >= 7:
        context.append("kontekst: akcja wizualna")
    if moment_reaction_score >= 7:
        context.append("moment: gra -> glos")
    if moment_reaction_stage == "game -> voice -> chat":
        context.append("moment: gra -> glos -> czat")
    return list(dict.fromkeys(cleaned + context))


def score_moment_reaction(game_reaction_score: int, chat_reaction_score: int = 0, chat_joy_score: int = 0) -> tuple[int, str]:
    """Score a causal-looking game event, voice reaction, then chat response."""
    if game_reaction_score < 7:
        return 0, ""
    score = min(16, int(game_reaction_score))
    stage = "game -> voice"
    # Chat is already read from the delayed post-clip window, so it is only
    # added after the game-to-microphone sequence has been confirmed.
    if chat_reaction_score >= 5:
        score += min(8, round(chat_reaction_score * 0.45))
        score += min(6, round(chat_joy_score * 0.45))
        stage = "game -> voice -> chat"
    return min(30, score), stage


def assess_logical_sense(text: str) -> int:
    """Estimate whether a transcript is understandable as a standalone thought."""
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


def assess_context(text: str, before: str = "", after: str = "") -> tuple[int, list[str]]:
    """Judge whether a clip works on its own after checking nearby speech.

    The neighbouring text is evidence, not extra transcript for the exported
    clip. It helps distinguish a self-contained thought from a sentence cut in
    half by candidate boundaries.
    """
    current = " ".join((text or "").split())
    before = " ".join((before or "").split())
    after = " ".join((after or "").split())
    logical = assess_logical_sense(current)
    score = 50 + round((logical - 50) * 0.55)
    signals: list[str] = []
    starts_with_link = bool(re.match(r"^(a|ale|bo|więc|i|że|żeby|to|jak|który|która|które)\b", current, re.I))
    complete_end = current.endswith((".", "!", "?"))
    if starts_with_link and before:
        score -= 16
        signals.append("depends on setup before the clip")
    if after and not complete_end:
        score -= 14
        signals.append("thought continues after the clip")
    elif complete_end and logical >= 68:
        score += 8
        signals.append("self-contained thought")
    if len(re.findall(r"[^\W_]+", current)) < 5 and (before or after):
        score -= 10
    if not signals and logical < 42:
        signals.append("limited standalone context")
    return max(1, min(99, round(score))), signals[:2]


def assess_self_containment(text: str, before: str = "", after: str = "") -> int:
    """Score whether the exported fragment is understandable without its setup."""
    current = " ".join((text or "").split())
    before = " ".join((before or "").split())
    after = " ".join((after or "").split())
    logical = assess_logical_sense(current)
    score = logical
    starts_with_link = bool(re.match(r"^(a|ale|bo|więc|i|że|żeby|to|jak|który|która|które)\b", current, re.I))
    complete_end = current.endswith((".", "!", "?"))
    if starts_with_link and before:
        score -= 20
    if after and not complete_end:
        score -= 18
    if len(re.findall(r"[^\W_]+", current)) < 5:
        score -= 12
    if complete_end and logical >= 68:
        score += 8
    return max(1, min(99, round(score)))


def assess_clip_quality(text: str, words: list[dict], start: float, end: float, tags: list[str]) -> tuple[int, list[str], float]:
    """Fast local heuristics used to rank clips and flag likely reading aloud."""
    duration = max(1.0, end - start)
    tokens = re.findall(r"[^\W_]+", text.lower())
    word_rate = len(tokens) / duration
    pauses = [float(words[index]["start"]) - float(words[index - 1]["end"]) for index in range(1, len(words)) if words[index].get("start") is not None and words[index - 1].get("end") is not None]
    long_pauses = sum(1 for pause in pauses if pause >= 0.9)
    fillers = sum(token in {"yyy", "eee", "hmm", "um", "jakby", "znaczy"} for token in tokens)
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
    matched_tags = [tag for tag in tags if tag in EMOTION_OR_OPINION_TAGS]
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
