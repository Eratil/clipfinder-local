from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services.workspace_cleanup import (
    CleanupEntry,
    CleanupPlan,
    LeaseAlreadyHeld,
    ScopedFileLease,
    cleanup_workspace,
    execute_workspace_cleanup,
    plan_workspace_cleanup,
)


def _old(path: Path, *, now: float, age: float = 500.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("temporary", encoding="utf-8")
    os.utime(path, (now - age, now - age))
    return path


def test_cleanup_plan_is_scoped_old_temp_only_and_protects_active_paths(tmp_path: Path):
    now = 10_000.0
    root = tmp_path / "work"
    old_part = _old(root / "upload.mp4.part", now=now)
    old_download = _old(root / "nested" / "vod.download", now=now)
    active_temp = _old(root / "active.tmp", now=now)
    ordinary = _old(root / "keep.mp4", now=now)
    young_temp = _old(root / "young.temp", now=now, age=5)

    plan = plan_workspace_cleanup(
        [root], older_than_seconds=60, active_paths=[active_temp], now=now,
    )

    assert plan.paths == (old_download.resolve(), old_part.resolve())
    assert active_temp.exists() and ordinary.exists() and young_temp.exists()


def test_dry_run_does_not_remove_anything_then_execution_removes_plan(tmp_path: Path):
    now = 20_000.0
    root = tmp_path / "work"
    candidate = _old(root / "render.tmp", now=now)
    plan = plan_workspace_cleanup([root], older_than_seconds=60, now=now)

    preview = execute_workspace_cleanup(plan, dry_run=True)
    assert preview.dry_run is True
    assert preview.planned == (candidate.resolve(),)
    assert preview.deleted == ()
    assert candidate.exists()

    result = execute_workspace_cleanup(plan)
    assert result.deleted == (candidate.resolve(),)
    assert result.skipped == () and result.errors == ()
    assert not candidate.exists()


def test_cleanup_wrapper_defaults_to_dry_run(tmp_path: Path):
    now = 30_000.0
    candidate = _old(tmp_path / "cache.part", now=now)
    result = cleanup_workspace([tmp_path], older_than_seconds=60, now=now)
    assert result.dry_run is True
    assert candidate.exists()


def test_changed_file_is_skipped_during_execution(tmp_path: Path):
    now = 40_000.0
    candidate = _old(tmp_path / "job.download", now=now)
    plan = plan_workspace_cleanup([tmp_path], older_than_seconds=60, now=now)
    candidate.write_text("new active contents", encoding="utf-8")

    result = execute_workspace_cleanup(plan)
    assert result.deleted == ()
    assert result.skipped == (candidate.resolve(),)
    assert candidate.exists()


def test_forged_traversal_entry_cannot_delete_outside_root(tmp_path: Path):
    now = 50_000.0
    root = tmp_path / "allowed"
    root.mkdir()
    outside = _old(tmp_path / "outside.tmp", now=now)
    stat = outside.stat()
    forged = CleanupEntry(
        path=outside,
        root=root.resolve(),
        modified_at=stat.st_mtime,
        size=stat.st_size,
        device=stat.st_dev,
        inode=stat.st_ino,
    )
    plan = CleanupPlan(
        roots=(root.resolve(),), active_paths=(), suffixes=(".tmp",),
        cutoff_at=now - 60, entries=(forged,),
    )

    result = execute_workspace_cleanup(plan)
    assert result.skipped == (outside,)
    assert outside.exists()


def test_symlink_to_outside_is_never_planned(tmp_path: Path):
    now = 60_000.0
    root = tmp_path / "allowed"
    root.mkdir()
    outside = _old(tmp_path / "outside.part", now=now)
    link = root / "linked.part"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this Windows setup")

    plan = plan_workspace_cleanup([root], older_than_seconds=60, now=now)
    assert plan.entries == ()
    assert outside.exists()


def test_forged_outside_symlink_resolving_inside_root_is_not_unlinked(tmp_path: Path):
    now = 70_000.0
    root = tmp_path / "allowed"
    target = _old(root / "target.tmp", now=now)
    outside_link = tmp_path / "outside-link.tmp"
    try:
        outside_link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this Windows setup")
    stat = target.stat()
    forged = CleanupEntry(
        path=outside_link,
        root=root.resolve(),
        modified_at=stat.st_mtime,
        size=stat.st_size,
        device=stat.st_dev,
        inode=stat.st_ino,
    )
    plan = CleanupPlan(
        roots=(root.resolve(),), active_paths=(), suffixes=(".tmp",),
        cutoff_at=now - 60, entries=(forged,),
    )

    result = execute_workspace_cleanup(plan)
    assert result.skipped == (outside_link,)
    assert outside_link.exists() and target.exists()


def test_active_lease_cannot_be_taken_or_released_by_other_owner(tmp_path: Path):
    now = [100.0]
    first = ScopedFileLease(
        tmp_path, "video:abc", owner="worker-1", stale_after_seconds=60,
        clock=lambda: now[0],
    ).acquire()
    second = ScopedFileLease(
        tmp_path, "video:abc", owner="worker-2", stale_after_seconds=60,
        clock=lambda: now[0],
    )

    with pytest.raises(LeaseAlreadyHeld, match="worker-1"):
        second.acquire()
    metadata = json.loads(first.metadata_path.read_text(encoding="utf-8"))
    assert metadata["owner"] == "worker-1"
    assert second.release() is False
    assert first.path.exists()
    assert first.release() is True


def test_live_kernel_lease_cannot_be_stolen_even_if_metadata_is_old(tmp_path: Path):
    now = [100.0]
    first = ScopedFileLease(
        tmp_path, "job:analyse", owner="worker-1", stale_after_seconds=30,
        clock=lambda: now[0],
    ).acquire()
    old_token = json.loads(first.metadata_path.read_text(encoding="utf-8"))["token"]
    now[0] = 131.0
    second = ScopedFileLease(
        tmp_path, "job:analyse", owner="worker-2", stale_after_seconds=30,
        clock=lambda: now[0],
    )
    with pytest.raises(LeaseAlreadyHeld):
        second.acquire()

    assert first.release() is True
    second.acquire()
    metadata = json.loads(second.metadata_path.read_text(encoding="utf-8"))
    assert metadata["owner"] == "worker-2"
    assert metadata["token"] != old_token
    assert second.release() is True


def test_malicious_scope_still_creates_lock_inside_explicit_directory(tmp_path: Path):
    lock_root = tmp_path / "locks"
    lease = ScopedFileLease(
        lock_root, "../../outside/video", owner="worker", stale_after_seconds=30,
    ).acquire()
    try:
        lease.path.resolve().relative_to(lock_root.resolve())
        assert ".." not in lease.path.name
        assert not (tmp_path / "outside").exists()
    finally:
        lease.release()


def test_context_manager_releases_lease(tmp_path: Path):
    with ScopedFileLease(
        tmp_path, "video:context", owner="worker", stale_after_seconds=30,
    ) as lease:
        assert lease.path.exists()
        assert lease.acquired is True
    # Metadata is retained intentionally; ownership is the kernel lock, not
    # file existence. A new owner can immediately acquire the same scope.
    assert lease.path.exists()
    assert lease.acquired is False
    replacement = ScopedFileLease(
        tmp_path, "video:context", owner="next", stale_after_seconds=30,
    ).acquire()
    assert replacement.release() is True


def test_live_lease_can_refresh_its_staleness_timestamp(tmp_path: Path):
    now = [100.0]
    lease = ScopedFileLease(
        tmp_path, "worker:global", owner="worker-1", stale_after_seconds=30,
        clock=lambda: now[0],
    ).acquire()
    now[0] = 120.0

    assert lease.refresh() is True
    metadata = json.loads(lease.metadata_path.read_text(encoding="utf-8"))
    assert metadata["timestamp"] == 120.0
    now[0] = 140.0
    contender = ScopedFileLease(
        tmp_path, "worker:global", owner="worker-2", stale_after_seconds=30,
        clock=lambda: now[0],
    )
    with pytest.raises(LeaseAlreadyHeld):
        contender.acquire()
    assert lease.release() is True
