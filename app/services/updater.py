"""Download and hand off desktop updates without requiring manual installer steps."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.config import settings
from app.services.updates import update_status


_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _updates_directory() -> Path:
    directory = settings.clipfinder_data_dir.parent / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _helper_path() -> Path:
    return Path(sys.executable).with_name("ClipFinderUpdateHelper.exe")


def automatic_updates_available() -> bool:
    return bool(getattr(sys, "frozen", False) and _helper_path().is_file())


def _set_job(job_id: str, **values) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(values)


def job_status(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _is_safe_release_asset(url: str) -> bool:
    parsed = urlparse(url)
    repository = settings.update_repository.strip().strip("/")
    expected_prefix = f"/{repository}/releases/download/"
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(expected_prefix)
        and parsed.path.lower().endswith(".exe")
        and "clipfinder-setup-" in parsed.path.lower()
    )


def _download(job_id: str, url: str, version: str) -> None:
    target = _updates_directory() / f"ClipFinder-Setup-{version}.exe"
    partial = target.with_suffix(".exe.part")
    try:
        request = Request(url, headers={"User-Agent": "ClipFinder-Local updater"})
        with urlopen(request, timeout=30) as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                progress = min(99, int(received * 100 / total)) if total else 0
                _set_job(job_id, progress=progress, downloaded_bytes=received, total_bytes=total, message="Downloading update")
        os.replace(partial, target)
        _set_job(job_id, state="completed", progress=100, downloaded_bytes=target.stat().st_size, total_bytes=target.stat().st_size, installer_path=str(target), message="Update ready to install")
    except Exception as exc:
        partial.unlink(missing_ok=True)
        _set_job(job_id, state="failed", message=str(exc))


def start_download() -> dict:
    update = update_status()
    if update.get("error"):
        raise RuntimeError(update["error"])
    if not update.get("update_available") or not _is_safe_release_asset(str(update.get("download_url") or "")):
        raise RuntimeError("No safe ClipFinder update is available.")
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "state": "downloading",
        "progress": 0,
        "downloaded_bytes": 0,
        "total_bytes": int(update.get("asset_size") or 0),
        "message": "Preparing update download",
        "version": update["latest_version"],
    }
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_download, args=(job_id, update["download_url"], update["latest_version"]), daemon=True, name="ClipFinder update download").start()
    return dict(job)


def install_downloaded_update(job_id: str) -> None:
    job = job_status(job_id)
    if not job or job.get("state") != "completed":
        raise RuntimeError("The update has not finished downloading.")
    if not automatic_updates_available():
        raise RuntimeError("Automatic installation is only available in the installed desktop app.")
    installer = Path(str(job.get("installer_path") or ""))
    if not installer.is_file():
        raise RuntimeError("The downloaded installer is no longer available.")
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    runtime_helper = _updates_directory() / "ClipFinderUpdateHelper.exe"
    shutil.copy2(_helper_path(), runtime_helper)
    subprocess.Popen([
        str(runtime_helper), "--parent-pid", str(os.getpid()), "--installer", str(installer), "--restart-exe", sys.executable,
    ], close_fds=True, creationflags=flags)
    _set_job(job_id, state="installing", message="Closing ClipFinder and installing the update")
