"""Native Windows window for ClipFinder's local FastAPI application.

This is intentionally a thin wrapper: the existing web interface and local API
stay unchanged, while pywebview provides a normal desktop window and starts the
server only when it is not already running.
"""

from __future__ import annotations

import socket
import os
import sys
import threading
import time
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn


HOST = "127.0.0.1"
PORT = 8000
APP_URL = f"http://{HOST}:{PORT}/"
HEALTH_URL = f"{APP_URL}api/health"
_bundled_dll_directories: list[object] = []
LOADING_PAGE = """<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>ClipFinder</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e121a;color:#edf2fa;font:16px Segoe UI,Arial,sans-serif}.card{display:grid;justify-items:center;gap:18px;text-align:center}.spinner{width:38px;height:38px;border:4px solid #2b374a;border-top-color:#77e3c0;border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}h1{margin:0;font-size:28px}p{max-width:420px;margin:0;color:#9eacc0;line-height:1.5}</style></head><body><div class=\"card\"><div class=\"spinner\"></div><h1>Starting ClipFinder</h1><p>Preparing your local library. This can take a little longer after an update.</p></div></body></html>"""


def bundled_asset_path(relative_path: str) -> Path:
    """Resolve an included asset in both source and PyInstaller builds."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative_path


def user_configuration_directory() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local_app_data / "ClipFinder"


def apply_runtime_configuration() -> None:
    """Load the per-user choices made by the Windows installer bootstrap."""
    if not getattr(sys, "frozen", False):
        return
    config_path = user_configuration_directory() / "runtime.json"
    try:
        # Windows PowerShell 5 writes UTF-8 files with a BOM by default.
        # ``utf-8-sig`` accepts both that installer-created form and plain UTF-8.
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        config = {}

    allowed_values = {
        "whisper_device": {"cuda", "cpu"},
        "whisper_compute_type": {"float16", "int8", "int8_float16"},
        "whisper_model": {"tiny", "base", "small", "medium", "large-v3"},
    }
    for key, values in allowed_values.items():
        value = str(config.get(key, "")).lower()
        if value in values:
            os.environ[key.upper()] = value

    ffmpeg_directory = Path(str(config.get("ffmpeg_bin_dir", "")))
    if not (ffmpeg_directory / "ffmpeg.exe").is_file():
        winget_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.is_dir():
            candidates = list(winget_root.glob("Gyan.FFmpeg.Shared_*/**/ffmpeg.exe"))
            if candidates:
                ffmpeg_directory = candidates[0].parent
    if (ffmpeg_directory / "ffmpeg.exe").is_file():
        os.environ["PATH"] = str(ffmpeg_directory) + os.pathsep + os.environ.get("PATH", "")

    # Keep the two paths explicitly available as well as on PATH.  The CUDA
    # preflight and CTranslate2 use ``os.add_dll_directory`` on modern Python;
    # PATH alone is not a reliable DLL search location there.
    for key, environment_key in (("cuda_bin_dir", "CUDA_BIN_DIR"), ("cudnn_bin_dir", "CUDNN_BIN_DIR")):
        directory = Path(str(config.get(key, "")))
        if directory.is_dir():
            os.environ[environment_key] = str(directory)
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


def show_error(title: str, text: str) -> None:
    """Show a useful message even when launched through pythonw.exe."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        print(f"{title}: {text}")


def play_close_confirmation_sound() -> None:
    """Play the bundled soft confirmation sound without using a Windows beep."""
    if os.name != "nt":
        return
    sound_path = bundled_asset_path("assets/close-pop.wav")
    if not sound_path.is_file():
        return
    try:
        import ctypes

        winmm = ctypes.windll.winmm
        winmm.mciSendStringW("close ClipFinderCloseSound", None, 0, None)
        # WAV starts more promptly than MP3 on the Windows MCI backend.
        winmm.mciSendStringW(f'open "{sound_path}" type waveaudio alias ClipFinderCloseSound', None, 0, None)
        # The bundled WAV is already attenuated to a quiet 5%, which is more
        # reliable across MCI/Windows audio backends than runtime volume calls.
        winmm.mciSendStringW("play ClipFinderCloseSound from 0", None, 0, None)
    except OSError:
        # Closing must always remain possible if a machine cannot play MP3.
        pass


def confirm_application_close(window) -> bool:
    """Use a quiet confirmation dialog and cancel the native noisy one."""
    play_close_confirmation_sound()
    return bool(window.create_confirmation_dialog("Close ClipFinder", "Do you want to close ClipFinder?"))


def local_server_is_ready() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=0.6) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def configure_frozen_data_directory() -> None:
    """Keep user videos and the database outside an installed application folder."""
    if not getattr(sys, "frozen", False) or os.environ.get("CLIPFINDER_DATA_DIR"):
        return
    os.environ["CLIPFINDER_DATA_DIR"] = str(user_configuration_directory() / "data")


def configure_bundled_dll_directories() -> None:
    """Make native libraries in PyInstaller subfolders discoverable on Windows.

    SciPy and NumPy place OpenBLAS DLLs in ``*.libs`` folders.  Windows does
    not search those sibling directories for a loaded ``.pyd`` automatically,
    which otherwise causes a misleading WinError 126 in the installed build.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    runtime_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
    for directory in (runtime_root, runtime_root / "scipy.libs", runtime_root / "numpy.libs", runtime_root / "torch" / "lib"):
        if directory.is_dir():
            _bundled_dll_directories.append(os.add_dll_directory(str(directory)))


def port_is_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((HOST, PORT)) == 0


def start_local_server() -> tuple[uvicorn.Server | None, threading.Thread | None]:
    """Start one local backend, or reuse the existing launcher backend."""
    if local_server_is_ready():
        return None, None
    if port_is_in_use():
        raise RuntimeError(
            "Port 8000 is occupied by another application. Close that application "
            "or start ClipFinder's existing launcher first."
        )

    configure_frozen_data_directory()
    apply_runtime_configuration()
    configure_bundled_dll_directories()
    # Importing directly makes the package visible to PyInstaller's analysis.
    from app.main import app

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, name="ClipFinder local server", daemon=True)
    thread.start()

    # The first start after an update may need to migrate an existing local
    # database.  It is safe but can take longer than the old 20-second limit.
    # Subsequent starts are quick because migrations are recorded as complete.
    startup_timeout_seconds = 90
    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        if local_server_is_ready():
            return server, thread
        if not thread.is_alive():
            break
        time.sleep(0.15)

    server.should_exit = True
    raise RuntimeError(
        f"ClipFinder's local server did not start within {startup_timeout_seconds} seconds. "
        "Run Start-ClipFinder.cmd to see its diagnostic output."
    )


def stop_server(server: uvicorn.Server | None, thread: threading.Thread | None) -> None:
    if server is None:
        return
    server.should_exit = True
    if thread is not None:
        thread.join(timeout=8)


def run() -> None:
    try:
        import webview
    except ImportError:
        show_error(
            "ClipFinder desktop window",
            "The desktop window component is missing. Run Install-ClipFinder.cmd again "
            "or install the project requirements, then reopen ClipFinder.",
        )
        return

    server_state: dict[str, uvicorn.Server | threading.Thread | None] = {"server": None, "thread": None}
    try:
        window = webview.create_window(
            "ClipFinder",
            html=LOADING_PAGE,
            width=1500,
            height=950,
            min_size=(1024, 700),
            confirm_close=False,
            background_color="#0e121a",
        )
        window.events.closing += confirm_application_close
        window.events.closed += lambda *_: stop_server(server_state["server"], server_state["thread"])

        def start_backend() -> None:
            try:
                server, thread = start_local_server()
                server_state["server"] = server
                server_state["thread"] = thread
                window.load_url(APP_URL)
            except Exception as exc:
                show_error("ClipFinder desktop window", str(exc))
                window.load_html(f"<html><body style='background:#0e121a;color:#edf2fa;font:16px Segoe UI;padding:40px'><h2>ClipFinder could not start</h2><p>{exc}</p><p>Run Start-ClipFinder.cmd to see diagnostic output.</p></body></html>")

        webview.start(start_backend, icon=str(bundled_asset_path("assets/clipfinder.ico")))
    except Exception as exc:
        show_error("ClipFinder desktop window", str(exc))
        stop_server(server_state["server"], server_state["thread"])


if __name__ == "__main__":
    run()
