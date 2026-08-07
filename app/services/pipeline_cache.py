"""Versioned, filesystem-backed cache for expensive pipeline stages.

The cache is deliberately independent from the database and the analysis
pipeline.  A caller supplies a stable video id, a source fingerprint, a stage
name and all parameters which can affect the result.  Values are JSON data;
model-specific objects should be converted to ordinary dictionaries/lists by
the caller.

Cache files are treated as disposable.  Invalid, truncated or tampered files
are reported as misses and safely discarded.  User-controlled names never
become path components: video and stage directories are derived from hashes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat as stat_module
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 1
SOURCE_FINGERPRINT_VERSION = 1
DEFAULT_MAX_ENTRY_BYTES = 256 * 1024 * 1024
DEFAULT_SAMPLE_BYTES = 256 * 1024


class PipelineCacheError(RuntimeError):
    """Base class for cache-specific failures."""


class CacheSafetyError(PipelineCacheError):
    """Raised when a cache path does not remain inside the managed root."""


class CacheSerializationError(PipelineCacheError, ValueError):
    """Raised when parameters or a payload are not canonical JSON data."""


@dataclass(frozen=True)
class CacheLookup:
    """Result of a cache lookup; ``hit`` distinguishes a cached ``None``."""

    key: str
    path: Path
    hit: bool
    value: Any = None
    reason: str = "missing"


@dataclass(frozen=True)
class CacheWriteResult:
    key: str
    path: Path
    size: int
    replaced: bool


@dataclass(frozen=True)
class CacheInvalidationResult:
    video_id: str
    planned: tuple[Path, ...]
    removed: tuple[Path, ...]
    skipped: tuple[Path, ...]
    errors: tuple[tuple[Path, str], ...]
    reclaimed_bytes: int
    dry_run: bool


@dataclass(frozen=True)
class CacheCleanupResult:
    scanned: int
    planned: tuple[Path, ...]
    removed: tuple[Path, ...]
    skipped: tuple[Path, ...]
    errors: tuple[tuple[Path, str], ...]
    reclaimed_bytes: int
    dry_run: bool


@dataclass(frozen=True)
class _ObservedFile:
    path: Path
    size: int
    modified_ns: int
    device: int
    inode: int
    valid_entry: bool


def _validate_json_value(value: Any, *, label: str, path: str = "$", depth: int = 0) -> None:
    if depth > 100:
        raise CacheSerializationError(f"{label} is nested too deeply at {path}")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CacheSerializationError(f"{label} contains a non-finite number at {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CacheSerializationError(f"{label} contains a non-string key at {path}")
            _validate_json_value(item, label=label, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for index, item in enumerate(value):
            _validate_json_value(item, label=label, path=f"{path}[{index}]", depth=depth + 1)
        return
    raise CacheSerializationError(f"{label} contains unsupported {type(value).__name__} at {path}")


def canonical_json(value: Any, *, label: str = "value") -> str:
    """Return deterministic UTF-8 JSON used by keys and integrity checks."""

    _validate_json_value(value, label=label)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise CacheSerializationError(f"{label} cannot be encoded as canonical JSON") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_nonempty(value: str, *, label: str, maximum: int) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{label} must not exceed {maximum} characters")
    return normalized


def fingerprint_bytes(data: bytes | bytearray | memoryview) -> str:
    """Create a complete content fingerprint for an in-memory source."""

    view = memoryview(data)
    digest = hashlib.sha256()
    digest.update(f"clipfinder-bytes-v{SOURCE_FINGERPRINT_VERSION}\0".encode("ascii"))
    digest.update(len(view).to_bytes(8, "big", signed=False))
    digest.update(view)
    return f"bytes-v{SOURCE_FINGERPRINT_VERSION}:sha256:{digest.hexdigest()}"


def fingerprint_source_file(
    source: str | os.PathLike[str],
    *,
    sample_bytes: int = DEFAULT_SAMPLE_BYTES,
) -> str:
    """Fingerprint a large source without reading the entire recording.

    Size, nanosecond mtime and samples from the beginning, middle and end are
    included.  The source is stat'ed again after reading and a changing file is
    rejected, preventing a cache key from being created for a partial upload.
    """

    if sample_bytes < 4096:
        raise ValueError("sample_bytes must be at least 4096")
    path = Path(source).expanduser().resolve(strict=True)
    before = path.stat()
    if not stat_module.S_ISREG(before.st_mode):
        raise ValueError(f"Source is not a regular file: {path}")

    size = int(before.st_size)
    maximum_offset = max(0, size - sample_bytes)
    offsets = sorted({0, max(0, (size - sample_bytes) // 2), maximum_offset})
    digest = hashlib.sha256()
    digest.update(f"clipfinder-sampled-file-v{SOURCE_FINGERPRINT_VERSION}\0".encode("ascii"))
    digest.update(size.to_bytes(8, "big", signed=False))
    digest.update(int(before.st_mtime_ns).to_bytes(8, "big", signed=False))
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            chunk = handle.read(sample_bytes)
            digest.update(offset.to_bytes(8, "big", signed=False))
            digest.update(len(chunk).to_bytes(8, "big", signed=False))
            digest.update(chunk)

    after = path.stat()
    identity_before = (
        int(before.st_dev), int(before.st_ino), int(before.st_size), int(before.st_mtime_ns),
    )
    identity_after = (
        int(after.st_dev), int(after.st_ino), int(after.st_size), int(after.st_mtime_ns),
    )
    if identity_after != identity_before:
        raise PipelineCacheError(f"Source changed while it was fingerprinted: {path}")
    return f"sampled-file-v{SOURCE_FINGERPRINT_VERSION}:sha256:{digest.hexdigest()}"


def make_cache_key(source_fingerprint: str, stage: str, parameters: Mapping[str, Any]) -> str:
    """Build a key solely from source identity, stage and stage parameters."""

    fingerprint = _normalized_nonempty(source_fingerprint, label="source_fingerprint", maximum=2048)
    stage_name = _normalized_nonempty(stage, label="stage", maximum=160)
    if not isinstance(parameters, Mapping):
        raise CacheSerializationError("parameters must be a JSON object")
    parameters_json = canonical_json(parameters, label="parameters")
    material = canonical_json(
        {
            "source_fingerprint": fingerprint,
            "stage": stage_name,
            "parameters": json.loads(parameters_json),
        },
        label="cache key material",
    )
    return _sha256_text(material)


def _stage_directory_name(stage: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", stage).strip(".-")[:48] or "stage"
    return f"{slug}-{_sha256_text(stage)[:16]}"


def _same_observation(path: Path, observed: _ObservedFile) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        not stat_module.S_ISLNK(current.st_mode)
        and stat_module.S_ISREG(current.st_mode)
        and int(current.st_size) == observed.size
        and int(current.st_mtime_ns) == observed.modified_ns
        and int(current.st_dev) == observed.device
        and int(current.st_ino) == observed.inode
    )


class PipelineCache:
    """Safe cache of JSON-compatible pipeline stage results."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        clock: Any = time.time,
    ) -> None:
        if max_entry_bytes < 1024:
            raise ValueError("max_entry_bytes must be at least 1024")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.version_root = self.root / f"schema-v{CACHE_SCHEMA_VERSION}"
        self.max_entry_bytes = int(max_entry_bytes)
        self._clock = clock
        self.root.mkdir(parents=True, exist_ok=True)
        self.version_root.mkdir(parents=True, exist_ok=True)
        self._assert_directory(self.root)
        self._assert_directory(self.version_root)

    def _assert_within(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.version_root)
        except ValueError as exc:
            raise CacheSafetyError(f"Cache path escapes the versioned root: {path}") from exc
        return resolved

    def _assert_directory(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or resolved != path or not resolved.is_dir():
            raise CacheSafetyError(f"Cache directory is not a directory: {path}")
        if path != self.root:
            self._assert_within(path)

    @staticmethod
    def _video_namespace(video_id: str) -> str:
        return _sha256_text(_normalized_nonempty(video_id, label="video_id", maximum=1024))

    def _video_directory(self, video_id: str) -> Path:
        return self.version_root / f"video-{self._video_namespace(video_id)}"

    def _entry_path(
        self,
        video_id: str,
        source_fingerprint: str,
        stage: str,
        parameters: Mapping[str, Any],
    ) -> tuple[str, Path]:
        stage_name = _normalized_nonempty(stage, label="stage", maximum=160)
        key = make_cache_key(source_fingerprint, stage_name, parameters)
        path = self._video_directory(video_id) / _stage_directory_name(stage_name) / f"{key}.json"
        self._assert_within(path)
        return key, path

    def _ensure_entry_directory(self, path: Path) -> None:
        # Create and validate one hashed level at a time.  ``mkdir(parents=True)``
        # would be unsafe here because it could follow an unexpected video-dir
        # symlink before we get a chance to reject it.
        self._assert_directory(self.version_root)
        video_directory = path.parent.parent
        if not video_directory.exists():
            video_directory.mkdir()
        self._assert_directory(video_directory)
        if not path.parent.exists():
            path.parent.mkdir()
        self._assert_directory(path.parent)

    @staticmethod
    def _entry_digest(entry_without_digest: Mapping[str, Any]) -> str:
        return _sha256_text(canonical_json(entry_without_digest, label="cache entry"))

    def put(
        self,
        *,
        video_id: str,
        source_fingerprint: str,
        stage: str,
        parameters: Mapping[str, Any],
        value: Any,
    ) -> CacheWriteResult:
        """Atomically store one stage result."""

        normalized_video_id = _normalized_nonempty(video_id, label="video_id", maximum=1024)
        fingerprint = _normalized_nonempty(source_fingerprint, label="source_fingerprint", maximum=2048)
        stage_name = _normalized_nonempty(stage, label="stage", maximum=160)
        parameters_json = canonical_json(parameters, label="parameters")
        value_json = canonical_json(value, label="cache value")
        canonical_parameters = json.loads(parameters_json)
        canonical_value = json.loads(value_json)
        key, path = self._entry_path(
            normalized_video_id, fingerprint, stage_name, canonical_parameters,
        )
        now = datetime.fromtimestamp(float(self._clock()), tz=UTC).isoformat()
        body: dict[str, Any] = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "cache_key": key,
            "video_id": normalized_video_id,
            "video_namespace": self._video_namespace(normalized_video_id),
            "source_fingerprint": fingerprint,
            "stage": stage_name,
            "parameters": canonical_parameters,
            "parameters_sha256": _sha256_text(parameters_json),
            "created_at": now,
            "value": canonical_value,
            "value_sha256": _sha256_text(value_json),
        }
        entry = dict(body)
        entry["entry_sha256"] = self._entry_digest(body)
        encoded = canonical_json(entry, label="cache entry").encode("utf-8")
        if len(encoded) > self.max_entry_bytes:
            raise CacheSerializationError(
                f"Encoded cache entry is {len(encoded)} bytes; limit is {self.max_entry_bytes}"
            )

        self._ensure_entry_directory(path)
        replaced = path.exists()
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        self._assert_within(temporary)
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_directory(path.parent)
            if path.exists() and path.is_symlink():
                raise CacheSafetyError(f"Refusing to replace a cache symlink: {path}")
            os.replace(temporary, path)
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass
        return CacheWriteResult(key=key, path=path, size=len(encoded), replaced=replaced)

    def _observe_regular_file(self, path: Path) -> _ObservedFile:
        self._assert_within(path)
        file_stat = path.lstat()
        if stat_module.S_ISLNK(file_stat.st_mode) or not stat_module.S_ISREG(file_stat.st_mode):
            raise CacheSafetyError(f"Cache entry is not a regular file: {path}")
        if path.resolve(strict=True) != path:
            raise CacheSafetyError(f"Cache entry resolves to a different path: {path}")
        return _ObservedFile(
            path=path,
            size=int(file_stat.st_size),
            modified_ns=int(file_stat.st_mtime_ns),
            device=int(file_stat.st_dev),
            inode=int(file_stat.st_ino),
            valid_entry=False,
        )

    def _decode_and_validate(self, path: Path, observed: _ObservedFile) -> tuple[dict[str, Any] | None, str]:
        if observed.size > self.max_entry_bytes:
            return None, "entry-too-large"
        try:
            raw = path.read_bytes()
        except OSError:
            return None, "read-error"
        if len(raw) != observed.size or not _same_observation(path, observed):
            return None, "changed-during-read"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None, "invalid-json"
        if not isinstance(parsed, dict):
            return None, "invalid-entry"
        try:
            required = {
                "schema_version", "cache_key", "video_id", "video_namespace",
                "source_fingerprint", "stage", "parameters", "parameters_sha256",
                "created_at", "value", "value_sha256", "entry_sha256",
            }
            if not required.issubset(parsed):
                return None, "missing-fields"
            if type(parsed["schema_version"]) is not int or parsed["schema_version"] != CACHE_SCHEMA_VERSION:
                return None, "schema-mismatch"
            if not isinstance(parsed["video_id"], str):
                return None, "invalid-entry"
            if not isinstance(parsed["source_fingerprint"], str):
                return None, "invalid-entry"
            if not isinstance(parsed["stage"], str):
                return None, "invalid-entry"
            if not isinstance(parsed["parameters"], dict):
                return None, "invalid-entry"
            video_id = _normalized_nonempty(parsed["video_id"], label="video_id", maximum=1024)
            fingerprint = _normalized_nonempty(
                parsed["source_fingerprint"], label="source_fingerprint", maximum=2048,
            )
            stage = _normalized_nonempty(parsed["stage"], label="stage", maximum=160)
            if parsed["video_namespace"] != self._video_namespace(video_id):
                return None, "video-namespace-mismatch"
            parameters_json = canonical_json(parsed["parameters"], label="cached parameters")
            value_json = canonical_json(parsed["value"], label="cached value")
            if parsed["parameters_sha256"] != _sha256_text(parameters_json):
                return None, "parameters-checksum-mismatch"
            if parsed["value_sha256"] != _sha256_text(value_json):
                return None, "value-checksum-mismatch"
            expected_key = make_cache_key(fingerprint, stage, parsed["parameters"])
            if parsed["cache_key"] != expected_key:
                return None, "key-mismatch"
            created_at = datetime.fromisoformat(str(parsed["created_at"]).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                return None, "invalid-entry"
            body = dict(parsed)
            entry_digest = body.pop("entry_sha256")
            if entry_digest != self._entry_digest(body):
                return None, "entry-checksum-mismatch"
            expected_path = self._entry_path(video_id, fingerprint, stage, parsed["parameters"])[1]
            if expected_path != path:
                return None, "path-mismatch"
        except (CacheSerializationError, TypeError, ValueError, OverflowError):
            return None, "invalid-entry"
        return parsed, "hit"

    def _discard_if_unchanged(self, observed: _ObservedFile) -> bool:
        if not _same_observation(observed.path, observed):
            return False
        try:
            observed.path.unlink()
        except OSError:
            return False
        return True

    def get(
        self,
        *,
        video_id: str,
        source_fingerprint: str,
        stage: str,
        parameters: Mapping[str, Any],
        discard_corrupt: bool = True,
    ) -> CacheLookup:
        """Read and validate an entry.  Corruption is a recoverable miss."""

        key, path = self._entry_path(video_id, source_fingerprint, stage, parameters)
        try:
            observed = self._observe_regular_file(path)
        except FileNotFoundError:
            return CacheLookup(key=key, path=path, hit=False, reason="missing")
        parsed, reason = self._decode_and_validate(path, observed)
        if parsed is None:
            if discard_corrupt and reason not in {"read-error", "changed-during-read"}:
                self._discard_if_unchanged(observed)
            return CacheLookup(key=key, path=path, hit=False, reason=reason)

        if (
            parsed["video_id"] != str(video_id).strip()
            or parsed["source_fingerprint"] != str(source_fingerprint).strip()
            or parsed["stage"] != str(stage).strip()
            or canonical_json(parsed["parameters"], label="cached parameters")
            != canonical_json(parameters, label="parameters")
        ):
            if discard_corrupt:
                self._discard_if_unchanged(observed)
            return CacheLookup(key=key, path=path, hit=False, reason="identity-mismatch")
        return CacheLookup(key=key, path=path, hit=True, value=parsed["value"], reason="hit")

    def _managed_files(self, base: Path) -> tuple[Path, ...]:
        if not base.exists():
            return ()
        self._assert_within(base)
        if base.is_symlink() or not base.is_dir() or base.resolve(strict=True) != base:
            raise CacheSafetyError(f"Unsafe cache directory: {base}")
        found: list[Path] = []
        for current, directories, files in os.walk(base, topdown=True, followlinks=False):
            current_path = Path(current)
            self._assert_within(current_path)
            safe_directories: list[str] = []
            for name in directories:
                child = current_path / name
                try:
                    if child.is_symlink() or not child.is_dir():
                        continue
                    self._assert_within(child)
                except (OSError, CacheSafetyError):
                    continue
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                if not (name.endswith(".json") or name.endswith(".tmp")):
                    continue
                child = current_path / name
                try:
                    self._assert_within(child)
                    child_stat = child.lstat()
                    if stat_module.S_ISLNK(child_stat.st_mode) or not stat_module.S_ISREG(child_stat.st_mode):
                        continue
                    if child.resolve(strict=True) != child:
                        continue
                except (FileNotFoundError, OSError, CacheSafetyError):
                    continue
                found.append(child)
        return tuple(sorted(found, key=lambda item: str(item).casefold()))

    def _remove_empty_directories(self, base: Path) -> None:
        if not base.exists() or base.is_symlink():
            return
        directories = [path for path in base.rglob("*") if path.is_dir() and not path.is_symlink()]
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            try:
                self._assert_within(directory)
                directory.rmdir()
            except (OSError, CacheSafetyError):
                pass
        try:
            if base != self.version_root:
                base.rmdir()
        except OSError:
            pass

    def invalidate_video(self, video_id: str, *, dry_run: bool = False) -> CacheInvalidationResult:
        """Remove only entries below the hashed namespace of one video."""

        normalized_video_id = _normalized_nonempty(video_id, label="video_id", maximum=1024)
        directory = self._video_directory(normalized_video_id)
        files = self._managed_files(directory)
        if dry_run:
            return CacheInvalidationResult(normalized_video_id, files, (), (), (), 0, True)

        removed: list[Path] = []
        skipped: list[Path] = []
        errors: list[tuple[Path, str]] = []
        reclaimed = 0
        for path in files:
            try:
                observed = self._observe_regular_file(path)
            except (FileNotFoundError, CacheSafetyError) as exc:
                skipped.append(path)
                if isinstance(exc, CacheSafetyError):
                    errors.append((path, str(exc)))
                continue
            try:
                if not _same_observation(path, observed):
                    skipped.append(path)
                    continue
                path.unlink()
            except FileNotFoundError:
                skipped.append(path)
            except OSError as exc:
                errors.append((path, str(exc)))
            else:
                removed.append(path)
                reclaimed += observed.size
        self._remove_empty_directories(directory)
        return CacheInvalidationResult(
            normalized_video_id, files, tuple(removed), tuple(skipped), tuple(errors), reclaimed, False,
        )

    def cleanup(
        self,
        *,
        older_than_seconds: float | None = None,
        max_total_bytes: int | None = None,
        temporary_older_than_seconds: float = 3600.0,
        remove_corrupt: bool = True,
        dry_run: bool = True,
        now: float | None = None,
    ) -> CacheCleanupResult:
        """Plan or execute bounded cleanup; previewing is the default."""

        if older_than_seconds is not None and older_than_seconds < 0:
            raise ValueError("older_than_seconds must not be negative")
        if max_total_bytes is not None and max_total_bytes < 0:
            raise ValueError("max_total_bytes must not be negative")
        if temporary_older_than_seconds < 0:
            raise ValueError("temporary_older_than_seconds must not be negative")

        timestamp = float(self._clock() if now is None else now)
        files = self._managed_files(self.version_root)
        observed_files: list[_ObservedFile] = []
        candidates: dict[Path, _ObservedFile] = {}
        valid_entries: list[_ObservedFile] = []
        for path in files:
            try:
                observed = self._observe_regular_file(path)
            except (FileNotFoundError, CacheSafetyError):
                continue
            is_temporary = path.name.endswith(".tmp")
            if is_temporary:
                observed_files.append(observed)
                if observed.modified_ns <= int((timestamp - temporary_older_than_seconds) * 1e9):
                    candidates[path] = observed
                continue
            parsed, _reason = self._decode_and_validate(path, observed)
            observed = _ObservedFile(
                path=observed.path,
                size=observed.size,
                modified_ns=observed.modified_ns,
                device=observed.device,
                inode=observed.inode,
                valid_entry=parsed is not None,
            )
            observed_files.append(observed)
            if parsed is None and remove_corrupt:
                candidates[path] = observed
            elif parsed is not None:
                valid_entries.append(observed)
                if (
                    older_than_seconds is not None
                    and observed.modified_ns <= int((timestamp - older_than_seconds) * 1e9)
                ):
                    candidates[path] = observed

        if max_total_bytes is not None:
            remaining = sum(entry.size for entry in valid_entries if entry.path not in candidates)
            for entry in sorted(valid_entries, key=lambda item: (item.modified_ns, str(item.path).casefold())):
                if remaining <= max_total_bytes:
                    break
                if entry.path in candidates:
                    continue
                candidates[entry.path] = entry
                remaining -= entry.size

        planned_records = sorted(candidates.values(), key=lambda item: str(item.path).casefold())
        planned = tuple(item.path for item in planned_records)
        if dry_run:
            return CacheCleanupResult(len(observed_files), planned, (), (), (), 0, True)

        removed: list[Path] = []
        skipped: list[Path] = []
        errors: list[tuple[Path, str]] = []
        reclaimed = 0
        for observed in planned_records:
            if not _same_observation(observed.path, observed):
                skipped.append(observed.path)
                continue
            try:
                observed.path.unlink()
            except FileNotFoundError:
                skipped.append(observed.path)
            except OSError as exc:
                errors.append((observed.path, str(exc)))
            else:
                removed.append(observed.path)
                reclaimed += observed.size
        self._remove_empty_directories(self.version_root)
        return CacheCleanupResult(
            len(observed_files), planned, tuple(removed), tuple(skipped), tuple(errors), reclaimed, False,
        )
