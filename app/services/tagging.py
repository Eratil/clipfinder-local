import re
import unicodedata
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
    ("rekomendacja", "recommendation, advice, suggestion or endorsement", ("polecam", "warto", "radzę", "powinien", "najlepsz")),
)

# Specific tags are lexical on purpose. A loose semantic match should not mark
# every candidate as a story, punchline, or criticism.
DETAILED_TAG_DEFINITIONS = (
    ("forma: opinia", ("moim zdaniem", "uważam", "według mnie", "dla mnie", "sądzę", "myślę że", "wydaje mi się")),
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
# Repeated objective/UI language is a strong signal that the streamer is
# reading a game's task, note or dialogue.  These are stems deliberately, so
# inflected Polish forms are caught as well.
_READING_CUE_STEMS = {
    "zadani", "misj", "zainstal", "mieszk", "lokator", "kamery", "kamera",
    "zachowaj", "wchodz", "zbierz", "informac", "przedmiot", "dziennik",
    "notatk", "dokument", "instrukcj", "otworz", "odblokuj", "znajdz",
}
_DOCUMENT_CUE_STEMS = {
    "dyrektyw", "zabran", "rozporzad", "ustaw", "paragraf", "artykul", "spoleczen", "obywatel",
    "filozof", "doktryn", "ideologi", "antyrzadow", "przepisy", "postanowien",
}
GAME_REACTION_TAG = "reakcja na grę"
# This tag is deliberately *not* inferred from the streamer's wording. It is
# assigned by chat.py only when a viewer question is followed by an answer.
CHAT_QUESTION_TAG = "pytanie"
CHAT_QUESTION_ANSWER_TAG = "forma: odpowiedź na pytanie czatu"

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
    if reading_likelihood >= 0.48:
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


def assess_extended_completeness(text: str, before: str = "", after: str = "", boundary_signals: list[str] | None = None) -> int:
    """A stricter full-thought check used by Extended analysis.

    The regular context score is intentionally forgiving so it can surface
    potential moments. This score is conservative: it rewards a complete,
    independently understandable sentence and penalizes clipped speech.
    """
    current = " ".join((text or "").split())
    tokens = re.findall(r"[^\W_]+", current.lower())
    context_score, _ = assess_context(current, before, after)
    self_contained = assess_self_containment(current, before, after)
    score = round((context_score * 0.46) + (self_contained * 0.46))
    if 6 <= len(tokens) <= 60:
        score += 5
    elif len(tokens) < 5:
        score -= 14
    elif len(tokens) > 90:
        score -= 8
    if current.endswith((".", "!", "?")):
        score += 6
    else:
        score -= 13
    if any(signal in {"start aligned to sentence", "end aligned to sentence", "extended to punchline"} for signal in (boundary_signals or [])):
        score += 4
    if re.match(r"^(a|ale|bo|więc|i|że|żeby|to|jak|który|która|które)\b", current, re.I) and before:
        score -= 12
    if after and not current.endswith((".", "!", "?")):
        score -= 10
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
    repeated_phrases = Counter(" ".join(tokens[index:index + 3]) for index in range(max(0, len(tokens) - 2)))
    repeated_phrase_count = max(repeated_phrases.values(), default=0)
    ui_cues = sum(1 for token in tokens if any(token.startswith(stem) for stem in _READING_CUE_STEMS))
    document_text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")
    document_tokens = re.findall(r"[a-z]+", document_text)
    document_cues = sum(1 for token in document_tokens if any(token.startswith(stem) for stem in _DOCUMENT_CUE_STEMS))
    if any(phrase in document_text for phrase in ("glos prawdy", "zabrania sie")):
        document_cues += 2
    sparse_punctuation = len(tokens) >= 28 and duration >= 22 and not re.search(r"[.!?]", text)
    matched_tags = [tag for tag in tags if tag in EMOTION_OR_OPINION_TAGS]
    reading = 0.0
    if len(tokens) >= 10 and word_rate < 1.25:
        reading += 0.22
    elif len(tokens) >= 10 and word_rate < 1.55:
        reading += 0.08
    if len(tokens) >= 10 and long_pauses >= max(2, len(tokens) // 9):
        reading += 0.25
    if fillers >= 2:
        reading += min(0.25, fillers * 0.08)
    if reading_words:
        reading += min(0.25, reading_words * 0.12)
    if repeated_phrase_count >= 2:
        reading += min(0.38, 0.18 + (repeated_phrase_count - 1) * 0.14)
    if ui_cues >= 2:
        reading += min(0.42, ui_cues * 0.11)
    if document_cues >= 2:
        # Formal vocabulary is common in genuine commentary too.  It is only
        # supporting evidence; the decisive confirmation comes from a visible
        # text-heavy game screen or another independent reading signal.
        reading += min(0.22, 0.06 + document_cues * 0.04)
    if sparse_punctuation and word_rate <= 4.2:
        reading += 0.18
    if len(tokens) >= 55 and duration >= 30 and not matched_tags:
        reading += 0.12
    reading = min(1.0, reading)

    signals: list[str] = []
    score = 35
    if 6 <= duration <= 30:
        score += 12
        signals.append("good clip length")
    elif 30 < duration <= 40:
        score += 3
        signals.append("long clip")
    if 1.4 <= word_rate <= 4.8:
        score += 10
        signals.append("natural speaking pace")
    if matched_tags:
        score += min(24, 8 * len(matched_tags))
        signals.append("emotion or opinion")
    if any(mark in text for mark in ("!", "?")):
        score += 7
        signals.append("expressive delivery")
    if reading >= 0.48:
        score -= 42
        signals.append("possible reading aloud")
    elif reading >= 0.3:
        score -= 12
        signals.append("some reading cues")
    if reading >= 0.48 and "possible reading aloud" not in signals[:3]:
        signals = signals[:2] + ["possible reading aloud"]
    return max(1, min(99, round(score))), signals[:3], round(reading, 3)


def assess_short_potential(
    text: str,
    start: float,
    end: float,
    tags: list[str],
    *,
    quality_score: int = 0,
    reading_likelihood: float = 0.0,
    logical_sense_score: int = -1,
    context_score: int = -1,
    self_contained_score: int = -1,
    extended_completeness_score: int = -1,
    game_reaction_score: int = 0,
    voice_expression_score: int = 0,
    moment_reaction_score: int = 0,
    chat_reaction_score: int = 0,
    chat_joy_score: int = 0,
) -> tuple[int, list[str]]:
    """Estimate whether a candidate works as a concise standalone short.

    This is intentionally independent from the discovery ranking.  The latter
    learns the user's preferences; this score answers a more practical
    question: does the clip have a compact shape, a clear thought and a reason
    to keep watching?
    """
    current = " ".join((text or "").split())
    tokens = re.findall(r"[^\W_]+", current.lower())
    duration = max(0.1, float(end) - float(start))
    tag_set = set(tags or [])
    signals: list[str] = []
    score = 18

    if 8 <= duration <= 28:
        score += 18
        signals.append("short-friendly length")
    elif 5 <= duration <= 38:
        score += 10
        signals.append("usable short length")
    elif duration > 50:
        score -= 18
        signals.append("too long for a short")
    elif duration > 38:
        score -= 7
        signals.append("long for a short")
    elif duration < 4:
        score -= 10
        signals.append("very brief clip")

    if self_contained_score >= 78:
        score += 23
        signals.append("stands on its own")
    elif self_contained_score >= 58:
        score += 11
        signals.append("mostly self-contained")
    elif 0 <= self_contained_score <= 38:
        score -= 18
        signals.append("needs surrounding context")

    if logical_sense_score >= 74:
        score += 14
        signals.append("complete thought")
    elif logical_sense_score >= 55:
        score += 6
    elif 0 <= logical_sense_score <= 38:
        score -= 15
        signals.append("unclear thought")

    if context_score >= 72:
        score += 7
    elif 0 <= context_score <= 35:
        score -= 8

    if extended_completeness_score >= 76:
        score += 9
        signals.append("verified complete ending")
    elif 0 <= extended_completeness_score <= 43:
        score -= 13
        signals.append("incomplete ending")

    hook_tags = {"humor", "forma: puenta", "forma: opinia", "forma: historia", "forma: krytyka", "forma: decyzja"}
    emotional_tags = {tag for tag in tag_set if tag.startswith("emocja:")} | (tag_set & EMOTION_OR_OPINION_TAGS)
    if tag_set & hook_tags:
        score += 8
        signals.append("clear content hook")
    if emotional_tags:
        score += 6
    if "forma: odpowied" in " ".join(tag_set).lower():
        score += 7
        signals.append("answer with context")

    if game_reaction_score >= 7 or moment_reaction_score >= 7:
        score += 10
        signals.append("game moment to reaction")
    elif voice_expression_score >= 7:
        score += 8
        signals.append("expressive voice")
    if chat_reaction_score >= 10:
        score += 7
        signals.append("chat reacted")
    if chat_joy_score >= 8:
        score += 5
        signals.append("chat amusement")
    if quality_score >= 78:
        score += 5

    if len(tokens) < 5:
        score -= 12
        signals.append("not enough spoken content")
    elif len(tokens) > 92:
        score -= 10
        signals.append("too much spoken content")
    if reading_likelihood >= 0.48 or "reading" in tag_set:
        score -= 42
        signals.append("likely reading aloud")
    elif reading_likelihood >= 0.30:
        score -= 14
        signals.append("reading cues")

    return max(1, min(99, round(score))), list(dict.fromkeys(signals))[:4]


def build_reference_prompt(transcripts: list[str], embeddings: list[list[float]]) -> str:
    tags = Counter(tag for text, vector in zip(transcripts, embeddings) for tag in infer_tags(text, vector))
    tags_text = ", ".join(name for name, _count in tags.most_common(4)) or "similar tone and context"
    stopwords = {"jest", "oraz", "tego", "który", "która", "bardzo", "tylko", "przez", "też", "więc", "żeby", "się", "nie", "dla"}
    words = [word.strip(".,!?;:-()[]{}\"'").lower() for text in transcripts for word in text.split()]
    keywords = [word for word, _count in Counter(word for word in words if len(word) >= 5 and word not in stopwords).most_common(6)]
    context = ", ".join(keywords) if keywords else "the tone, topic and structure of reference clips"
    return f"Find clips similar to the reference examples. Prioritize: {tags_text}. Typical context and wording: {context}."
