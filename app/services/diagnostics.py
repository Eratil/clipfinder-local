"""Privacy-conscious local diagnostics for support and bug reports."""
from __future__ import annotations

import logging
import os
import platform
import re
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.config import settings


LOGGER_NAME = "clipfinder"
_configured = False


def log_path() -> Path:
    return settings.clipfinder_data_dir / "logs" / "clipfinder.log"


def logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure() -> logging.Logger:
    """Write a small rotating log without collecting user media or transcripts."""
    global _configured
    result = logger()
    if _configured:
        return result
    result.setLevel(logging.INFO)
    result.propagate = False
    destination = log_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(destination, maxBytes=1_000_000, backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    result.addHandler(handler)
    _configured = True
    return result


def safe_error(error: BaseException | str) -> str:
    """Keep useful error text while removing URLs and local file paths from reports."""
    value = redact(str(error))
    return value[:1_500]


def redact(value: str) -> str:
    value = re.sub(r"https?://\S+", "[url]", value, flags=re.IGNORECASE)
    value = re.sub(r"[A-Za-z]:\\[^\r\n\"']+", "[path]", value)
    return value


def log_failure(event: str, error: BaseException) -> None:
    """Log a redacted traceback so a report cannot disclose source content."""
    trace = redact("".join(traceback.format_exception(type(error), error, error.__traceback__)))[:12_000]
    logger().error("%s error=%s\n%s", event, safe_error(error), trace)


def build_report(runtime: dict[str, Any], version: str) -> str:
    """Return a shareable report with system state and recent operational logs only."""
    try:
        recent = log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-220:]
    except OSError:
        recent = ["No diagnostic log has been created yet."]
    lines = [
        "ClipFinder diagnostic report",
        f"App version: {version}",
        f"Python: {sys.version.split()[0]}",
        f"Windows: {platform.platform()}",
        f"Process architecture: {platform.architecture()[0]}",
        f"Process ID: {os.getpid()}",
        f"Configured transcription: {runtime.get('transcription', {}).get('label', 'unknown')}",
        f"Similarity search: {runtime.get('embeddings', {}).get('label', 'unknown')}",
        f"GPU: {runtime.get('gpu', {}).get('name', 'not detected') if runtime.get('gpu') else 'not detected'}",
        "",
        "Privacy note: this report contains no recording, audio, transcript, prompt, chat content or source URL.",
        "Recent diagnostic events:",
        *recent,
    ]
    return "\n".join(lines) + "\n"
