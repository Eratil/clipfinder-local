import re
import unicodedata
from collections import Counter

from app.services.embeddings import cosine, embed_texts
from app.services.tag_taxonomy import (
    CHAT_QUESTION_ANSWER_TAG,
    CHAT_QUESTION_TAG,
    CONTEXT_TAG_PREFIXES,
    GAME_REACTION_TAG,
    GAME_REACTION_MIN_SCORE,
    canonicalize_tags,
    tag_category,
)

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
_FILLERS = {"yyy", "eee", "hmm", "um", "jakby", "znaczy"}

# NFKD decomposes most Polish diacritics, but not the stroked letter ``ł``.
# Translate it first so keyword rules treat e.g. "udało się" and
# "udalo sie" identically.
_POLISH_ASCII_TRANSLATION = str.maketrans({"\u0142": "l", "\u0141": "L"})


def _ascii_normalize(value: str) -> str:
    return unicodedata.normalize(
        "NFKD", (value or "").translate(_POLISH_ASCII_TRANSLATION)
    ).encode("ascii", "ignore").decode("ascii")
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
_tag_vectors: list[list[float]] | None = None


def deduplicate_content_tags(tags: list[str], limit: int = 6) -> list[str]:
    """Keep one precise semantic tag from each content category.

    The caller supplies detailed lexical tags before broad/semantic ones, so a
    specific label wins naturally without hard-coding every Polish synonym.
    Diagnostic tags are canonicalized too. ``enrich_tags`` removes stale
    diagnostics before rebuilding them from current evidence.
    """
    selected: list[str] = []
    used: set[str] = set()
    for tag in canonicalize_tags(tags):
        category = tag_category(tag)
        if category in used:
            continue
        selected.append(tag)
        used.add(category)
        if len(selected) >= limit:
            break
    return selected


def infer_tags(text: str, embedding: list[float], limit: int = 6) -> list[str]:
    global _tag_vectors
    lowered = text.lower()
    lexical = [name for name, _description, markers in TAG_DEFINITIONS if any(marker in lowered for marker in markers)]
    detailed = detailed_lexical_tags(text)
    if _tag_vectors is None:
        _tag_vectors = embed_texts([item[1] for item in TAG_DEFINITIONS])
    semantic = [(cosine(embedding, vector), TAG_DEFINITIONS[index][0]) for index, vector in enumerate(_tag_vectors)]
    semantic_tags = [name for score, name in sorted(semantic, reverse=True) if score >= 0.34]
    return deduplicate_content_tags(list(dict.fromkeys(detailed + lexical + semantic_tags)), limit)


def detailed_lexical_tags(text: str) -> list[str]:
    lowered = (text or "").lower()
    return canonicalize_tags(
        name for name, markers in DETAILED_TAG_DEFINITIONS
        if any(marker in lowered for marker in markers)
    )


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
    # Rebuild diagnostic labels every time a score changes.  ``reading`` was a
    # legacy duplicate of ``format: czytanie`` and is intentionally dropped.
    semantic = [tag for tag in canonicalize_tags(tags) if not tag.startswith(CONTEXT_TAG_PREFIXES)]
    cleaned = deduplicate_content_tags(semantic)
    context: list[str] = []
    if reading_likelihood >= 0.48:
        context.append("format: czytanie")
    # Structure is one mutually exclusive diagnostic category.  The detailed
    # numeric scores stay available in the right-hand panel.
    if 0 <= logical_sense_score <= 35:
        context.append("struktura: urwana wypowiedź")
    elif 0 <= context_score <= 38:
        context.append("struktura: wymaga kontekstu")
    elif 0 <= self_contained_score <= 35:
        context.append("struktura: zależny od kontekstu")
    elif self_contained_score >= 75:
        context.append("struktura: samowystarczalny")
    elif logical_sense_score >= 68:
        context.append("struktura: samodzielna myśl")
    if voice_expression_score >= 7:
        context.append("wypowiedź: ekspresyjna")
    elif voice_expression_score <= -7:
        context.append("wypowiedź: jednostajna")
    # Game/chat/vision evidence is exposed as scores and signals in Detailed
    # scoring.  It is not duplicated as several near-identical card tags.
    return canonicalize_tags(cleaned + context)


def score_moment_reaction(game_reaction_score: int, chat_reaction_score: int = 0, chat_joy_score: int = 0) -> tuple[int, str]:
    """Score a causal-looking game event, voice reaction, then chat response."""
    if game_reaction_score < GAME_REACTION_MIN_SCORE:
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


def assess_extended_story_shape(text: str, words: list[dict] | None = None, before: str = "", after: str = "") -> tuple[int, int, list[str]]:
    """Score the opening hook and the closing payoff of an Extended clip.

    This is intentionally conservative.  It does not try to invent a joke or
    a reaction; it only checks whether the start gives the viewer a reason to
    stay and whether the end resolves the spoken thought.
    """
    current = " ".join((text or "").split())
    tokens = re.findall(r"[^\W_]+", current.lower())
    if not tokens:
        return 1, 1, ["empty spoken fragment"]
    early_words = []
    if words:
        first_start = float(words[0].get("start") or 0.0)
        early_words = [str(word.get("word") or "") for word in words if float(word.get("start") or first_start) <= first_start + 3.5]
    early = " ".join(early_words) if early_words else " ".join(tokens[:12])
    early_normalized = _ascii_normalize(early.lower())
    # A grammatical sentence is not automatically a hook or payoff. Start
    # from a neutral-low value and require explicit early tension/claim and a
    # resolving ending before awarding those labels.
    hook = 34
    ending = 34
    signals: list[str] = []
    first_tokens = re.findall(r"[^\W_]+", early.lower())
    filler_count = sum(token in _FILLERS for token in first_tokens)
    starts_with_link = bool(re.match(r"^(a|ale|bo|wiec|i|ze|zeby|to|jak|ktory|ktora|ktore)\b", current, re.I))
    if starts_with_link and before:
        hook -= 23
        signals.append("weak opening depends on earlier speech")
    if filler_count:
        hook -= min(20, filler_count * 8)
    if len(first_tokens) >= 5:
        hook += 5
    hook_evidence = 0
    if "?" in early or re.search(r"^(?:co|jak|dlaczego|czy|kto|gdzie)\b", early_normalized, re.I):
        hook += 15
        hook_evidence += 1
    if re.search(r"\b(?:co jest|nie wierze|serio|niemozliwe|o kurwa|masakra)\b", early_normalized, re.I):
        hook += 13
        hook_evidence += 1
    if re.search(r"\b(?:moim zdaniem|uwazam|mysle ze|dla mnie|najlepszy|najgorszy|absurd|problem jest)\b", early_normalized, re.I):
        hook += 11
        hook_evidence += 1
    if re.search(r"\b(?:zobaczcie|sluchajcie|wyobrazcie sobie|teraz uwaga)\b", early_normalized, re.I):
        hook += 10
        hook_evidence += 1
    if "!" in early and hook_evidence:
        hook += 4
    if hook_evidence == 0:
        hook = min(hook, 52)
    if len(tokens) < 6:
        hook -= 14

    complete_end = current.endswith((".", "!", "?"))
    if complete_end:
        ending += 12
    else:
        ending -= 20
        signals.append("ending does not resolve the thought")
    if tokens[-1] in _TRAILING_CONNECTORS:
        ending -= 14
    closing_words = " ".join(tokens[-12:])
    closing_normalized = _ascii_normalize(closing_words)
    payoff_evidence = 0
    if re.search(r"\b(?:wiec|jednak|dlatego|okazalo sie|w koncu|finalnie|koniec)\b", closing_normalized, re.I):
        ending += 13
        payoff_evidence += 1
    if re.search(r"\b(?:najlepsze|najgorsze|absurdalne|genialne|beznadziejne|warto|nie warto)\b", closing_normalized, re.I):
        ending += 10
        payoff_evidence += 1
    if re.search(r"\b(?:wygralem|przegralem|udalo sie|nie udalo sie|zginalem|dziala|nie dziala)\b", closing_normalized, re.I):
        ending += 10
        payoff_evidence += 1
    if complete_end and payoff_evidence == 0:
        ending = min(ending, 54)
    if after and not complete_end:
        ending -= 12
    if hook >= 66:
        signals.append("clear opening hook")
    if ending >= 68:
        signals.append("resolved ending or payoff")
    return max(1, min(99, round(hook))), max(1, min(99, round(ending))), signals[:2]


def assess_opening_clarity(text: str, words: list[dict] | None = None) -> tuple[int, list[str]]:
    """Score whether the first two spoken seconds establish a clear premise.

    This intentionally uses spoken content only. Stream alerts, emotes and
    overlays can create plenty of motion in a recording, but must never count
    as a content hook.
    """
    current = " ".join((text or "").split())
    tokens = re.findall(r"[^\W_]+", current.lower())
    if not tokens:
        return 1, ["no spoken opening"]
    early_words: list[str] = []
    if words:
        first_start = float(words[0].get("start") or 0.0)
        early_words = [
            str(word.get("word") or "")
            for word in words
            if float(word.get("start") or first_start) <= first_start + 2.0
        ]
    opening = " ".join(early_words) if early_words else " ".join(tokens[:7])
    normalized = _ascii_normalize(opening.lower())
    opening_tokens = re.findall(r"[a-z]+", normalized)
    score = 26
    signals: list[str] = []
    if len(opening_tokens) >= 4:
        score += 10
    else:
        score -= 12
        signals.append("opening is too sparse")
    if opening_tokens and opening_tokens[0] in {"a", "ale", "bo", "wiec", "i", "ze", "zeby", "to"}:
        score -= 18
        signals.append("opening starts mid-thought")
    filler_count = sum(token in _FILLERS for token in opening_tokens)
    if filler_count:
        score -= min(18, filler_count * 9)
        signals.append("filler-heavy opening")

    premise_evidence = 0
    if "?" in opening or re.search(r"^(?:co|jak|dlaczego|czy|kto|gdzie)\b", normalized, re.I):
        score += 16
        premise_evidence += 1
    if re.search(r"\b(?:co jest|nie wierze|serio|niemozliwe|masakra|nagle)\b", normalized, re.I):
        score += 13
        premise_evidence += 1
    if re.search(r"\b(?:moim zdaniem|uwazam|mysle ze|dla mnie|najlepszy|najgorszy|problem|dzisiaj)\b", normalized, re.I):
        score += 11
        premise_evidence += 1
    if re.search(r"\b(?:zobaczcie|sluchajcie|wyobrazcie sobie|teraz uwaga)\b", normalized, re.I):
        score += 10
        premise_evidence += 1
    if premise_evidence:
        signals.append("clear first-two-seconds premise")
    else:
        score = min(score, 52)
        signals.append("opening has no clear premise")
    return max(1, min(99, round(score))), signals[:2]


def assess_extended_punchline(text: str) -> tuple[int, list[str]]:
    """Score an actual turn, outcome or comedic payoff near the ending.

    A complete sentence is not enough: this feature looks for a late change of
    direction, an outcome, or an explicit joke/reveal and is kept independent
    from the general ending score.
    """
    current = " ".join((text or "").split())
    tokens = re.findall(r"[^\W_]+", current.lower())
    if len(tokens) < 8:
        return 1, ["too little speech for a punchline"]
    split_at = max(5, round(len(tokens) * 0.62))
    setup = _ascii_normalize(" ".join(tokens[:split_at]))
    closing = _ascii_normalize(" ".join(tokens[split_at:]))
    score = 24
    signals: list[str] = []
    if current.endswith((".", "!", "?")):
        score += 8
    else:
        score -= 16
        signals.append("ending cuts off before payoff")

    turn_evidence = 0
    if re.search(r"\b(?:ale jednak|a jednak|tylko ze|okazalo sie|nagle|w koncu|finalnie|plot twist)\b", closing, re.I):
        score += 23
        turn_evidence += 1
    if re.search(r"\b(?:wygralem|przegralem|udalo sie|nie udalo sie|zginalem|przegrana|wygrana|dziala|nie dziala)\b", closing, re.I):
        score += 16
        turn_evidence += 1
    if re.search(r"\b(?:najlepsze jest|najgorsze jest|absurdalne|genialne|beznadziejne|oczywiscie ze nie|no i)\b", closing, re.I):
        score += 15
        turn_evidence += 1
    if re.search(r"\b(?:haha+|heh+|xd+|lol+)\b", closing, re.I):
        score += 10
        turn_evidence += 1
    if turn_evidence and not any(marker in setup for marker in ("ale jednak", "a jednak", "okazalo sie", "nagle", "w koncu")):
        score += 7
        signals.append("late turn or reveal")
    if turn_evidence >= 2:
        score += 8
    if turn_evidence:
        signals.append("clear punchline or outcome")
    else:
        score = min(score, 48)
        signals.append("no verified punchline")
    return max(1, min(99, round(score))), signals[:2]


def assess_extended_reading_likelihood(text: str, before: str = "", after: str = "", base_likelihood: float = 0.0) -> float:
    """Use stricter, combined evidence for game-note and task reading.

    Formal words alone are deliberately not enough: a streamer can use them
    while expressing a real opinion.  In Extended mode we flag the clip only
    when formal/document language is paired with directive or quote-like
    wording and there is no clear personal commentary.
    """
    normalized = _ascii_normalize(" ".join((text or "").lower().split()))
    tokens = re.findall(r"[a-z]+", normalized)
    formal_hits = sum(1 for token in tokens if any(token.startswith(stem) for stem in _DOCUMENT_CUE_STEMS))
    directive_hits = len(re.findall(
        r"\b(?:zabrania sie|nalezy|ma(?:ja|) zostac|powinien(?:no|niscie)?|"
        r"zachowaj(?:cie)?|zainstaluj(?:cie)?|wejdz(?:cie)?|zbierz(?:cie)?|"
        r"kontynuuj|przechowywania|ukrywania)\b",
        normalized,
    ))
    quote_markers = sum(marker in normalized for marker in (
        "glos prawdy", "dyrektywa", "rozporzadzenie", "zabrania sie", "wszystkie ",
    ))
    personal_commentary = bool(re.search(
        r"\b(?:moim zdaniem|uwazam|mysle|wydaje mi sie|dla mnie|ja bym|wedlug mnie)\b",
        normalized,
    ))
    sentence_count = len(re.findall(r"[.!?]", text or ""))

    boost = 0.0
    if not personal_commentary and (formal_hits >= 3 or directive_hits >= 2):
        boost += 0.42
    elif not personal_commentary and formal_hits >= 2 and directive_hits >= 1:
        boost += 0.32
    if not personal_commentary and formal_hits >= 2 and quote_markers >= 1:
        boost += 0.16
    if not personal_commentary and len(tokens) >= 22 and sentence_count >= 3 and (formal_hits >= 2 or directive_hits >= 2):
        boost += 0.10
    # Nearby speech does not prove reading, but a clip that contains a block
    # of document language between unrelated remarks is more suspicious.
    if boost and before and after and not personal_commentary:
        boost += 0.05
    return round(min(1.0, max(float(base_likelihood or 0.0), float(base_likelihood or 0.0) + boost)), 3)


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
    document_text = _ascii_normalize(text.lower())
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


def calibrate_quality_score(
    score: float,
    *,
    duration: float,
    tags: list[str],
    quality_signals: list[str],
    reading_likelihood: float,
    logical_sense_score: int,
    context_score: int,
    self_contained_score: int,
    extended_completeness_score: int,
    game_reaction_score: int,
    voice_expression_score: int,
    moment_reaction_score: int,
) -> tuple[int, str | None]:
    """Apply final, deliberately strict calibration to a quality score.

    Quality may accumulate several related bonuses during the pipeline.  This
    final pass keeps an ordinary fluent fragment from reaching 99 merely
    because those correlated signals all happen to be present.
    """
    value = float(score)
    tag_set = set(tags or [])
    emotional_delivery = bool({"humor", "gniew", "zaskoczenie", "radość", "złość"}.intersection(tag_set))
    strong_delivery = (
        emotional_delivery
        or "expressive delivery" in quality_signals
        or game_reaction_score >= GAME_REACTION_MIN_SCORE
        or moment_reaction_score >= 10
        or voice_expression_score >= 10
    )
    ideal_presentation = (
        6 <= duration <= 30
        and "natural speaking pace" in quality_signals
        and reading_likelihood < 0.20
    )

    if reading_likelihood >= 0.48:
        value = min(value, 22)
    elif reading_likelihood >= 0.30:
        value = min(value, 68)
    if self_contained_score < 58 or logical_sense_score < 55:
        value = min(value, 72)
    elif self_contained_score < 72 or logical_sense_score < 70:
        value = min(value, 82)
    if 0 <= context_score < 52:
        value = min(value, 86)
    if extended_completeness_score < 0:
        value = min(value, 93)
    elif extended_completeness_score < 65:
        value = min(value, 88)
    if not strong_delivery:
        value = min(value, 90)

    exceptional = (
        ideal_presentation
        and self_contained_score >= 85
        and logical_sense_score >= 84
        and context_score >= 72
        and extended_completeness_score >= 82
        and strong_delivery
    )
    if not exceptional:
        value = min(value, 95)
    return max(1, min(99, round(value))), "exceptional quality criteria met" if exceptional else None


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
    extended_hook_score: int = -1,
    extended_ending_score: int = -1,
    opening_clarity_score: int = -1,
    extended_punchline_score: int = -1,
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

    # Structure is an eligibility gate, not proof that people would watch the
    # clip.  The previous version added four correlated checks (logic,
    # context, self-containment and extended completeness) independently,
    # allowing a merely well-formed sentence to score like a strong short.
    score = 20
    structurally_strong = False
    if 8 <= duration <= 28:
        score += 15
        signals.append("short-friendly length")
    elif 5 <= duration <= 38:
        score += 8
        signals.append("usable short length")
    elif duration > 50:
        score -= 20
        signals.append("too long for a short")
    elif duration > 38:
        score -= 9
        signals.append("long for a short")
    elif duration < 4:
        score -= 12
        signals.append("very brief clip")

    if self_contained_score >= 78 and logical_sense_score >= 74 and context_score >= 62:
        score += 18
        structurally_strong = True
        signals.append("stands on its own")
    elif self_contained_score >= 58 and logical_sense_score >= 55:
        score += 8
        signals.append("mostly self-contained")
    elif self_contained_score < 58 or logical_sense_score < 55:
        score -= 14
        signals.append("needs surrounding context")

    if extended_completeness_score >= 76:
        score += 8
        signals.append("verified complete ending")
    elif 0 <= extended_completeness_score <= 43:
        score -= 15
        signals.append("incomplete ending")

    hook_tags = {"humor", "forma: puenta", "forma: opinia", "forma: historia", "forma: krytyka", "forma: decyzja"}
    emotional_tags = {tag for tag in tag_set if tag.startswith("emocja:")} | (tag_set & EMOTION_OR_OPINION_TAGS)
    has_content_hook = bool(tag_set & hook_tags)
    # This is assigned only after chat.py has semantically matched a viewer's
    # earlier question to this spoken response.  Do not infer it from a
    # question-shaped sentence or generic semantic tag.
    has_answer = CHAT_QUESTION_ANSWER_TAG in tag_set
    verified_game_reaction = (
        game_reaction_score >= GAME_REACTION_MIN_SCORE
        or moment_reaction_score >= GAME_REACTION_MIN_SCORE
    )
    expressive_voice = voice_expression_score >= 10
    has_chat_reaction = chat_reaction_score >= 12 or chat_joy_score >= 8
    content_interest = has_content_hook or bool(emotional_tags)
    story_shape = extended_hook_score >= 66 and extended_ending_score >= 68
    clear_opening = opening_clarity_score >= 64
    punchline = extended_punchline_score >= 66
    proof_count = sum((verified_game_reaction, expressive_voice, has_chat_reaction, has_answer, content_interest, story_shape, punchline))
    attention_strength = 0

    # These are independent reasons to keep watching.  Unlike the structural
    # foundation above, they can raise a clip into the top range.
    if content_interest:
        attention_strength += 7
        signals.append("clear content hook")
    if story_shape:
        attention_strength += 10
        signals.append("hook and payoff verified")
    if clear_opening:
        attention_strength += 6
        signals.append("clear first-two-seconds premise")
    if punchline:
        attention_strength += 12
        signals.append("late punchline or outcome verified")
    if has_answer:
        attention_strength += 12
        signals.append("answer with context")
    if verified_game_reaction:
        attention_strength += 18
        signals.append("game moment to reaction")
    if expressive_voice:
        attention_strength += 8 if verified_game_reaction else 12
        signals.append("expressive voice")
    if chat_reaction_score >= 10:
        attention_strength += 8
        signals.append("chat reacted")
    if chat_joy_score >= 8:
        attention_strength += 6
        signals.append("chat amusement")
    if proof_count >= 2:
        attention_strength += 4
    score += attention_strength

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

    # A useful structure is necessary, but not enough.  A candidate without a
    # concrete attention signal is capped as a clean excerpt rather than being
    # presented as a likely Short.
    if self_contained_score < 58 or logical_sense_score < 55:
        score = min(score, 72)
    elif not structurally_strong:
        score = min(score, 82)
    if 0 <= context_score < 52:
        score = min(score, 86)
    if extended_completeness_score < 0:
        score = min(score, 92)
    elif extended_completeness_score < 65:
        score = min(score, 88)
    if attention_strength == 0:
        score = min(score, 65)
    elif proof_count == 1:
        score = min(score, 88)

    exceptional = (
        8 <= duration <= 28
        and self_contained_score >= 85
        and logical_sense_score >= 84
        and context_score >= 72
        and extended_completeness_score >= 82
        and quality_score >= 80
        and reading_likelihood < 0.20
        and proof_count >= 2
        and attention_strength >= 24
        and clear_opening
        and (punchline or has_answer or verified_game_reaction)
    )
    if exceptional:
        signals.append("exceptional short criteria met")
    else:
        score = min(score, 95)

    return max(1, min(99, round(score))), list(dict.fromkeys(signals))[:4]


def build_reference_prompt(transcripts: list[str], embeddings: list[list[float]]) -> str:
    tags = Counter(tag for text, vector in zip(transcripts, embeddings) for tag in infer_tags(text, vector))
    tags_text = ", ".join(name for name, _count in tags.most_common(4)) or "similar tone and context"
    stopwords = {"jest", "oraz", "tego", "który", "która", "bardzo", "tylko", "przez", "też", "więc", "żeby", "się", "nie", "dla"}
    words = [word.strip(".,!?;:-()[]{}\"'").lower() for text in transcripts for word in text.split()]
    keywords = [word for word, _count in Counter(word for word in words if len(word) >= 5 and word not in stopwords).most_common(6)]
    context = ", ".join(keywords) if keywords else "the tone, topic and structure of reference clips"
    return f"Find clips similar to the reference examples. Prioritize: {tags_text}. Typical context and wording: {context}."
