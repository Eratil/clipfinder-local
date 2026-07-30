"""Small standalone process used to replace a running ClipFinder installation."""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import time
from pathlib import Path


def _write_log(directory: Path, message: str) -> None:
    """Leave a small diagnostic trail if a silent update cannot finish."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "update-helper.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        # An updater must never fail simply because its diagnostic log is unavailable.
        pass


def _show_error(text: str) -> None:
    """A silent helper still needs to explain an update failure to its user."""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, "ClipFinder update", 0x10)
    except Exception:
        pass


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_elevated_installer(installer: Path, arguments: list[str], directory: Path) -> int:
    """Retry only after an access-denied style failure, with an explicit UAC prompt."""
    script_path = directory / "run-elevated-update.ps1"
    argument_list = ", ".join(_powershell_quote(value) for value in arguments)
    script_path.write_text(
        "$process = Start-Process -FilePath " + _powershell_quote(str(installer)) +
        " -ArgumentList @(" + argument_list + ") -Verb RunAs -Wait -PassThru\n" +
        "if ($null -eq $process) { exit 1 }\n" +
        "exit $process.ExitCode\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode
    finally:
        script_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--restart-exe", required=True)
    args = parser.parse_args()
    installer, restart_exe = Path(args.installer), Path(args.restart_exe)
    log_directory = installer.parent
    if not installer.is_file() or not restart_exe.is_file():
        _write_log(log_directory, "Stopped: installer or application executable is missing.")
        return 2

    # API writes are immediate, so ClipFinder has no unsaved editor state to lose.
    _write_log(log_directory, f"Starting update helper for PID {args.parent_pid}.")
    time.sleep(1.2)
    # Do not use taskkill /T here: the helper is itself a child of ClipFinder,
    # so /T terminates this process before it can launch the installer.
    close_result = subprocess.run(
        ["taskkill", "/PID", str(args.parent_pid), "/F"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _write_log(log_directory, f"Close request finished with exit code {close_result.returncode}.")
    time.sleep(1)
    installer_arguments = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS"]
    result = subprocess.run([str(installer), *installer_arguments], creationflags=subprocess.CREATE_NO_WINDOW)
    _write_log(log_directory, f"Installer finished with exit code {result.returncode}.")
    if result.returncode == 5:
        _write_log(log_directory, "Retrying installer with administrator permission.")
        elevated_exit_code = _run_elevated_installer(installer, installer_arguments, log_directory)
        result = subprocess.CompletedProcess([], elevated_exit_code)
        _write_log(log_directory, f"Elevated installer finished with exit code {result.returncode}.")
    if result.returncode == 0:
        try:
            subprocess.Popen([str(restart_exe)], close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
            _write_log(log_directory, "Restarted ClipFinder successfully.")
        except OSError as exc:
            _write_log(log_directory, f"Update installed but ClipFinder could not restart: {exc}")
            return 3
        return 0
    _write_log(log_directory, "Update failed; ClipFinder was not restarted.")
    _show_error(
        "ClipFinder could not install the update.\n\n"
        f"Installer exit code: {result.returncode}\n"
        f"You can run it manually:\n{installer}"
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
