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
import tempfile
import html
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
_single_instance_mutex: object | None = None
_SINGLE_INSTANCE_MUTEX_NAME = r"Local\ClipFinderDesktopSingleton"
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


def acquire_single_instance_lock() -> bool:
    """Claim the per-user Windows mutex used by the desktop application.

    The local API always uses a fixed port, so allowing a second desktop
    process only creates a confusing extra window.  A named mutex protects the
    whole startup path, including the short period before the API is ready.
    ``Local`` deliberately scopes this to the interactive user session rather
    than interfering with a different Windows user on the same computer.
    """
    global _single_instance_mutex
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        mutex = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        if not mutex:
            raise OSError("Windows could not create the ClipFinder instance lock.")
        # ERROR_ALREADY_EXISTS means another ClipFinder desktop process owns
        # this named mutex.  Close only our newly-created handle in that case.
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(mutex)
            return False
        _single_instance_mutex = mutex
        return True
    except OSError:
        # Do not prevent a user from opening the application if Windows itself
        # cannot provide this optional protection.  The existing port check is
        # still a safe final guard in that unusual case.
        return True


def release_single_instance_lock() -> None:
    """Release the desktop mutex when this process has finished."""
    global _single_instance_mutex
    if _single_instance_mutex is None or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(_single_instance_mutex)
    except OSError:
        pass
    finally:
        _single_instance_mutex = None


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
            "or close the other ClipFinder instance, then try again."
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
        r"Check %LOCALAPPDATA%\ClipFinder\data\logs\clipfinder.log and "
        r"%LOCALAPPDATA%\ClipFinder\setup-status.txt for diagnostic details."
    )


def stop_server(server: uvicorn.Server | None, thread: threading.Thread | None) -> None:
    if server is None:
        return
    server.should_exit = True
    if thread is not None:
        thread.join(timeout=8)


def run_packaged_smoke_check() -> int:
    """Import the frozen runtime without opening a window or touching user data."""
    if not getattr(sys, "frozen", False):
        print("The packaged smoke check must be run through ClipFinder.exe.", file=sys.stderr)
        return 2
    try:
        # The base artifact is intentionally CPU-only. Do not read a user's
        # runtime.json here because an installed CUDA toolkit would hide a bad
        # or accidentally GPU-linked base package.
        os.environ["WHISPER_DEVICE"] = "cpu"
        os.environ["WHISPER_COMPUTE_TYPE"] = "int8"
        # Importing app.main creates the settings singleton. Point it at a
        # disposable directory before that import so the release smoke test
        # can never migrate or create files in the user's real library.
        with tempfile.TemporaryDirectory(prefix="clipfinder-smoke-") as smoke_data:
            os.environ["CLIPFINDER_DATA_DIR"] = smoke_data
            configure_bundled_dll_directories()
            import certifi  # noqa: F401
            import ctranslate2
            import cv2  # noqa: F401
            import fastapi  # noqa: F401
            import faster_whisper  # noqa: F401
            import numpy  # noqa: F401
            import scipy  # noqa: F401
            import sentence_transformers  # noqa: F401
            import torch
            import truststore  # noqa: F401
            import webview  # noqa: F401
            import yt_dlp  # noqa: F401
            from app.main import app as packaged_app
            from app.services.model_catalog import model_identity, runtime_compatibility

            compatibility = runtime_compatibility()
            if ctranslate2.__version__ != str(compatibility.get("ctranslate2") or ""):
                raise RuntimeError(
                    "Packaged CTranslate2 version does not match runtime-compatibility.json: "
                    f"{ctranslate2.__version__}."
                )
            for model_kind in ("transcription_default", "transcription_fast", "similarity"):
                model_identity(model_kind)
            if packaged_app is None:
                raise RuntimeError("The packaged FastAPI application was not created.")
            if torch.version.cuda is not None:
                raise RuntimeError(f"Base package contains CUDA-enabled PyTorch ({torch.version.cuda}).")
            for relative in (
                "app/static/index.html",
                "assets/clipfinder.ico",
                "assets/close-pop.wav",
                "assets/runtime-compatibility.json",
            ):
                if not bundled_asset_path(relative).is_file():
                    raise RuntimeError(f"Bundled asset is missing: {relative}")
            if not Path(sys.executable).with_name("ClipFinderUpdateHelper.exe").is_file():
                raise RuntimeError("ClipFinderUpdateHelper.exe is missing.")
    except Exception as exc:
        print(f"Packaged smoke check failed: {exc}", file=sys.stderr)
        return 1
    print("Packaged ClipFinder smoke check passed.")
    return 0


def run_gpu_runtime_probe(cuda_bin: str, cudnn_bin: str) -> int:
    """Verify the exact CUDA/cuDNN pair with the packaged CTranslate2 build."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return 2
    handles: list[object] = []
    try:
        import ctypes

        for value in (cuda_bin, cudnn_bin):
            directory = Path(value).resolve()
            if not directory.is_dir():
                raise RuntimeError(f"Runtime directory is missing: {directory}")
            handles.append(os.add_dll_directory(str(directory)))
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        from app.services.model_catalog import runtime_compatibility

        compatibility = runtime_compatibility()
        for component, directory in (("cuda", Path(cuda_bin)), ("cudnn", Path(cudnn_bin))):
            for name in compatibility[component]["required_dlls"]:
                ctypes.WinDLL(str(directory / name))
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("CTranslate2 did not detect a CUDA device.")
    except Exception as exc:
        print(f"GPU runtime probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Keep handles alive through the CTranslate2 check, then release them.
        handles.clear()
    return 0


def run() -> None:
    if not acquire_single_instance_lock():
        show_error(
            "ClipFinder is already running",
            "ClipFinder is already open. Switch to the existing window instead of starting another instance.",
        )
        return
    try:
        try:
            import webview
        except ImportError:
            show_error(
                "ClipFinder desktop window",
                "The desktop window component is missing. Repair the installed application "
                "or install the source-project requirements, then reopen ClipFinder.",
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
                    safe_error = html.escape(str(exc))
                    window.load_html(
                        "<html><body style='background:#0e121a;color:#edf2fa;font:16px Segoe UI;padding:40px'>"
                        f"<h2>ClipFinder could not start</h2><p>{safe_error}</p>"
                        r"<p>Check %LOCALAPPDATA%\ClipFinder\data\logs\clipfinder.log and "
                        r"%LOCALAPPDATA%\ClipFinder\setup-status.txt.</p></body></html>"
                    )

            webview.start(start_backend, icon=str(bundled_asset_path("assets/clipfinder.ico")))
        except Exception as exc:
            show_error("ClipFinder desktop window", str(exc))
            stop_server(server_state["server"], server_state["thread"])
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    if "--packaged-smoke-check" in sys.argv[1:]:
        raise SystemExit(run_packaged_smoke_check())
    if "--gpu-runtime-probe" in sys.argv[1:]:
        try:
            probe_index = sys.argv.index("--gpu-runtime-probe")
            raise SystemExit(run_gpu_runtime_probe(sys.argv[probe_index + 1], sys.argv[probe_index + 2]))
        except (ValueError, IndexError):
            raise SystemExit(2)
    run()
