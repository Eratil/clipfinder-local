"""Small standalone process used to replace a running ClipFinder installation."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--restart-exe", required=True)
    args = parser.parse_args()
    installer, restart_exe = Path(args.installer), Path(args.restart_exe)
    if not installer.is_file() or not restart_exe.is_file():
        return 2

    # API writes are immediate, so ClipFinder has no unsaved editor state to lose.
    time.sleep(1.2)
    subprocess.run(["taskkill", "/PID", str(args.parent_pid), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(1)
    result = subprocess.run([
        str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS",
    ], creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode == 0:
        subprocess.Popen([str(restart_exe)], close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        return 0
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
