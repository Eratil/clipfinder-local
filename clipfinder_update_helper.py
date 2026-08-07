"""Small standalone process used to replace a running ClipFinder installation."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath


PATCH_MANIFEST_PATH = "__clipfinder_patch__/manifest.json"
PATCH_FILES_PREFIX = "files/"
MAX_PATCH_FILES = 100_000
MAX_PATCH_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
PATCH_FREE_SPACE_RESERVE = 128 * 1024 * 1024
PATCH_JOURNAL_FILENAME = "compact-update-transaction.json"
PATCH_TRANSACTION_PREFIX = "patch-transaction-"
PATCH_JOURNAL_SCHEMA = 1


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
            process = subprocess.Popen([str(restart_exe)], close_fds=True, cwd=str(restart_exe.parent))
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


def _durable_copy(source: Path, destination: Path) -> None:
    """Copy a file and flush its contents before it can protect a mutation."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    try:
        shutil.copystat(source, destination)
    except OSError:
        # Metadata is useful but file contents are the rollback guarantee.
        pass


def _atomic_write_json(path: Path, value: dict) -> None:
    """Persist a journal revision without ever exposing a partial JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _transaction_root(directory: Path, transaction_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise RuntimeError("The compact update recovery journal has an invalid transaction identifier.")
    root = directory.resolve()
    transaction = (root / f"{PATCH_TRANSACTION_PREFIX}{transaction_id}").resolve()
    try:
        transaction.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("The compact update recovery journal points outside its work directory.") from exc
    return transaction


def _journal_path(directory: Path) -> Path:
    return directory / PATCH_JOURNAL_FILENAME


def _write_patch_journal(directory: Path, journal: dict) -> None:
    _atomic_write_json(_journal_path(directory), journal)


def _load_patch_journal(directory: Path, install_root: Path) -> dict | None:
    path = _journal_path(directory)
    if not path.is_file():
        return None
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "An unfinished compact update has an unreadable recovery journal. Use the full setup installer instead."
        ) from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema") != PATCH_JOURNAL_SCHEMA
        or journal.get("app") != "ClipFinder"
        or journal.get("state") not in {"applying", "committed"}
        or not isinstance(journal.get("operations"), list)
        or len(journal["operations"]) > MAX_PATCH_FILES
    ):
        raise RuntimeError("An unfinished compact update has an invalid recovery journal. Use the full setup installer instead.")
    transaction_id = str(journal.get("transaction_id") or "")
    _transaction_root(directory, transaction_id)
    recorded_root = Path(str(journal.get("install_root") or "")).resolve()
    if os.path.normcase(str(recorded_root)) != os.path.normcase(str(install_root.resolve())):
        raise RuntimeError("An unfinished compact update belongs to a different ClipFinder installation.")
    seen: set[Path] = set()
    for operation in journal["operations"]:
        if (
            not isinstance(operation, dict)
            or operation.get("action") not in {"replace", "remove"}
            or operation.get("state") not in {"prepared", "applied"}
        ):
            raise RuntimeError("The compact update recovery journal contains an invalid operation.")
        relative = _safe_relative(str(operation.get("path") or ""))
        if relative in seen or not isinstance(operation.get("had_original"), bool):
            raise RuntimeError("The compact update recovery journal contains a conflicting operation.")
        seen.add(relative)
        for field in ("previous_sha256", "new_sha256"):
            digest = operation.get(field)
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise RuntimeError("The compact update recovery journal contains an invalid file digest.")
        if operation["had_original"] and not operation.get("previous_sha256"):
            raise RuntimeError("The compact update recovery journal is missing an original file digest.")
        if operation["action"] == "replace" and not operation.get("new_sha256"):
            raise RuntimeError("The compact update recovery journal is missing a replacement file digest.")
        if operation["action"] == "remove" and not operation["had_original"]:
            raise RuntimeError("The compact update recovery journal contains an invalid removal operation.")
    return journal


def _cleanup_transaction(directory: Path, transaction_id: str) -> None:
    transaction = _transaction_root(directory, transaction_id)
    # Once the journal is removed the installation is already either fully
    # committed or fully restored. Leftover copies are harmless and are
    # removed by the orphan cleanup on a later invocation.
    _journal_path(directory).unlink(missing_ok=True)
    shutil.rmtree(transaction, ignore_errors=True)


def _cleanup_orphan_transactions(directory: Path) -> None:
    """Remove staging left by a crash that happened before the first mutation."""
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    pattern = re.compile(re.escape(PATCH_TRANSACTION_PREFIX) + r"[0-9a-f]{32}")
    for child in children:
        if child.is_dir() and pattern.fullmatch(child.name):
            shutil.rmtree(child, ignore_errors=True)


def _rollback_patch_transaction(journal: dict, install_root: Path, directory: Path) -> None:
    transaction = _transaction_root(directory, str(journal["transaction_id"]))
    rollback_root = transaction / "rollback"
    for operation in reversed(journal["operations"]):
        relative = _safe_relative(str(operation["path"]))
        target = _install_target(install_root, relative)
        temporary_new = target.with_name(f".{target.name}.clipfinder-new")
        temporary_new.unlink(missing_ok=True)
        if operation["had_original"]:
            backup = _install_target(rollback_root, relative)
            previous_hash = str(operation["previous_sha256"])
            if not backup.is_file() or _sha256(backup) != previous_hash:
                raise RuntimeError(
                    "The compact update rollback copy is missing or damaged. Use the full setup installer instead."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_restore = target.with_name(f".{target.name}.clipfinder-restore")
            temporary_restore.unlink(missing_ok=True)
            _durable_copy(backup, temporary_restore)
            os.replace(temporary_restore, target)
            if _sha256(target) != previous_hash:
                raise RuntimeError("The compact update could not verify a restored file.")
        elif target.exists():
            expected_hash = operation.get("new_sha256")
            if not target.is_file() or not expected_hash or _sha256(target) != expected_hash:
                raise RuntimeError(
                    "The compact update found an unexpected file while recovering. Use the full setup installer instead."
                )
            target.unlink()


def _committed_transaction_is_intact(journal: dict, install_root: Path) -> bool:
    for operation in journal["operations"]:
        relative = _safe_relative(str(operation["path"]))
        target = _install_target(install_root, relative)
        if operation["action"] == "replace":
            if not target.is_file() or _sha256(target) != operation["new_sha256"]:
                return False
        elif target.exists():
            return False
    return True


def _recover_incomplete_patch(install_root: Path, directory: Path) -> None:
    """Finish cleanup or roll back a transaction interrupted by process/power loss."""
    directory.mkdir(parents=True, exist_ok=True)
    journal = _load_patch_journal(directory, install_root)
    if journal is None:
        _cleanup_orphan_transactions(directory)
        return
    transaction_id = str(journal["transaction_id"])
    if journal["state"] == "committed" and _committed_transaction_is_intact(journal, install_root):
        _write_log(directory, "Recovered a committed compact update after an interrupted cleanup.")
        _cleanup_transaction(directory, transaction_id)
        return
    _rollback_patch_transaction(journal, install_root, directory)
    _write_log(directory, "Rolled back an incomplete compact update from its durable recovery journal.")
    _cleanup_transaction(directory, transaction_id)


def _safe_relative(value: str) -> Path:
    relative = PurePosixPath(str(value).replace("\\", "/"))
    if (
        not value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise RuntimeError("The update patch contains an unsafe file path.")
    return Path(*relative.parts)


def _install_target(install_root: Path, relative: Path) -> Path:
    """Resolve a manifest target and keep it below the installation root."""
    root = install_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("The update patch contains a file path outside ClipFinder.") from exc
    return target


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
        changed.append((item, _install_target(install_root, relative)))
    for item in manifest["remove"]:
        if not isinstance(item, dict) or not isinstance(item.get("previous_sha256"), str):
            raise RuntimeError("The update patch contains an invalid removal entry.")
        relative = _safe_relative(str(item.get("path") or ""))
        if relative in seen:
            raise RuntimeError("The update patch contains a conflicting file entry.")
        seen.add(relative)
        removed.append((item, _install_target(install_root, relative)))
    return changed, removed


def _verify_previous_file(target: Path, expected_hash: str | None) -> None:
    if expected_hash is None:
        return
    if not target.is_file() or _sha256(target) != expected_hash:
        raise RuntimeError("This compact update does not match the installed ClipFinder files. Use the full setup installer instead.")


def _verify_patch_free_space(
    changed: list[tuple[dict, Path]],
    removed: list[tuple[dict, Path]],
    install_root: Path,
    working_directory: Path,
) -> None:
    # The log/update work directory may not exist on the first compact update.
    # Create it before asking Windows for the volume's free-space statistics.
    working_directory.mkdir(parents=True, exist_ok=True)
    staged_bytes = sum(int(item["size"]) for item, _target in changed)
    rollback_bytes = sum(
        target.stat().st_size for _item, target in (*changed, *removed) if target.is_file()
    )
    largest_replacement = max((int(item["size"]) for item, _target in changed), default=0)
    working_required = staged_bytes + rollback_bytes + PATCH_FREE_SPACE_RESERVE
    target_required = largest_replacement + PATCH_FREE_SPACE_RESERVE
    working_free = shutil.disk_usage(working_directory).free
    target_free = shutil.disk_usage(install_root).free
    same_volume = os.path.splitdrive(str(working_directory.resolve()))[0].lower() == os.path.splitdrive(str(install_root.resolve()))[0].lower()
    if same_volume:
        if min(working_free, target_free) < working_required + target_required:
            raise RuntimeError("There is not enough free disk space to stage and safely roll back this compact update.")
    elif working_free < working_required or target_free < target_required:
        raise RuntimeError("There is not enough free disk space to stage and safely roll back this compact update.")


def _apply_patch(patch_path: Path, install_root: Path, current_version: str, directory: Path) -> str:
    """Verify a patch completely, then replace only its listed files.

    Changed originals are copied to a durable rollback directory first.  A
    journal written before every mutation lets a later helper invocation undo
    work interrupted by process termination or power loss.
    """
    _recover_incomplete_patch(install_root, directory)
    transaction_id = uuid.uuid4().hex
    transaction = _transaction_root(directory, transaction_id)
    stage = transaction / "stage"
    rollback = transaction / "rollback"
    journal: dict | None = None
    try:
        with zipfile.ZipFile(patch_path) as archive:
            manifest = _read_patch_manifest(archive)
            if manifest.get("from_version") != current_version or not isinstance(manifest.get("to_version"), str):
                raise RuntimeError("This compact update is for a different ClipFinder version. Use the full setup installer instead.")
            changed, removed = _validate_patch_entries(manifest, install_root)
            if len(changed) + len(removed) > MAX_PATCH_FILES:
                raise RuntimeError("The update patch contains too many file operations.")
            expected_members = {PATCH_MANIFEST_PATH}
            expected_members.update(PATCH_FILES_PREFIX + _safe_relative(str(item["path"])).as_posix() for item, _target in changed)
            actual_members = {member.filename for member in archive.infolist() if not member.is_dir()}
            if actual_members != expected_members:
                raise RuntimeError("The update patch contains unexpected or missing archive entries.")
            total_size = sum(int(item["size"]) for item, _target in changed)
            if total_size < 0 or total_size > MAX_PATCH_UNCOMPRESSED_BYTES:
                raise RuntimeError("The update patch is larger than the supported safety limit.")
            _verify_patch_free_space(changed, removed, install_root, directory)
            for item, target in changed:
                _verify_previous_file(target, item.get("previous_sha256"))
                relative = _safe_relative(str(item["path"]))
                member_name = PATCH_FILES_PREFIX + relative.as_posix()
                try:
                    member = archive.getinfo(member_name)
                except KeyError as exc:
                    raise RuntimeError("The update patch is missing a changed file.") from exc
                if member.file_size != item["size"]:
                    raise RuntimeError("The update patch file size does not match its manifest.")
                staged = stage / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, staged.open("wb") as output:
                    shutil.copyfileobj(source, output)
                if staged.stat().st_size != item["size"] or _sha256(staged) != item["sha256"]:
                    raise RuntimeError("A downloaded update file did not pass verification.")
            for item, target in removed:
                _verify_previous_file(target, item["previous_sha256"])

        journal = {
            "schema": PATCH_JOURNAL_SCHEMA,
            "app": "ClipFinder",
            "state": "applying",
            "transaction_id": transaction_id,
            "install_root": str(install_root.resolve()),
            "from_version": current_version,
            "to_version": str(manifest["to_version"]),
            "operations": [],
        }
        _write_patch_journal(directory, journal)
        for item, target in changed:
            relative = _safe_relative(str(item["path"]))
            if target.exists() and not target.is_file():
                raise RuntimeError("A compact update target is not a regular file.")
            had_original = target.is_file()
            previous_hash = _sha256(target) if had_original else None
            if had_original:
                backup = rollback / relative
                _durable_copy(target, backup)
                if _sha256(backup) != previous_hash:
                    raise RuntimeError("A compact update rollback copy did not pass verification.")
            operation = {
                "action": "replace",
                "path": relative.as_posix(),
                "had_original": had_original,
                "previous_sha256": previous_hash,
                "new_sha256": str(item["sha256"]),
                "state": "prepared",
            }
            journal["operations"].append(operation)
            _write_patch_journal(directory, journal)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_target = target.with_name(f".{target.name}.clipfinder-new")
            temporary_target.unlink(missing_ok=True)
            _durable_copy(stage / relative, temporary_target)
            os.replace(temporary_target, target)
            if _sha256(target) != item["sha256"]:
                raise RuntimeError("A copied update file did not pass verification.")
            operation["state"] = "applied"
            _write_patch_journal(directory, journal)
        for item, target in removed:
            relative = _safe_relative(str(item["path"]))
            backup = rollback / relative
            if not target.is_file():
                raise RuntimeError("A compact update removal target is not a regular file.")
            previous_hash = _sha256(target)
            _durable_copy(target, backup)
            if _sha256(backup) != previous_hash:
                raise RuntimeError("A compact update rollback copy did not pass verification.")
            operation = {
                "action": "remove",
                "path": relative.as_posix(),
                "had_original": True,
                "previous_sha256": previous_hash,
                "new_sha256": None,
                "state": "prepared",
            }
            journal["operations"].append(operation)
            _write_patch_journal(directory, journal)
            target.unlink()
            operation["state"] = "applied"
            _write_patch_journal(directory, journal)
        journal["state"] = "committed"
        _write_patch_journal(directory, journal)
        _write_log(directory, f"Applied verified compact update {current_version} -> {manifest['to_version']} ({len(changed)} changed, {len(removed)} removed files).")
        try:
            _cleanup_transaction(directory, transaction_id)
        except OSError as cleanup_error:
            # The committed journal is safe to clean on the next helper run.
            _write_log(directory, f"Committed update cleanup will be retried later: {cleanup_error}")
        return str(manifest["to_version"])
    except Exception:
        if journal is not None and _journal_path(directory).is_file():
            try:
                persisted = _load_patch_journal(directory, install_root)
                if persisted is not None:
                    _rollback_patch_transaction(persisted, install_root, directory)
                    _cleanup_transaction(directory, transaction_id)
            except Exception as rollback_error:
                # Preserve the journal and rollback copies for an elevated or
                # later helper invocation instead of discarding recovery data.
                _write_log(directory, f"Rollback remains pending in the recovery journal: {rollback_error}")
        raise
    finally:
        if not _journal_path(directory).exists():
            shutil.rmtree(transaction, ignore_errors=True)


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
        installer.unlink(missing_ok=True)
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
    patch.unlink(missing_ok=True)
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
