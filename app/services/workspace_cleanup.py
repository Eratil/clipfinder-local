"""Conservative cleanup and per-job filesystem leases.

The helpers in this module deliberately know nothing about ClipFinder's data
layout.  A caller must provide every directory which may be inspected.  This
keeps cleanup opt-in and makes it impossible for a broad, implicit workspace
path to become a deletion target.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_TEMP_SUFFIXES = (".part", ".download", ".tmp", ".temp")


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    """Return whether *path* is *root* or one of its descendants."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_active(path: Path, active_paths: tuple[Path, ...]) -> bool:
    # Treat an active directory as protecting all descendants.  For a file,
    # this naturally protects only that exact path because no candidate can be
    # below a regular file.
    return any(_is_within(path, active) for active in active_paths)


def _has_temp_suffix(path: Path, suffixes: tuple[str, ...]) -> bool:
    name = path.name.casefold()
    return any(name.endswith(suffix) for suffix in suffixes)


@dataclass(frozen=True)
class CleanupEntry:
    """A file observed while producing a cleanup plan."""

    path: Path
    root: Path
    modified_at: float
    size: int
    device: int
    inode: int


@dataclass(frozen=True)
class CleanupPlan:
    """An immutable dry-run result which can later be executed safely."""

    roots: tuple[Path, ...]
    active_paths: tuple[Path, ...]
    suffixes: tuple[str, ...]
    cutoff_at: float
    entries: tuple[CleanupEntry, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(entry.path for entry in self.entries)


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of executing (or previewing) a cleanup plan."""

    planned: tuple[Path, ...]
    deleted: tuple[Path, ...]
    skipped: tuple[Path, ...]
    errors: tuple[tuple[Path, str], ...]
    dry_run: bool


def plan_workspace_cleanup(
    roots: Iterable[str | os.PathLike[str]],
    *,
    older_than_seconds: float,
    active_paths: Iterable[str | os.PathLike[str]] = (),
    now: float | None = None,
    suffixes: Iterable[str] = DEFAULT_TEMP_SUFFIXES,
) -> CleanupPlan:
    """Find old temporary files below explicitly supplied directories.

    Symlinks are never candidates.  Both roots and candidates are resolved and
    checked with :meth:`Path.relative_to`, preventing a symlink or ``..`` path
    from escaping the permitted directory.
    """
    if older_than_seconds < 0:
        raise ValueError("older_than_seconds must not be negative")

    resolved_roots = tuple(dict.fromkeys(_resolved(root) for root in roots))
    if not resolved_roots:
        raise ValueError("At least one cleanup root must be provided")
    resolved_active = tuple(dict.fromkeys(_resolved(path) for path in active_paths))
    normalized_suffixes = tuple(
        dict.fromkeys(
            suffix.casefold() if str(suffix).startswith(".") else f".{str(suffix).casefold()}"
            for suffix in suffixes
            if str(suffix).strip()
        )
    )
    if not normalized_suffixes:
        raise ValueError("At least one temporary-file suffix must be provided")

    timestamp = time.time() if now is None else float(now)
    cutoff_at = timestamp - float(older_than_seconds)
    entries: list[CleanupEntry] = []
    seen: set[Path] = set()

    for root in resolved_roots:
        if not root.exists() or not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            try:
                # A link may resolve outside the root, so reject it before any
                # target metadata is inspected.
                if candidate.is_symlink():
                    continue
                resolved_candidate = candidate.resolve(strict=True)
                if resolved_candidate in seen or not _is_within(resolved_candidate, root):
                    continue
                if not resolved_candidate.is_file():
                    continue
                if not _has_temp_suffix(resolved_candidate, normalized_suffixes):
                    continue
                if _is_active(resolved_candidate, resolved_active):
                    continue
                stat = resolved_candidate.stat()
                if stat.st_mtime > cutoff_at:
                    continue
            except (FileNotFoundError, OSError):
                # Files may disappear while a plan is being assembled.
                continue
            seen.add(resolved_candidate)
            entries.append(
                CleanupEntry(
                    path=resolved_candidate,
                    root=root,
                    modified_at=float(stat.st_mtime),
                    size=int(stat.st_size),
                    device=int(stat.st_dev),
                    inode=int(stat.st_ino),
                )
            )

    entries.sort(key=lambda entry: str(entry.path).casefold())
    return CleanupPlan(
        roots=resolved_roots,
        active_paths=resolved_active,
        suffixes=normalized_suffixes,
        cutoff_at=cutoff_at,
        entries=tuple(entries),
    )


def _entry_still_matches_plan(entry: CleanupEntry, plan: CleanupPlan) -> bool:
    path = entry.path.resolve(strict=False)
    root = entry.root.resolve(strict=False)
    # Real plans only store canonical paths.  Reject hand-crafted entries whose
    # lexical path points elsewhere (for example an outside symlink resolving
    # to a file inside the root), because unlink acts on the lexical path.
    if entry.path != path or entry.root != root:
        return False
    if root not in plan.roots or not _is_within(path, root):
        return False
    if _is_active(path, plan.active_paths) or not _has_temp_suffix(path, plan.suffixes):
        return False
    try:
        if path.is_symlink() or not path.is_file():
            return False
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return False
    # Do not delete a file which was replaced, touched, or changed after the
    # preview.  A future plan can reconsider it after it becomes stale again.
    return (
        float(stat.st_mtime) == entry.modified_at
        and int(stat.st_size) == entry.size
        and int(stat.st_dev) == entry.device
        and int(stat.st_ino) == entry.inode
        and float(stat.st_mtime) <= plan.cutoff_at
    )


def execute_workspace_cleanup(plan: CleanupPlan, *, dry_run: bool = False) -> CleanupResult:
    """Execute a previously created plan after revalidating every candidate."""
    planned = plan.paths
    if dry_run:
        return CleanupResult(planned, (), (), (), True)

    deleted: list[Path] = []
    skipped: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for entry in plan.entries:
        if not _entry_still_matches_plan(entry, plan):
            skipped.append(entry.path)
            continue
        try:
            entry.path.unlink()
        except FileNotFoundError:
            skipped.append(entry.path)
        except OSError as exc:
            errors.append((entry.path, str(exc)))
        else:
            deleted.append(entry.path)
    return CleanupResult(planned, tuple(deleted), tuple(skipped), tuple(errors), False)


def cleanup_workspace(
    roots: Iterable[str | os.PathLike[str]],
    *,
    older_than_seconds: float,
    active_paths: Iterable[str | os.PathLike[str]] = (),
    now: float | None = None,
    suffixes: Iterable[str] = DEFAULT_TEMP_SUFFIXES,
    dry_run: bool = True,
) -> CleanupResult:
    """Convenience wrapper; previewing is the safe default."""
    plan = plan_workspace_cleanup(
        roots,
        older_than_seconds=older_than_seconds,
        active_paths=active_paths,
        now=now,
        suffixes=suffixes,
    )
    return execute_workspace_cleanup(plan, dry_run=dry_run)


class LeaseAlreadyHeld(RuntimeError):
    """Raised when another non-stale owner holds a scoped lease."""

    def __init__(self, path: Path, owner: str | None = None):
        detail = f" by {owner}" if owner else ""
        super().__init__(f"Lease is already held{detail}: {path}")
        self.path = path
        self.owner = owner


def _safe_scope_name(scope: str) -> str:
    normalized = str(scope).strip()
    if not normalized:
        raise ValueError("scope must not be empty")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized).strip(".-")[:48] or "job"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}.lock"


class ScopedFileLease:
    """A process-safe, owner-aware lease for one video or background job.

    The descriptor is locked by the operating system for its whole lifetime.
    A crashed process therefore releases ownership automatically.  The small
    JSON file is intentionally retained after release: deleting a path after
    unlocking creates a TOCTOU race in which an old owner could delete a new
    owner's lock file.
    """

    def __init__(
        self,
        lock_directory: str | os.PathLike[str],
        scope: str,
        *,
        owner: str,
        stale_after_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not str(owner).strip():
            raise ValueError("owner must not be empty")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than zero")
        self.lock_directory = _resolved(lock_directory)
        self.scope = str(scope)
        self.owner = str(owner)
        self.stale_after_seconds = float(stale_after_seconds)
        self._clock = clock
        self._token = uuid.uuid4().hex
        self._acquired = False
        self._descriptor: int | None = None
        self.path = self.lock_directory / _safe_scope_name(self.scope)
        self.metadata_path = self.path.with_suffix(".json")
        if not _is_within(self.path.resolve(strict=False), self.lock_directory):
            raise ValueError("Resolved lock path escapes lock directory")
        if not _is_within(self.metadata_path.resolve(strict=False), self.lock_directory):
            raise ValueError("Resolved metadata path escapes lock directory")

    @property
    def acquired(self) -> bool:
        return self._acquired

    def _metadata(self, now: float) -> dict[str, object]:
        return {
            "scope": self.scope,
            "owner": self.owner,
            "token": self._token,
            "timestamp": float(now),
        }

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _write_metadata(self, now: float) -> None:
        if self._descriptor is None:
            raise OSError("Lease descriptor is closed")
        self.metadata_path.write_text(
            json.dumps(self._metadata(now), ensure_ascii=False), encoding="utf-8",
        )

    def acquire(self) -> "ScopedFileLease":
        if self._acquired:
            return self
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating the directory and reject a link swapped in
        # by another process or an unexpected filesystem layout.
        actual_directory = self.lock_directory.resolve(strict=True)
        if actual_directory != self.lock_directory:
            raise ValueError("Lock directory changed while acquiring lease")

        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(descriptor).st_size < 1:
                os.write(descriptor, b" ")
                os.fsync(descriptor)
            self._lock_descriptor(descriptor)
        except OSError as exc:
            os.close(descriptor)
            try:
                parsed = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                metadata = parsed if isinstance(parsed, dict) else None
            except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
                metadata = None
            owner = str(metadata.get("owner")) if metadata and metadata.get("owner") else None
            raise LeaseAlreadyHeld(self.path, owner) from exc
        self._descriptor = descriptor
        self._acquired = True
        try:
            self._write_metadata(float(self._clock()))
        except BaseException:
            self.release()
            raise
        return self

    def release(self) -> bool:
        """Release the kernel lock while retaining harmless metadata."""
        if not self._acquired or self._descriptor is None:
            return False
        try:
            self._unlock_descriptor(self._descriptor)
        finally:
            os.close(self._descriptor)
            self._descriptor = None
            self._acquired = False
        return True

    def refresh(self) -> bool:
        """Refresh the timestamp while this instance still owns the lease.

        Long analyses may outlive ``stale_after_seconds``.  The timestamp is
        diagnostic only; ownership remains tied to the locked descriptor.
        """
        if not self._acquired or self._descriptor is None:
            return False
        self._write_metadata(float(self._clock()))
        return True

    def __enter__(self) -> "ScopedFileLease":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
