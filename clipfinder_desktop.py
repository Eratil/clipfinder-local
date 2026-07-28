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
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return

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
    if (ffmpeg_directory / "ffmpeg.exe").is_file():
        os.environ["PATH"] = str(ffmpeg_directory) + os.pathsep + os.environ.get("PATH", "")


def show_error(title: str, text: str) -> None:
    """Show a useful message even when launched through pythonw.exe."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    except Exception:
        print(f"{title}: {text}")


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
    # Importing directly makes the package visible to PyInstaller's analysis.
    from app.main import app

    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, name="ClipFinder local server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if local_server_is_ready():
            return server, thread
        if not thread.is_alive():
            break
        time.sleep(0.15)

    server.should_exit = True
    raise RuntimeError("ClipFinder's local server did not start. Run Start-ClipFinder.cmd to see its diagnostic output.")


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

    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
        server, thread = start_local_server()
        window = webview.create_window(
            "ClipFinder",
            APP_URL,
            width=1500,
            height=950,
            min_size=(1024, 700),
            confirm_close=True,
            background_color="#0e121a",
        )
        window.events.closing += lambda *_: stop_server(server, thread)
        webview.start(icon=str(bundled_asset_path("assets/clipfinder.ico")))
    except Exception as exc:
        show_error("ClipFinder desktop window", str(exc))
        stop_server(server, thread)


if __name__ == "__main__":
    run()
