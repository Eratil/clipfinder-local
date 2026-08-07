from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import clipfinder_update_helper
from app.services import runtime_paths, updater, updates
from tools import build_update_package, verify_release


def test_release_preflight_reads_recursive_pins_and_reports_drift(tmp_path: Path):
    (tmp_path / "requirements-dev.txt").write_text(
        "-r requirements.txt\npyinstaller==6.21.0\nctranslate2==4.8.1\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("-c constraints.txt\nfastapi==1.0.0\n", encoding="utf-8")
    (tmp_path / "constraints.txt").write_text("pydantic==2.0.0\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "version.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    for relative in verify_release.REQUIRED_SOURCE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (tmp_path / "installer" / "runtime-compatibility.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "contract_id": "test-contract",
                "architecture": "x64",
                "ctranslate2": "4.8.1",
                "cuda": {"required_dlls": ["cublas64_12.dll"]},
                "cudnn": {"required_dlls": ["cudnn64_9.dll"]},
                "models": {
                    name: {"id": name, "revision": "a" * 40}
                    for name in ("transcription_default", "transcription_fast", "similarity")
                },
            }
        ),
        encoding="utf-8",
    )
    versions = {
        "fastapi": "1.0.0",
        "pydantic": "2.1.0",
        "pyinstaller": "6.21.0",
        "ctranslate2": "4.8.1",
        "opencv-python": None,
        "torchaudio": None,
        "torchvision": None,
        "easyocr": None,
    }

    problems = verify_release.preflight_problems(
        tmp_path, "1.2.3", versions=versions, python_version=(3, 11), pointer_bits=64,
    )

    assert problems == ["pydantic must be 2.0.0, got 2.1.0."]


def test_release_preflight_rejects_unpinned_requirement(tmp_path: Path):
    (tmp_path / "requirements-dev.txt").write_text("fastapi>=1\n", encoding="utf-8")

    problems = verify_release.preflight_problems(
        tmp_path, "1.2.3", versions={}, python_version=(3, 11), pointer_bits=64,
    )

    assert problems == ["Requirement is not pinned exactly in requirements-dev.txt: fastapi>=1"]


def test_release_cache_embeds_version_and_patch_round_trip(tmp_path: Path):
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    previous.mkdir()
    current.mkdir()
    (previous / "keep.txt").write_text("old", encoding="utf-8")
    (previous / "remove.txt").write_text("remove", encoding="utf-8")
    (current / "keep.txt").write_text("new", encoding="utf-8")
    (current / "add.txt").write_text("add", encoding="utf-8")
    cache = tmp_path / "cache.zip"
    build_update_package.cache_release(previous, "1.0.0", cache)
    assert build_update_package.cache_version(cache) == "1.0.0"
    patch, _manifest = build_update_package.build_patch(cache, "1.0.0", current, "1.0.1", tmp_path)

    installed = tmp_path / "installed"
    installed.mkdir()
    for item in previous.iterdir():
        (installed / item.name).write_bytes(item.read_bytes())
    result = clipfinder_update_helper._apply_patch(patch, installed, "1.0.0", tmp_path / "logs")

    assert result == "1.0.1"
    assert (installed / "keep.txt").read_text(encoding="utf-8") == "new"
    assert (installed / "add.txt").read_text(encoding="utf-8") == "add"
    assert not (installed / "remove.txt").exists()


def test_patch_rejects_unexpected_zip_entry_before_modifying_installation(tmp_path: Path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "app.txt").write_text("before", encoding="utf-8")
    (new / "app.txt").write_text("after", encoding="utf-8")
    cache = build_update_package.cache_release(old, "1.0.0", tmp_path / "cache.zip")
    patch, _manifest = build_update_package.build_patch(cache, "1.0.0", new, "1.0.1", tmp_path)
    with zipfile.ZipFile(patch, "a") as archive:
        archive.writestr("unexpected.bin", b"not allowed")
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "app.txt").write_text("before", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unexpected"):
        clipfinder_update_helper._apply_patch(patch, installed, "1.0.0", tmp_path / "logs")

    assert (installed / "app.txt").read_text(encoding="utf-8") == "before"


class _SimulatedPowerLoss(BaseException):
    pass


def test_interrupted_patch_is_recovered_from_durable_journal_on_next_run(tmp_path: Path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    installed = tmp_path / "installed"
    for directory in (old, new, installed):
        directory.mkdir()
    (old / "01-first.txt").write_text("first-before", encoding="utf-8")
    (old / "02-second.txt").write_text("second-before", encoding="utf-8")
    (old / "03-remove.txt").write_text("remove-me", encoding="utf-8")
    (new / "01-first.txt").write_text("first-after", encoding="utf-8")
    (new / "02-second.txt").write_text("second-after", encoding="utf-8")
    for source in old.iterdir():
        (installed / source.name).write_bytes(source.read_bytes())
    cache = build_update_package.cache_release(old, "1.0.0", tmp_path / "cache.zip")
    patch, _manifest = build_update_package.build_patch(cache, "1.0.0", new, "1.0.1", tmp_path)
    work = tmp_path / "update-work"
    real_replace = clipfinder_update_helper.os.replace

    def interrupt_second_install_write(source, destination):
        if Path(destination) == installed / "02-second.txt":
            raise _SimulatedPowerLoss()
        return real_replace(source, destination)

    monkeypatch.setattr(clipfinder_update_helper.os, "replace", interrupt_second_install_write)
    try:
        with pytest.raises(_SimulatedPowerLoss):
            clipfinder_update_helper._apply_patch(patch, installed, "1.0.0", work)
    finally:
        monkeypatch.setattr(clipfinder_update_helper.os, "replace", real_replace)

    assert (work / clipfinder_update_helper.PATCH_JOURNAL_FILENAME).is_file()
    assert (installed / "01-first.txt").read_text(encoding="utf-8") == "first-after"
    assert (installed / "02-second.txt").read_text(encoding="utf-8") == "second-before"

    result = clipfinder_update_helper._apply_patch(patch, installed, "1.0.0", work)

    assert result == "1.0.1"
    assert (installed / "01-first.txt").read_text(encoding="utf-8") == "first-after"
    assert (installed / "02-second.txt").read_text(encoding="utf-8") == "second-after"
    assert not (installed / "03-remove.txt").exists()
    assert not (work / clipfinder_update_helper.PATCH_JOURNAL_FILENAME).exists()
    assert not list(work.glob(f"{clipfinder_update_helper.PATCH_TRANSACTION_PREFIX}*"))


def test_committed_patch_survives_interrupted_cleanup(tmp_path: Path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    installed = tmp_path / "installed"
    for directory in (old, new, installed):
        directory.mkdir()
    (old / "app.txt").write_text("before", encoding="utf-8")
    (new / "app.txt").write_text("after", encoding="utf-8")
    (installed / "app.txt").write_text("before", encoding="utf-8")
    cache = build_update_package.cache_release(old, "1.0.0", tmp_path / "cache.zip")
    patch, _manifest = build_update_package.build_patch(cache, "1.0.0", new, "1.0.1", tmp_path)
    work = tmp_path / "update-work"
    real_cleanup = clipfinder_update_helper._cleanup_transaction

    def interrupt_cleanup(_directory, _transaction_id):
        raise _SimulatedPowerLoss()

    monkeypatch.setattr(clipfinder_update_helper, "_cleanup_transaction", interrupt_cleanup)
    try:
        with pytest.raises(_SimulatedPowerLoss):
            clipfinder_update_helper._apply_patch(patch, installed, "1.0.0", work)
    finally:
        monkeypatch.setattr(clipfinder_update_helper, "_cleanup_transaction", real_cleanup)

    journal_path = work / clipfinder_update_helper.PATCH_JOURNAL_FILENAME
    assert json.loads(journal_path.read_text(encoding="utf-8"))["state"] == "committed"
    assert (installed / "app.txt").read_text(encoding="utf-8") == "after"

    clipfinder_update_helper._recover_incomplete_patch(installed, work)

    assert (installed / "app.txt").read_text(encoding="utf-8") == "after"
    assert not journal_path.exists()
    assert not list(work.glob(f"{clipfinder_update_helper.PATCH_TRANSACTION_PREFIX}*"))


@pytest.mark.parametrize(
    "unsafe_path",
    ("D:escape/file.txt", "folder/file.txt:stream", "../escape.txt", "//server/share/file.txt"),
)
def test_patch_rejects_windows_escape_and_ads_paths(tmp_path: Path, unsafe_path: str):
    install_root = tmp_path / "installed"
    install_root.mkdir()
    manifest = {
        "files": [{"path": unsafe_path, "size": 1, "sha256": "0" * 64}],
        "remove": [],
    }

    with pytest.raises(RuntimeError, match="unsafe|outside"):
        clipfinder_update_helper._validate_patch_entries(manifest, install_root)


class _Response(io.BytesIO):
    def __init__(self, value: bytes, *, headers: dict | None = None):
        super().__init__(value)
        self.headers = headers or {}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_update_status_requires_setup_matching_release_tag(monkeypatch):
    payload = {
        "tag_name": "v1.2.4",
        "assets": [
            {"name": "ClipFinder-Setup-1.2.3.exe", "browser_download_url": "https://example.invalid/old", "size": 1},
            {
                "name": "ClipFinder-Setup-1.2.4.exe",
                "browser_download_url": "https://github.com/Eratil/clipfinder-local/releases/download/v1.2.4/ClipFinder-Setup-1.2.4.exe",
                "size": 7,
                "digest": "sha256:" + "a" * 64,
            },
        ],
    }
    monkeypatch.setattr(updates, "__version__", "1.2.3")
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(json.dumps(payload).encode()))

    status = updates.update_status()

    assert status["download_name"] == "ClipFinder-Setup-1.2.4.exe"
    assert status["asset_sha256"] == "a" * 64


def test_download_verifies_size_and_github_digest(tmp_path: Path, monkeypatch):
    content = b"verified update"
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(updater.settings, "clipfinder_data_dir", tmp_path / "data")
    monkeypatch.setattr(
        updater,
        "urlopen",
        lambda *_args, **_kwargs: _Response(content, headers={"Content-Length": str(len(content))}),
    )
    updater._jobs.clear()
    updater._jobs["job"] = {"id": "job", "state": "downloading"}

    updater._download("job", "https://example.invalid", "update.exe", "installer", len(content), digest)

    job = updater.job_status("job")
    assert job and job["state"] == "completed"
    assert Path(job["asset_path"]).read_bytes() == content


def test_download_removes_partial_file_after_digest_failure(tmp_path: Path, monkeypatch):
    content = b"tampered"
    monkeypatch.setattr(updater.settings, "clipfinder_data_dir", tmp_path / "data")
    monkeypatch.setattr(updater, "urlopen", lambda *_args, **_kwargs: _Response(content))
    updater._jobs.clear()
    updater._jobs["job"] = {"id": "job", "state": "downloading"}

    updater._download("job", "https://example.invalid", "update.exe", "installer", len(content), "0" * 64)

    job = updater.job_status("job")
    assert job and job["state"] == "failed"
    assert not list((tmp_path / "updates").glob("*.part"))


def test_download_stops_as_soon_as_github_size_is_exceeded(tmp_path: Path, monkeypatch):
    content = b"larger than declared"
    monkeypatch.setattr(updater.settings, "clipfinder_data_dir", tmp_path / "data")
    monkeypatch.setattr(updater, "urlopen", lambda *_args, **_kwargs: _Response(content))
    updater._jobs.clear()
    updater._jobs["job"] = {"id": "job", "state": "downloading"}

    updater._download("job", "https://example.invalid", "update.exe", "installer", 2, hashlib.sha256(content).hexdigest())

    assert updater.job_status("job")["state"] == "failed"
    assert not list((tmp_path / "updates").glob("*.part"))


def test_staged_update_is_reverified_before_helper_is_started(tmp_path: Path, monkeypatch):
    asset = tmp_path / "update.exe"
    original = b"verified"
    asset.write_bytes(original)
    updater._jobs.clear()
    updater._jobs["job"] = {
        "id": "job",
        "state": "completed",
        "asset_path": str(asset),
        "expected_size": len(original),
        "expected_sha256": hashlib.sha256(original).hexdigest(),
    }
    asset.write_bytes(b"tampered")
    monkeypatch.setattr(updater, "automatic_updates_available", lambda: True)

    with pytest.raises(RuntimeError, match="changed after download"):
        updater.install_downloaded_update("job")


def test_patch_refuses_to_modify_installation_without_rollback_space(tmp_path: Path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    installed = tmp_path / "installed"
    for directory in (old, new, installed):
        directory.mkdir()
    (old / "app.txt").write_text("before", encoding="utf-8")
    (new / "app.txt").write_text("after", encoding="utf-8")
    (installed / "app.txt").write_text("before", encoding="utf-8")
    cache = build_update_package.cache_release(old, "1.0.0", tmp_path / "cache.zip")
    patch, _manifest = build_update_package.build_patch(cache, "1.0.0", new, "1.0.1", tmp_path)
    monkeypatch.setattr(clipfinder_update_helper.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

    with pytest.raises(RuntimeError, match="free disk space"):
        clipfinder_update_helper._apply_patch(patch, installed, "1.0.0", tmp_path / "logs")

    assert (installed / "app.txt").read_text(encoding="utf-8") == "before"


def test_cuda_discovery_matches_cudnn_to_the_same_minor(tmp_path: Path, monkeypatch):
    cuda_bin = tmp_path / "NVIDIA GPU Computing Toolkit" / "CUDA" / "v12.9" / "bin"
    correct_cudnn = tmp_path / "NVIDIA" / "CUDNN" / "v9.24" / "bin" / "12.9" / "x64"
    wrong_cudnn = tmp_path / "NVIDIA" / "CUDNN" / "v9.24" / "bin" / "13.3" / "x64"
    for directory in (cuda_bin, correct_cudnn, wrong_cudnn):
        directory.mkdir(parents=True)
    compatibility = runtime_paths.runtime_compatibility()
    for name in compatibility["cuda"]["required_dlls"]:
        (cuda_bin / name).write_bytes(b"")
    for directory in (correct_cudnn, wrong_cudnn):
        for name in compatibility["cudnn"]["required_dlls"]:
            (directory / name).write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("CUDA_BIN_DIR", raising=False)
    monkeypatch.delenv("CUDNN_BIN_DIR", raising=False)

    pairs = runtime_paths.compatible_runtime_pairs()

    assert [(pair.version, pair.cuda_bin, pair.cudnn_bin) for pair in pairs] == [
        ((12, 9), cuda_bin, correct_cudnn),
    ]


def test_explicit_cuda_pair_has_priority_over_newer_auto_discovery(tmp_path: Path, monkeypatch):
    compatibility = runtime_paths.runtime_compatibility()
    explicit_cuda = tmp_path / "custom" / "v12.3" / "bin"
    explicit_cudnn = tmp_path / "custom-cudnn" / "bin" / "12.3" / "x64"
    auto_cuda = tmp_path / "NVIDIA GPU Computing Toolkit" / "CUDA" / "v12.9" / "bin"
    auto_cudnn = tmp_path / "NVIDIA" / "CUDNN" / "v9.24" / "bin" / "12.9" / "x64"
    for directory in (explicit_cuda, explicit_cudnn, auto_cuda, auto_cudnn):
        directory.mkdir(parents=True)
    for directory in (explicit_cuda, auto_cuda):
        for name in compatibility["cuda"]["required_dlls"]:
            (directory / name).write_bytes(b"")
    for directory in (explicit_cudnn, auto_cudnn):
        for name in compatibility["cudnn"]["required_dlls"]:
            (directory / name).write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("CUDA_BIN_DIR", str(explicit_cuda))
    monkeypatch.setenv("CUDNN_BIN_DIR", str(explicit_cudnn))

    assert runtime_paths.preferred_runtime_pair() == runtime_paths.GpuRuntimePair(
        (12, 3), explicit_cuda, explicit_cudnn,
    )


def test_unversioned_explicit_cuda_path_is_not_guessed_as_12_9(tmp_path: Path, monkeypatch):
    compatibility = runtime_paths.runtime_compatibility()
    cuda_bin = tmp_path / "custom-cuda" / "bin"
    cudnn_bin = tmp_path / "custom-cudnn" / "bin"
    cuda_bin.mkdir(parents=True)
    cudnn_bin.mkdir(parents=True)
    for name in compatibility["cuda"]["required_dlls"]:
        (cuda_bin / name).write_bytes(b"")
    for name in compatibility["cudnn"]["required_dlls"]:
        (cudnn_bin / name).write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "empty"))
    monkeypatch.setenv("CUDA_BIN_DIR", str(cuda_bin))
    monkeypatch.setenv("CUDNN_BIN_DIR", str(cudnn_bin))

    assert runtime_paths.compatible_runtime_pairs() == []
