from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.pipeline_cache import (
    CACHE_SCHEMA_VERSION,
    CacheSafetyError,
    CacheSerializationError,
    PipelineCache,
    canonical_json,
    fingerprint_bytes,
    fingerprint_source_file,
    make_cache_key,
)


_DEFAULT_VALUE = object()


def _put(cache: PipelineCache, *, video_id: str = "video-1", value=_DEFAULT_VALUE):
    return cache.put(
        video_id=video_id,
        source_fingerprint="source:abc",
        stage="transcription",
        parameters={"model": "small", "language": "pl"},
        value={"segments": [1, 2, 3]} if value is _DEFAULT_VALUE else value,
    )


def _get(cache: PipelineCache, *, video_id: str = "video-1"):
    return cache.get(
        video_id=video_id,
        source_fingerprint="source:abc",
        stage="transcription",
        parameters={"language": "pl", "model": "small"},
    )


def test_canonical_parameters_make_key_independent_of_dictionary_order():
    first = make_cache_key("source", "audio features", {"b": 2, "a": {"z": 1, "x": 0}})
    second = make_cache_key("source", "audio features", {"a": {"x": 0, "z": 1}, "b": 2})

    assert first == second
    assert len(first) == 64
    assert canonical_json({"ą": 1, "a": 2}) == '{"a":2,"ą":1}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "bad"}, object()])
def test_canonical_json_rejects_ambiguous_or_unsupported_values(value):
    with pytest.raises(CacheSerializationError):
        canonical_json({"value": value})


def test_source_fingerprints_change_with_content(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"a" * 20_000)
    first = fingerprint_source_file(source, sample_bytes=4096)
    source.write_bytes(b"a" * 9_000 + b"b" * 2_000 + b"a" * 9_000)
    second = fingerprint_source_file(source, sample_bytes=4096)

    assert first.startswith("sampled-file-v1:sha256:")
    assert first != second
    assert fingerprint_bytes(b"clip") == fingerprint_bytes(bytearray(b"clip"))
    assert fingerprint_bytes(b"clip") != fingerprint_bytes(b"other")


def test_round_trip_validates_identity_and_supports_cached_none(tmp_path: Path):
    cache = PipelineCache(tmp_path / "cache")
    written = _put(cache, value=None)

    lookup = _get(cache)
    wrong_parameters = cache.get(
        video_id="video-1",
        source_fingerprint="source:abc",
        stage="transcription",
        parameters={"model": "large-v3", "language": "pl"},
    )

    assert written.path.is_file()
    assert lookup.hit is True and lookup.value is None and lookup.reason == "hit"
    assert wrong_parameters.hit is False and wrong_parameters.reason == "missing"
    document = json.loads(written.path.read_text(encoding="utf-8"))
    assert document["schema_version"] == CACHE_SCHEMA_VERSION
    assert document["cache_key"] == written.key
    assert document["parameters"] == {"language": "pl", "model": "small"}


def test_tampered_payload_is_a_miss_and_is_discarded(tmp_path: Path):
    cache = PipelineCache(tmp_path / "cache")
    written = _put(cache)
    document = json.loads(written.path.read_text(encoding="utf-8"))
    document["value"] = {"segments": [999]}
    written.path.write_text(json.dumps(document), encoding="utf-8")

    lookup = _get(cache)

    assert lookup.hit is False
    assert lookup.reason in {"value-checksum-mismatch", "entry-checksum-mismatch"}
    assert not written.path.exists()


def test_truncated_and_oversized_entries_are_safe_misses(tmp_path: Path):
    cache = PipelineCache(tmp_path / "cache", max_entry_bytes=1024)
    key, path = cache._entry_path(  # exercise a physically corrupt expected entry
        "video-1", "source:abc", "transcription", {"language": "pl", "model": "small"},
    )
    path.parent.mkdir(parents=True)
    path.write_text("{truncated", encoding="utf-8")
    invalid = _get(cache)
    assert invalid.key == key and invalid.reason == "invalid-json"
    assert not path.exists()

    path.write_bytes(b"x" * 1025)
    oversized = _get(cache)
    assert oversized.reason == "entry-too-large"
    assert not path.exists()


def test_atomic_write_failure_keeps_previous_entry_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    cache = PipelineCache(tmp_path / "cache")
    first = _put(cache, value={"version": 1})
    original = first.path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        _put(cache, value={"version": 2})

    assert first.path.read_bytes() == original
    assert list(first.path.parent.glob("*.tmp")) == []


def test_video_and_stage_names_never_become_unsafe_path_components(tmp_path: Path):
    cache = PipelineCache(tmp_path / "cache")
    written = cache.put(
        video_id="../../outside/video",
        source_fingerprint="source",
        stage="../../transcription: pl",
        parameters={},
        value={"ok": True},
    )

    assert written.path.resolve().is_relative_to(cache.version_root)
    assert "outside" not in written.path.parts
    assert written.path.name == f"{written.key}.json"


def test_symlink_in_cache_tree_is_never_followed(tmp_path: Path):
    cache = PipelineCache(tmp_path / "cache")
    outside = tmp_path / "outside"
    outside.mkdir()
    video_directory = cache._video_directory("video-1")
    try:
        video_directory.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Creating directory symlinks is not permitted on this machine")

    with pytest.raises(CacheSafetyError):
        _put(cache)
    assert list(outside.iterdir()) == []


def test_invalidate_one_video_does_not_touch_another(tmp_path: Path):
    cache = PipelineCache(tmp_path / "cache")
    first = _put(cache, video_id="video-1")
    second = _put(cache, video_id="video-2")

    preview = cache.invalidate_video("video-1", dry_run=True)
    assert preview.dry_run is True
    assert preview.planned == (first.path,)
    assert first.path.exists() and second.path.exists()

    result = cache.invalidate_video("video-1")
    assert result.removed == (first.path,)
    assert result.reclaimed_bytes > 0
    assert not first.path.exists()
    assert second.path.exists() and _get(cache, video_id="video-2").hit


def test_cleanup_is_dry_run_by_default_then_removes_old_entries(tmp_path: Path):
    now = 1_000_000.0
    cache = PipelineCache(tmp_path / "cache", clock=lambda: now)
    old_entry = _put(cache, video_id="old")
    os.utime(old_entry.path, (now - 500, now - 500))
    fresh_entry = _put(cache, video_id="fresh")

    preview = cache.cleanup(older_than_seconds=60, now=now)
    assert preview.dry_run is True
    assert preview.planned == (old_entry.path,)
    assert old_entry.path.exists() and fresh_entry.path.exists()

    result = cache.cleanup(older_than_seconds=60, now=now, dry_run=False)
    assert result.removed == (old_entry.path,)
    assert result.reclaimed_bytes > 0
    assert fresh_entry.path.exists()


def test_cleanup_removes_corrupt_entries_and_enforces_size_limit(tmp_path: Path):
    now = 2_000_000.0
    cache = PipelineCache(tmp_path / "cache", clock=lambda: now)
    first = _put(cache, video_id="one", value={"data": "a" * 200})
    second = _put(cache, video_id="two", value={"data": "b" * 200})
    os.utime(first.path, (now - 20, now - 20))
    corrupt = cache.version_root / "video-dead" / "stage-dead" / ("f" * 64 + ".json")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("not-json", encoding="utf-8")

    result = cache.cleanup(
        max_total_bytes=second.path.stat().st_size,
        remove_corrupt=True,
        now=now,
        dry_run=False,
    )

    assert corrupt in result.removed
    assert first.path in result.removed
    assert second.path.exists()


def test_cleanup_removes_only_old_abandoned_atomic_temporary_files(tmp_path: Path):
    now = 3_000_000.0
    cache = PipelineCache(tmp_path / "cache", clock=lambda: now)
    directory = cache._video_directory("video-1") / "stage-test"
    directory.mkdir(parents=True)
    old_temp = directory / ".entry.json.dead.tmp"
    young_temp = directory / ".entry.json.active.tmp"
    old_temp.write_text("old", encoding="utf-8")
    young_temp.write_text("young", encoding="utf-8")
    os.utime(old_temp, (now - 5000, now - 5000))

    result = cache.cleanup(now=now, temporary_older_than_seconds=3600, dry_run=False)

    assert old_temp in result.removed
    assert not old_temp.exists()
    assert young_temp.exists()
