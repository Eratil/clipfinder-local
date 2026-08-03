"""Small standalone process used to replace a running ClipFinder installation."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath


PATCH_MANIFEST_PATH = "__clipfinder_patch__/manifest.json"
PATCH_FILES_PREFIX = "files/"


def _write_log(directory: Path, message: str) -> None:
    """Leave a small diagnostic trail if a silent update cannot finish."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "update-helper.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _show_error(text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "ClipFinder update", 0x10)
    except Exception:
        pass


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_elevated_installer(installer: Path, arguments: list[str], directory: Path) -> int:
    script_path = directory / "run-elevated-update.ps1"
    argument_list = ", ".join(_powershell_quote(value) for value in arguments)
    script_path.write_text(
        "$process = Start-Process -FilePath " + _powershell_quote(str(installer)) +
        " -ArgumentList @(" + argument_list + ") -Verb RunAs -Wait -PassThru\n" +
        "if ($null -eq $process) { exit 1 }\nexit $process.ExitCode\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)], creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode
    finally:
        script_path.unlink(missing_ok=True)


def _run_elevated_helper(arguments: list[str], directory: Path) -> int:
    """Restart this helper as administrator only when a patch needs it."""
    return _run_elevated_installer(Path(sys.executable), arguments, directory)


def _restart_clipfinder(restart_exe: Path, directory: Path) -> bool:
    for attempt in range(1, 3):
        try:
            process = subprocess.Popen([str(restart_exe)], close_fds=True)
        except OSError as exc:
            _write_log(directory, f"Restart attempt {attempt} could not start ClipFinder: {exc}")
        else:
            _write_log(directory, f"Restart attempt {attempt} started ClipFinder (PID {process.pid}).")
            time.sleep(5)
            if process.poll() is None:
                _write_log(directory, "ClipFinder remained running after restart.")
                return True
            _write_log(directory, f"Restart attempt {attempt} exited immediately with code {process.returncode}.")
        time.sleep(2)
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> Path:
    relative = PurePosixPath(str(value).replace("\\", "/"))
    if not value or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("The update patch contains an unsafe file path.")
    return Path(*relative.parts)


def _read_patch_manifest(archive: zipfile.ZipFile) -> dict:
    try:
        value = json.loads(archive.read(PATCH_MANIFEST_PATH).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The update patch manifest is invalid.") from exc
    if value.get("schema") != 1 or value.get("app") != "ClipFinder" or not isinstance(value.get("files"), list) or not isinstance(value.get("remove"), list):
        raise RuntimeError("The update patch manifest has an unsupported format.")
    return value


def _validate_patch_entries(manifest: dict, install_root: Path) -> tuple[list[tuple[dict, Path]], list[tuple[dict, Path]]]:
    changed: list[tuple[dict, Path]] = []
    removed: list[tuple[dict, Path]] = []
    seen: set[Path] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("sha256"), str) or not isinstance(item.get("size"), int):
            raise RuntimeError("The update patch contains an invalid file entry.")
        relative = _safe_relative(str(item.get("path") or ""))
        if relative in seen:
            raise RuntimeError("The update patch contains a duplicate file entry.")
        seen.add(relative)
        changed.append((item, install_root / relative))
    for item in manifest["remove"]:
        if not isinstance(item, dict) or not isinstance(item.get("previous_sha256"), str):
            raise RuntimeError("The update patch contains an invalid removal entry.")
        relative = _safe_relative(str(item.get("path") or ""))
        if relative in seen:
            raise RuntimeError("The update patch contains a conflicting file entry.")
        seen.add(relative)
        removed.append((item, install_root / relative))
    return changed, removed


def _verify_previous_file(target: Path, expected_hash: str | None) -> None:
    if expected_hash is None:
        return
    if not target.is_file() or _sha256(target) != expected_hash:
        raise RuntimeError("This compact update does not match the installed ClipFinder files. Use the full setup installer instead.")


def _apply_patch(patch_path: Path, install_root: Path, current_version: str, directory: Path) -> str:
    """Verify a patch completely, then replace only its listed files.

    Changed originals are copied to a temporary rollback directory first.  An
    invalid ZIP, wrong source version, modified installation or failed write
    therefore cannot leave a partially updated app behind.
    """
    stage = directory / f"patch-stage-{uuid.uuid4().hex}"
    rollback = directory / f"patch-rollback-{uuid.uuid4().hex}"
    touched: list[tuple[Path, Path | None]] = []
    try:
        with zipfile.ZipFile(patch_path) as archive:
            manifest = _read_patch_manifest(archive)
            if manifest.get("from_version") != current_version or not isinstance(manifest.get("to_version"), str):
                raise RuntimeError("This compact update is for a different ClipFinder version. Use the full setup installer instead.")
            changed, removed = _validate_patch_entries(manifest, install_root)
            for item, target in changed:
                _verify_previous_file(target, item.get("previous_sha256"))
                relative = _safe_relative(str(item["path"]))
                member_name = PATCH_FILES_PREFIX + relative.as_posix()
                try:
                    member = archive.getinfo(member_name)
                except KeyError as exc:
                    raise RuntimeError("The update patch is missing a changed file.") from exc
                staged = stage / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, staged.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if staged.stat().st_size != item["size"] or _sha256(staged) != item["sha256"]:
                    raise RuntimeError("A downloaded update file did not pass verification.")
            for item, target in removed:
                _verify_previous_file(target, item["previous_sha256"])

        for item, target in changed:
            relative = _safe_relative(str(item["path"]))
            backup = rollback / relative if target.exists() else None
            if backup:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            touched.append((target, backup))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_target = target.with_name(f".{target.name}.clipfinder-new")
            temporary_target.unlink(missing_ok=True)
            shutil.copy2(stage / relative, temporary_target)
            os.replace(temporary_target, target)
            if _sha256(target) != item["sha256"]:
                raise RuntimeError("A copied update file did not pass verification.")
        for item, target in removed:
            relative = _safe_relative(str(item["path"]))
            backup = rollback / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            touched.append((target, backup))
            target.unlink()
        _write_log(directory, f"Applied verified compact update {current_version} -> {manifest['to_version']} ({len(changed)} changed, {len(removed)} removed files).")
        return str(manifest["to_version"])
    except Exception:
        for target, backup in reversed(touched):
            try:
                if backup and backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as rollback_error:
                _write_log(directory, f"Rollback could not restore a file: {rollback_error}")
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(rollback, ignore_errors=True)


def _close_parent(parent_pid: int, directory: Path) -> None:
    if parent_pid <= 0:
        return
    time.sleep(1.2)
    # Do not use taskkill /T: the helper is itself a child of ClipFinder.
    result = subprocess.run(["taskkill", "/PID", str(parent_pid), "/F"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    _write_log(directory, f"Close request finished with exit code {result.returncode}.")
    time.sleep(1)


def _finish_restart(restart_exe: Path, directory: Path, success_message: str) -> int:
    time.sleep(2)
    if _restart_clipfinder(restart_exe, directory):
        _write_log(directory, success_message)
        return 0
    _write_log(directory, "Update completed but ClipFinder could not remain running after restart.")
    _show_error("ClipFinder was updated, but it did not start automatically.\n\nStart it manually from:\n" + str(restart_exe))
    return 3


def _install_full(installer: Path, restart_exe: Path, directory: Path) -> int:
    arguments = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS"]
    result = subprocess.run([str(installer), *arguments], creationflags=subprocess.CREATE_NO_WINDOW)
    _write_log(directory, f"Installer finished with exit code {result.returncode}.")
    if result.returncode == 5:
        _write_log(directory, "Retrying installer with administrator permission.")
        result = subprocess.CompletedProcess([], _run_elevated_installer(installer, arguments, directory))
        _write_log(directory, f"Elevated installer finished with exit code {result.returncode}.")
    if result.returncode == 0:
        return _finish_restart(restart_exe, directory, "Restarted ClipFinder successfully after full update.")
    _show_error("ClipFinder could not install the update.\n\nInstaller exit code: " + str(result.returncode) + "\nYou can run it manually:\n" + str(installer))
    return result.returncode


def _apply_compact_update(args, patch: Path, restart_exe: Path, directory: Path) -> int:
    try:
        target_version = _apply_patch(patch, restart_exe.parent, args.current_version, directory)
    except PermissionError as exc:
        if args.elevated:
            _write_log(directory, f"Elevated compact update failed with access denied: {exc}")
            _show_error("ClipFinder needs permission to apply this compact update. Use the full setup installer instead.")
            return 5
        _write_log(directory, "Retrying compact update with administrator permission.")
        elevated_arguments = ["--parent-pid", "0", "--patch", str(patch), "--restart-exe", str(restart_exe), "--current-version", args.current_version, "--elevated"]
        return _run_elevated_helper(elevated_arguments, directory)
    except Exception as exc:
        _write_log(directory, f"Compact update failed safely: {exc}")
        _show_error("ClipFinder could not apply the compact update safely.\n\nUse the full setup installer from the GitHub Release instead.\n\nDetails: " + str(exc))
        return 4
    return _finish_restart(restart_exe, directory, f"Restarted ClipFinder successfully after compact update to {target_version}.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", required=True, type=int)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--installer")
    source.add_argument("--patch")
    parser.add_argument("--current-version", default="")
    parser.add_argument("--restart-exe", required=True)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    restart_exe = Path(args.restart_exe)
    asset = Path(args.installer or args.patch)
    log_directory = asset.parent
    if not asset.is_file() or not restart_exe.is_file():
        _write_log(log_directory, "Stopped: update file or application executable is missing.")
        return 2
    if args.patch and not args.current_version:
        _write_log(log_directory, "Stopped: compact update has no current version.")
        return 2
    _write_log(log_directory, f"Starting {'compact' if args.patch else 'full'} update helper for PID {args.parent_pid}.")
    _close_parent(args.parent_pid, log_directory)
    if args.installer:
        return _install_full(asset, restart_exe, log_directory)
    return _apply_compact_update(args, asset, restart_exe, log_directory)


if __name__ == "__main__":
    raise SystemExit(main())
