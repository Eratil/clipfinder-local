"""Dependency graph for deterministic segment feature recomputation.

The analysis pipeline, chat import and editor can all change the same derived
segment fields.  This module provides one small, model-free DAG that answers
two questions consistently:

* which derived features are stale after an input changed;
* in which order should those features be rebuilt.

The graph deliberately does not infer semantic tags or embeddings.  It only
uses the tags already supplied by the caller and the local heuristics from
``tagging.py``.  Consequently it is safe to use in API requests, migrations
and tests without loading an ML model.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.tagging import (
    CHAT_QUESTION_ANSWER_TAG,
    CHAT_QUESTION_TAG,
    EMOTION_OR_OPINION_TAGS,
    GAME_REACTION_TAG,
    assess_clip_quality,
    assess_context,
    assess_extended_completeness,
    assess_extended_reading_likelihood,
    assess_extended_story_shape,
    assess_logical_sense,
    assess_self_containment,
    assess_short_potential,
    calibrate_quality_score,
    enrich_tags,
    score_moment_reaction,
)


FEATURE_SCHEMA_VERSION = "2"

SOURCE_TRANSCRIPT = "source:transcript"
SOURCE_TIMING = "source:timing"
SOURCE_TAGS = "source:tags"
SOURCE_CHAT = "source:chat"
SOURCE_AUDIO = "source:audio"
SOURCE_VISION = "source:vision"
SOURCE_CONTEXT = "source:context"
SOURCE_MODE = "source:mode"

SOURCE_FIELDS: dict[str, frozenset[str]] = {
    SOURCE_TRANSCRIPT: frozenset({"transcript", "text"}),
    SOURCE_TIMING: frozenset({
        "start", "end", "start_seconds", "end_seconds", "words", "word_timestamps",
    }),
    SOURCE_TAGS: frozenset({"tags", "semantic_tags"}),
    SOURCE_CHAT: frozenset({
        "chat_reaction_score", "chat_joy_score", "chat_message_count",
        "chat_unique_authors", "chat_surge", "chat_question_match_score",
        "chat_question_text",
    }),
    SOURCE_AUDIO: frozenset({
        "audio_event_score", "game_reaction_score", "voice_expression_score",
    }),
    SOURCE_VISION: frozenset({
        "vision_score", "visual_reading_likelihood", "reading_screen_score",
    }),
    SOURCE_CONTEXT: frozenset({
        "context_before", "context_after", "boundary_signals",
    }),
    SOURCE_MODE: frozenset({"analysis_mode"}),
}
SOURCE_NAMES = frozenset(SOURCE_FIELDS)

DYNAMIC_EVIDENCE_TAGS = frozenset({
    GAME_REACTION_TAG,
    CHAT_QUESTION_TAG,
    CHAT_QUESTION_ANSWER_TAG,
})

# A timing edit changes which media/chat/context interval belongs to the clip.
# Values not explicitly supplied in ``changed_fields`` are invalidated before
# the DAG runs, rather than accidentally reusing evidence from the old range.
_RANGE_EVIDENCE_DEFAULTS: dict[str, Any] = {
    "context_before": "",
    "context_after": "",
    "boundary_signals": [],
    "audio_event_score": 0,
    "game_reaction_score": 0,
    "voice_expression_score": 0,
    "vision_score": 0,
    "visual_reading_likelihood": 0.0,
    "chat_reaction_score": 0,
    "chat_joy_score": 0,
    "chat_message_count": 0,
    "chat_unique_authors": 0,
    "chat_surge": 0.0,
    "chat_messages": [],
    "chat_question_match_score": 0,
    "chat_question_text": "",
}

_TRANSCRIPT_EVIDENCE_DEFAULTS: dict[str, Any] = {
    "boundary_signals": [],
    "chat_question_match_score": 0,
    "chat_question_text": "",
}


FeatureComputer = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class FeatureNode:
    """One deterministic feature calculation in the DAG."""

    name: str
    dependencies: tuple[str, ...]
    outputs: tuple[str, ...]
    compute: FeatureComputer


@dataclass(frozen=True)
class FeatureGraphResult:
    """JSON-ready updates plus an audit trail of executed graph nodes."""

    updates: dict[str, Any]
    recomputed_nodes: tuple[str, ...]
    skipped_nodes: tuple[str, ...] = ()


def _first(state: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in state and state[key] is not None:
            return state[key]
    return default


def _float(state: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    value = _first(state, *keys, default=default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(state: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    value = _first(state, *keys, default=default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return list(decoded) if isinstance(decoded, list) else []
    return []


def _text(state: Mapping[str, Any]) -> str:
    return str(_first(state, "transcript", "text", default="") or "")


def _start(state: Mapping[str, Any]) -> float:
    return _float(state, "start_seconds", "start", default=0.0)


def _end(state: Mapping[str, Any]) -> float:
    return _float(state, "end_seconds", "end", default=_start(state) + 1.0)


def _words(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _list(_first(state, "word_timestamps", "words", default=[])) if isinstance(item, dict)]


def _tags(state: Mapping[str, Any]) -> list[str]:
    value = _first(state, "tags", "semantic_tags", default=[])
    return list(dict.fromkeys(str(tag) for tag in _list(value) if str(tag).strip()))


def _signals(state: Mapping[str, Any], key: str) -> list[str]:
    return list(dict.fromkeys(str(signal) for signal in _list(state.get(key)) if str(signal).strip()))


def _compute_speech_quality(state: dict[str, Any]) -> dict[str, Any]:
    score, signals, reading = assess_clip_quality(
        _text(state), _words(state), _start(state), _end(state), _tags(state),
    )
    return {
        "_base_quality_score": score,
        "_base_quality_signals": signals,
        "text_reading_likelihood": round(reading, 3),
    }


def _analysis_mode(state: Mapping[str, Any]) -> str:
    value = str(state.get("analysis_mode") or "default").strip().casefold()
    return value if value in {"fast", "default", "extended"} else "default"


def _compute_extended_analysis(state: dict[str, Any]) -> dict[str, Any]:
    """Produce conservative, deterministic signals only in Extended mode."""
    if _analysis_mode(state) != "extended":
        return {
            "extended_reading_likelihood": 0.0,
            "extended_hook_score": -1,
            "extended_ending_score": -1,
            "extended_story_signals": [],
            "extended_completeness_score": -1,
        }
    before = str(state.get("context_before") or "")
    after = str(state.get("context_after") or "")
    boundary_signals = _signals(state, "boundary_signals")
    text_reading = _float(state, "text_reading_likelihood", default=0.0)
    extended_reading = assess_extended_reading_likelihood(
        _text(state), before, after, text_reading,
    )
    hook, ending, story_signals = assess_extended_story_shape(
        _text(state), _words(state), before, after,
    )
    completeness = assess_extended_completeness(
        _text(state), before, after, boundary_signals,
    )
    completeness = round((completeness * 0.76) + (hook * 0.08) + (ending * 0.16))
    return {
        "extended_reading_likelihood": round(extended_reading, 3),
        "extended_hook_score": int(hook),
        "extended_ending_score": int(ending),
        "extended_story_signals": list(dict.fromkeys(story_signals)),
        "extended_completeness_score": max(1, min(99, int(completeness))),
    }


def _compute_effective_reading(state: dict[str, Any]) -> dict[str, Any]:
    screen_score = max(0.0, _float(state, "reading_screen_score", default=0.0))
    screen_reading = min(1.0, 0.52 + screen_score * 0.035) if screen_score > 0 else 0.0
    visual_reading = max(
        _float(state, "visual_reading_likelihood", default=0.0),
        screen_reading,
    )
    return {
        "visual_reading_likelihood": round(min(1.0, max(0.0, visual_reading)), 3),
        "reading_likelihood": round(min(1.0, max(
            _float(state, "text_reading_likelihood", default=0.0),
            visual_reading,
            _float(state, "extended_reading_likelihood", default=0.0),
        )), 3),
    }


def _compute_logical_sense(state: dict[str, Any]) -> dict[str, Any]:
    score = assess_logical_sense(_text(state))
    if _float(state, "reading_likelihood", default=0.0) >= 0.48:
        score = min(score, 35)
    return {"logical_sense_score": score}


def _context_is_available(state: Mapping[str, Any]) -> bool:
    # Empty strings are valid context data (there may simply be no speech in
    # that direction).  Absence of both keys means the caller did not load the
    # neighbouring transcript, so existing scores must not be overwritten.
    return "context_before" in state or "context_after" in state


def _compute_context(state: dict[str, Any]) -> dict[str, Any] | None:
    if not _context_is_available(state):
        return None
    before = str(state.get("context_before") or "")
    after = str(state.get("context_after") or "")
    score, signals = assess_context(_text(state), before, after)
    self_contained = assess_self_containment(_text(state), before, after)
    if _float(state, "reading_likelihood", default=0.0) >= 0.48:
        score = min(score, 35)
        self_contained = min(self_contained, 35)
    return {
        "context_score": score,
        "context_signals": signals,
        "self_contained_score": self_contained,
    }


def _compute_moment_reaction(state: dict[str, Any]) -> dict[str, Any]:
    score, stage = score_moment_reaction(
        _int(state, "game_reaction_score"),
        _int(state, "chat_reaction_score"),
        _int(state, "chat_joy_score"),
    )
    return {"moment_reaction_score": score, "moment_reaction_stage": stage}


def _compute_tag_enrichment(state: dict[str, Any]) -> dict[str, Any]:
    # Evidence-derived tags can become stale after a range, transcript, chat or
    # audio change.  Always strip them first and recreate only those supported
    # by the current state.
    tags = [tag for tag in _tags(state) if tag not in DYNAMIC_EVIDENCE_TAGS]
    enriched = enrich_tags(
        list(dict.fromkeys(tags)),
        logical_sense_score=_int(state, "logical_sense_score", default=-1),
        reading_likelihood=_float(state, "reading_likelihood"),
        game_reaction_score=_int(state, "game_reaction_score"),
        voice_expression_score=_int(state, "voice_expression_score"),
        chat_reaction_score=_int(state, "chat_reaction_score"),
        chat_joy_score=_int(state, "chat_joy_score"),
        vision_score=_int(state, "vision_score"),
        context_score=_int(state, "context_score", default=-1),
        self_contained_score=_int(state, "self_contained_score", default=-1),
        moment_reaction_score=_int(state, "moment_reaction_score"),
        moment_reaction_stage=str(state.get("moment_reaction_stage") or ""),
    )
    if _int(state, "game_reaction_score") >= 7:
        enriched.append(GAME_REACTION_TAG)
    if _int(state, "chat_question_match_score") >= 40:
        enriched.extend((CHAT_QUESTION_TAG, CHAT_QUESTION_ANSWER_TAG))
    return {"tags": list(dict.fromkeys(enriched))}


def _voice_led(tags: list[str]) -> bool:
    return bool(set(tags).intersection(EMOTION_OR_OPINION_TAGS)) or any(
        tag.startswith("forma:") for tag in tags
    )


def _compute_quality(state: dict[str, Any]) -> dict[str, Any]:
    # A full run receives the base from ``speech_quality``.  Selective runs
    # such as a chat-only update do not persist private ``_base`` fields, so
    # only those runs reconstruct the base instead of calibrating an already
    # calibrated quality score a second time.
    if "_base_quality_score" in state and "_base_quality_signals" in state:
        score = _int(state, "_base_quality_score")
        signals = _signals(state, "_base_quality_signals")
    else:
        score, signals, _fallback_reading = assess_clip_quality(
            _text(state), _words(state), _start(state), _end(state), _tags(state),
        )
    reading = _float(state, "reading_likelihood", default=0.0)
    game_reaction = _int(state, "game_reaction_score")
    voice_expression = _int(state, "voice_expression_score")
    tags = _tags(state)

    if game_reaction >= 7:
        score = min(99, score + min(10, game_reaction))
        signals.append("game sound followed by microphone reaction")
    elif _voice_led(tags) and voice_expression >= 7:
        score = min(99, score + min(6, voice_expression - 5))
        signals.append("expressive vocal delivery")
    elif voice_expression <= -7:
        score = max(1, score - 7)
        signals.append("monotonous vocal delivery")

    context_score = _int(state, "context_score", default=-1)
    if context_score >= 72:
        score = min(99, score + 3)
        signals.extend(_signals(state, "context_signals"))
    elif 0 <= context_score <= 38:
        score = max(1, score - 6)
        signals.extend(_signals(state, "context_signals"))
    if _int(state, "vision_score") >= 7:
        signals.append("visual action")

    # Boundary alignment is source evidence, not a separately accumulated
    # quality result.  Include it once in the final signal list and in the
    # Extended completeness calculation above.
    signals.extend(_signals(state, "boundary_signals"))

    hook = _int(state, "extended_hook_score", default=-1)
    ending = _int(state, "extended_ending_score", default=-1)
    completeness = _int(state, "extended_completeness_score", default=-1)
    if hook >= 0 and ending >= 0:
        score = max(1, min(99, score + round((hook - 50) * 0.10) + round((ending - 50) * 0.12)))
        signals.extend(_signals(state, "extended_story_signals"))
    if completeness >= 76:
        score = min(99, score + 6)
        signals.append("extended complete-thought verification")
    elif 0 <= completeness <= 43:
        score = max(1, score - 14)
        signals.append("extended incomplete-thought warning")
    text_reading = _float(state, "text_reading_likelihood", default=0.0)
    if reading >= 0.48 and reading > text_reading:
        signals.append("extended or visual reading verification")
    elif reading >= 0.30 and reading > text_reading + 0.08:
        signals.append("extended or visual reading cues")

    score, calibration_signal = calibrate_quality_score(
        score,
        duration=max(0.0, _end(state) - _start(state)),
        tags=tags,
        quality_signals=signals,
        reading_likelihood=reading,
        logical_sense_score=_int(state, "logical_sense_score", default=-1),
        context_score=context_score,
        self_contained_score=_int(state, "self_contained_score", default=-1),
        extended_completeness_score=completeness,
        game_reaction_score=game_reaction,
        voice_expression_score=voice_expression,
        moment_reaction_score=_int(state, "moment_reaction_score"),
    )
    if calibration_signal:
        signals.append(calibration_signal)
    return {
        "quality_score": score,
        "quality_signals": list(dict.fromkeys(signals)),
    }


def _compute_short_potential(state: dict[str, Any]) -> dict[str, Any]:
    score, signals = assess_short_potential(
        _text(state), _start(state), _end(state), _tags(state),
        quality_score=_int(state, "quality_score"),
        reading_likelihood=_float(state, "reading_likelihood"),
        logical_sense_score=_int(state, "logical_sense_score", default=-1),
        context_score=_int(state, "context_score", default=-1),
        self_contained_score=_int(state, "self_contained_score", default=-1),
        extended_completeness_score=_int(state, "extended_completeness_score", default=-1),
        game_reaction_score=_int(state, "game_reaction_score"),
        voice_expression_score=_int(state, "voice_expression_score"),
        moment_reaction_score=_int(state, "moment_reaction_score"),
        chat_reaction_score=_int(state, "chat_reaction_score"),
        chat_joy_score=_int(state, "chat_joy_score"),
    )
    return {"short_potential_score": score, "short_potential_signals": signals}


DEFAULT_NODES: tuple[FeatureNode, ...] = (
    FeatureNode(
        "speech_quality",
        (SOURCE_TRANSCRIPT, SOURCE_TIMING, SOURCE_TAGS),
        ("_base_quality_score", "_base_quality_signals", "text_reading_likelihood"),
        _compute_speech_quality,
    ),
    FeatureNode(
        "extended_analysis",
        (SOURCE_TRANSCRIPT, SOURCE_TIMING, SOURCE_CONTEXT, SOURCE_MODE, "speech_quality"),
        (
            "extended_reading_likelihood", "extended_hook_score",
            "extended_ending_score", "extended_story_signals",
            "extended_completeness_score",
        ),
        _compute_extended_analysis,
    ),
    FeatureNode(
        "effective_reading",
        (SOURCE_VISION, "speech_quality", "extended_analysis"),
        ("visual_reading_likelihood", "reading_likelihood"),
        _compute_effective_reading,
    ),
    FeatureNode(
        "logical_sense",
        (SOURCE_TRANSCRIPT, "effective_reading"),
        ("logical_sense_score",),
        _compute_logical_sense,
    ),
    FeatureNode(
        "context",
        (SOURCE_TRANSCRIPT, SOURCE_TIMING, SOURCE_CONTEXT, "effective_reading"),
        ("context_score", "context_signals", "self_contained_score"),
        _compute_context,
    ),
    FeatureNode(
        "moment_reaction",
        (SOURCE_AUDIO, SOURCE_CHAT),
        ("moment_reaction_score", "moment_reaction_stage"),
        _compute_moment_reaction,
    ),
    FeatureNode(
        "tag_enrichment",
        (
            SOURCE_TAGS, SOURCE_AUDIO, SOURCE_CHAT, SOURCE_VISION,
            "effective_reading", "logical_sense", "context", "moment_reaction",
        ),
        ("tags",),
        _compute_tag_enrichment,
    ),
    FeatureNode(
        "quality",
        (
            SOURCE_TRANSCRIPT, SOURCE_TIMING, SOURCE_AUDIO, SOURCE_VISION,
            SOURCE_CONTEXT, "speech_quality", "effective_reading", "extended_analysis",
            "logical_sense", "context", "moment_reaction", "tag_enrichment",
        ),
        ("quality_score", "quality_signals"),
        _compute_quality,
    ),
    FeatureNode(
        "short_potential",
        (
            SOURCE_TRANSCRIPT, SOURCE_TIMING, SOURCE_AUDIO, SOURCE_CHAT,
            "effective_reading", "extended_analysis", "logical_sense", "context", "moment_reaction",
            "tag_enrichment", "quality",
        ),
        ("short_potential_score", "short_potential_signals"),
        _compute_short_potential,
    ),
)


class FeatureGraph:
    """Validated DAG with field-to-source invalidation and selective execution."""

    def __init__(self, nodes: Iterable[FeatureNode]):
        ordered = tuple(nodes)
        self._nodes = {node.name: node for node in ordered}
        if len(self._nodes) != len(ordered):
            raise ValueError("Feature node names must be unique.")
        self._declaration_order = tuple(node.name for node in ordered)
        self._output_producers = self._validate_outputs(ordered)
        self._validate_dependencies()
        self._topological_order = self._sort()

    @property
    def topological_order(self) -> tuple[str, ...]:
        return self._topological_order

    @property
    def dependencies(self) -> dict[str, tuple[str, ...]]:
        return {name: self._nodes[name].dependencies for name in self._declaration_order}

    @property
    def output_producers(self) -> dict[str, str]:
        """Expose the validated one-output/one-producer contract for diagnostics."""
        return dict(self._output_producers)

    @staticmethod
    def _validate_outputs(nodes: tuple[FeatureNode, ...]) -> dict[str, str]:
        producers: dict[str, str] = {}
        for node in nodes:
            for output in node.outputs:
                if not str(output).strip():
                    raise ValueError(f"Feature node {node.name} declares an empty output name")
                if output in producers:
                    raise ValueError(
                        f"Duplicate feature output {output!r}: "
                        f"{producers[output]} and {node.name}"
                    )
                producers[output] = node.name
        return producers

    def _validate_dependencies(self) -> None:
        known = set(self._nodes) | set(SOURCE_NAMES)
        for node in self._nodes.values():
            unknown = set(node.dependencies) - known
            if unknown:
                raise ValueError(f"Unknown dependencies for {node.name}: {sorted(unknown)}")

    def _sort(self) -> tuple[str, ...]:
        pending = set(self._nodes)
        result: list[str] = []
        while pending:
            ready = [
                name for name in self._declaration_order
                if name in pending
                and not any(dependency in pending for dependency in self._nodes[name].dependencies)
            ]
            if not ready:
                raise ValueError(f"Feature dependency cycle: {sorted(pending)}")
            for name in ready:
                pending.remove(name)
                result.append(name)
        return tuple(result)

    def _dirty_roots(self, changed_fields: Iterable[str]) -> tuple[set[str], set[str]]:
        changed = {str(field) for field in changed_fields}
        sources = {
            source for source, fields in SOURCE_FIELDS.items()
            if source in changed or bool(fields.intersection(changed))
        }
        nodes = {name for name in self._nodes if name in changed}
        nodes.update(
            producer for field, producer in self._output_producers.items() if field in changed
        )
        return sources, nodes

    @staticmethod
    def _fresh_fields(changed_fields: set[str]) -> set[str]:
        fresh = set(changed_fields)
        for source, fields in SOURCE_FIELDS.items():
            if source in changed_fields:
                fresh.update(fields)
        return fresh

    def _invalidated_evidence(self, changed_fields: set[str]) -> dict[str, Any]:
        fresh = self._fresh_fields(changed_fields)
        transcript_changed = bool(
            SOURCE_TRANSCRIPT in changed_fields
            or SOURCE_FIELDS[SOURCE_TRANSCRIPT].intersection(changed_fields)
        )
        timing_changed = bool(
            SOURCE_TIMING in changed_fields
            or SOURCE_FIELDS[SOURCE_TIMING].intersection(changed_fields)
        )
        defaults: dict[str, Any] = {}
        if transcript_changed:
            defaults.update(_TRANSCRIPT_EVIDENCE_DEFAULTS)
        if timing_changed:
            defaults.update(_RANGE_EVIDENCE_DEFAULTS)
        # A field listed by the caller is fresh even if another changed source
        # would normally invalidate it (e.g. timing + freshly rescored audio).
        return {key: value for key, value in defaults.items() if key not in fresh}

    def affected_nodes(self, changed_fields: Iterable[str] | None = None) -> tuple[str, ...]:
        if changed_fields is None:
            return self.topological_order
        dirty_sources, dirty_nodes = self._dirty_roots(changed_fields)
        affected: set[str] = set()
        for name in self.topological_order:
            dependencies = self._nodes[name].dependencies
            if (
                name in dirty_nodes
                or any(dependency in dirty_sources for dependency in dependencies)
                or any(dependency in affected for dependency in dependencies)
            ):
                affected.add(name)
        return tuple(name for name in self.topological_order if name in affected)

    def recompute(
        self,
        segment: Mapping[str, Any],
        changed_fields: Iterable[str] | None = None,
    ) -> FeatureGraphResult:
        """Recompute stale nodes without mutating ``segment``.

        Values in ``updates`` are normal Python values (lists are not encoded
        as JSON); the persistence layer remains responsible for serialization.
        Unknown changed fields are intentionally a no-op, which lets API
        callers pass full PATCH field sets without coupling to this module.
        """
        state = dict(segment)
        changed = None if changed_fields is None else {str(field) for field in changed_fields}
        invalidated = self._invalidated_evidence(changed) if changed is not None else {}
        state.update(invalidated)
        updates: dict[str, Any] = dict(invalidated)
        recomputed: list[str] = []
        skipped: list[str] = []
        effective_changed = None if changed is None else changed | set(invalidated)
        for name in self.affected_nodes(effective_changed):
            values = self._nodes[name].compute(state)
            if values is None:
                skipped.append(name)
                continue
            undeclared = set(values) - set(self._nodes[name].outputs)
            if undeclared:
                raise RuntimeError(
                    f"Feature node {name} returned undeclared outputs: {sorted(undeclared)}"
                )
            state.update(values)
            updates.update({key: value for key, value in values.items() if not key.startswith("_")})
            recomputed.append(name)
        return FeatureGraphResult(updates, tuple(recomputed), tuple(skipped))


FEATURE_GRAPH = FeatureGraph(DEFAULT_NODES)


def recompute_segment_features(
    segment: Mapping[str, Any],
    changed_fields: Iterable[str] | None = None,
) -> FeatureGraphResult:
    """Public convenience wrapper around the application's default DAG."""
    return FEATURE_GRAPH.recompute(segment, changed_fields)
