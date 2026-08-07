from __future__ import annotations

import threading
import time
from pathlib import Path

from app.services.background_worker import DurableBackgroundWorker, QueueAdapter


class FakeQueue:
    def __init__(self, jobs: list[str], *, silent_seconds: float = 0.0) -> None:
        self.pending = list(jobs)
        self.running: dict[str, dict] = {}
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.heartbeat_count = 0
        self.active_dispatches = 0
        self.max_dispatches = 0
        self.silent_seconds = silent_seconds
        self.lock = threading.Lock()
        self.done = threading.Event()

    def claim(self, worker_id: str, _lease_seconds: float):
        with self.lock:
            if not self.pending:
                return None
            job_id = self.pending.pop(0)
            item = {"id": job_id, "lease_token": f"{worker_id}:{job_id}", "cancel_requested": 0}
            self.running[job_id] = item
            return dict(item)

    def heartbeat(self, job_id: str, _token: str, _lease_seconds: float):
        with self.lock:
            self.heartbeat_count += 1
            item = self.running.get(job_id)
            return dict(item) if item else None

    def progress(self, job_id: str, token: str, _progress: int, _message: str, lease_seconds: float):
        return self.heartbeat(job_id, token, lease_seconds)

    def complete(self, job_id: str, _token: str):
        with self.lock:
            item = self.running.pop(job_id, None)
            if item:
                self.completed.append(job_id)
            if not self.pending and not self.running:
                self.done.set()
            return item

    def fail(self, job_id: str, _token: str, error: str, _retryable: bool):
        with self.lock:
            self.running.pop(job_id, None)
            self.failed.append(f"{job_id}:{error}")
            if not self.pending and not self.running:
                self.done.set()
        return None

    def dispatch(self, _job: dict, report, _should_cancel) -> None:
        with self.lock:
            self.active_dispatches += 1
            self.max_dispatches = max(self.max_dispatches, self.active_dispatches)
        try:
            if self.silent_seconds:
                time.sleep(self.silent_seconds)
            report(50, "halfway")
        finally:
            with self.lock:
                self.active_dispatches -= 1

    def adapter(self) -> QueueAdapter:
        return QueueAdapter(
            name="fake",
            claim=self.claim,
            heartbeat=self.heartbeat,
            update_progress=self.progress,
            complete=self.complete,
            fail=self.fail,
            dispatch=self.dispatch,
            recover=lambda: None,
        )


class FlakyQueue(FakeQueue):
    def __init__(
        self,
        jobs: list[str],
        *,
        claim_failures: int = 0,
        heartbeat_failures: int = 0,
        complete_failures: int = 0,
        fail_failures: int = 0,
        dispatch_failures: set[str] | None = None,
        silent_seconds: float = 0.0,
    ) -> None:
        super().__init__(jobs, silent_seconds=silent_seconds)
        self.claim_failures = claim_failures
        self.heartbeat_failures = heartbeat_failures
        self.complete_failures = complete_failures
        self.fail_failures = fail_failures
        self.dispatch_failures = dispatch_failures or set()
        self.claim_attempts = 0

    def claim(self, worker_id: str, lease_seconds: float):
        self.claim_attempts += 1
        if self.claim_failures:
            self.claim_failures -= 1
            raise RuntimeError("temporary claim failure")
        return super().claim(worker_id, lease_seconds)

    def heartbeat(self, job_id: str, token: str, lease_seconds: float):
        if self.heartbeat_failures:
            self.heartbeat_failures -= 1
            raise RuntimeError("temporary heartbeat failure")
        return super().heartbeat(job_id, token, lease_seconds)

    def complete(self, job_id: str, token: str):
        if self.complete_failures:
            self.complete_failures -= 1
            raise RuntimeError("temporary completion failure")
        return super().complete(job_id, token)

    def fail(self, job_id: str, token: str, error: str, retryable: bool):
        if self.fail_failures:
            self.fail_failures -= 1
            raise RuntimeError("temporary failure-state update failure")
        return super().fail(job_id, token, error, retryable)

    def dispatch(self, job: dict, report, should_cancel) -> None:
        if str(job["id"]) in self.dispatch_failures:
            raise RuntimeError("simulated dispatch failure")
        super().dispatch(job, report, should_cancel)


def _worker(
    queue: FakeQueue,
    lock_directory: Path,
    *,
    errors: list[tuple[str, str]] | None = None,
) -> DurableBackgroundWorker:
    error_log = errors if errors is not None else []
    return DurableBackgroundWorker(
        [queue.adapter()],
        lock_directory=lock_directory,
        poll_seconds=0.01,
        lease_seconds=0.15,
        heartbeat_seconds=0.02,
        process_lease_seconds=0.1,
        adapter_error_backoff_seconds=0.005,
        adapter_error_backoff_max_seconds=0.02,
        on_error=lambda context, exc: error_log.append((context, str(exc))),
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_worker_processes_jobs_sequentially(tmp_path: Path) -> None:
    queue = FakeQueue(["one", "two", "three"])
    worker = _worker(queue, tmp_path / "locks")
    worker.start()
    try:
        assert queue.done.wait(2)
    finally:
        assert worker.stop(1)

    assert queue.completed == ["one", "two", "three"]
    assert queue.failed == []
    assert queue.max_dispatches == 1


def test_heartbeat_runs_during_silent_native_stage(tmp_path: Path) -> None:
    queue = FakeQueue(["slow"], silent_seconds=0.12)
    worker = _worker(queue, tmp_path / "locks")
    worker.start()
    try:
        assert queue.done.wait(2)
    finally:
        assert worker.stop(1)

    # One call comes from progress; the rest prove the independent timer kept
    # renewing while dispatch emitted no progress at all.
    assert queue.heartbeat_count >= 4
    assert queue.completed == ["slow"]


def test_claim_exception_backs_off_and_worker_keeps_running(tmp_path: Path) -> None:
    queue = FlakyQueue(["after-claim-error"], claim_failures=3)
    errors: list[tuple[str, str]] = []
    started = time.monotonic()
    worker = _worker(queue, tmp_path / "locks", errors=errors)
    worker.start()
    try:
        assert queue.done.wait(2)
        assert worker.running
    finally:
        assert worker.stop(1)

    assert time.monotonic() - started >= 0.02
    assert queue.claim_attempts >= 4
    assert queue.completed == ["after-claim-error"]
    assert any(context == "Claiming work for fake queue" for context, _ in errors)


def test_heartbeat_exceptions_do_not_kill_silent_job(tmp_path: Path) -> None:
    queue = FlakyQueue(["slow"], heartbeat_failures=2, silent_seconds=0.12)
    errors: list[tuple[str, str]] = []
    worker = _worker(queue, tmp_path / "locks", errors=errors)
    worker.start()
    try:
        assert queue.done.wait(2)
        assert worker.running
    finally:
        assert worker.stop(1)

    assert queue.completed == ["slow"]
    assert any(context == "Refreshing job lease for fake queue" for context, _ in errors)


def test_exhausted_complete_retries_do_not_kill_worker(tmp_path: Path) -> None:
    queue = FlakyQueue(["one", "two"], complete_failures=3)
    errors: list[tuple[str, str]] = []
    worker = _worker(queue, tmp_path / "locks", errors=errors)
    worker.start()
    try:
        assert _wait_until(lambda: queue.completed == ["two"])
        assert worker.running
    finally:
        assert worker.stop(1)

    assert queue.completed == ["two"]
    assert "one" in queue.running
    assert any(context == "Completing job for fake queue" for context, _ in errors)
    assert any(context == "Completing fake job one" for context, _ in errors)


def test_exhausted_fail_retries_do_not_kill_worker(tmp_path: Path) -> None:
    queue = FlakyQueue(
        ["bad", "good"],
        fail_failures=3,
        dispatch_failures={"bad"},
    )
    errors: list[tuple[str, str]] = []
    worker = _worker(queue, tmp_path / "locks", errors=errors)
    worker.start()
    try:
        assert _wait_until(lambda: queue.completed == ["good"])
        assert worker.running
    finally:
        assert worker.stop(1)

    assert queue.failed == []
    assert "bad" in queue.running
    assert queue.completed == ["good"]
    assert any(
        context == "Recording retryable job failure for fake queue"
        for context, _ in errors
    )
    assert any(
        context == "Recording retryable failure for fake job bad"
        for context, _ in errors
    )


def test_faulty_error_reporter_cannot_kill_worker(tmp_path: Path) -> None:
    queue = FlakyQueue(["one"], claim_failures=1)
    worker = DurableBackgroundWorker(
        [queue.adapter()],
        lock_directory=tmp_path / "locks",
        poll_seconds=0.01,
        lease_seconds=0.15,
        heartbeat_seconds=0.02,
        process_lease_seconds=0.1,
        adapter_error_backoff_seconds=0.005,
        adapter_error_backoff_max_seconds=0.02,
        on_error=lambda _context, _exc: (_ for _ in ()).throw(RuntimeError("logger failed")),
    )
    worker.start()
    try:
        assert queue.done.wait(2)
        assert worker.running
    finally:
        assert worker.stop(1)

    assert queue.completed == ["one"]
