"""Optional, user-triggered update checks against public GitHub releases."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from app.config import settings
from app.version import __version__


def _version_parts(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(int(part) for part in match.groups()) if match else (0, 0, 0)


def update_status() -> dict:
    """Return release metadata without downloading or executing anything."""
    repository = settings.update_repository.strip().strip("/")
    base = {"current_version": __version__, "repository": repository, "update_available": False}
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        return {**base, "error": "Update repository is not configured."}
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ClipFinder-Local"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = "No public GitHub release is available yet." if exc.code == 404 else f"GitHub returned HTTP {exc.code}."
        return {**base, "error": message}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {**base, "error": f"Could not check for updates: {exc}"}

    latest = str(release.get("tag_name") or release.get("name") or "").lstrip("v")
    installer = next(
        (asset for asset in release.get("assets", []) if str(asset.get("name", "")).lower().endswith(".exe") and "clipfinder-setup" in str(asset.get("name", "")).lower()),
        None,
    )
    if not latest or not installer:
        return {**base, "error": "The latest release does not contain a ClipFinder setup executable."}
    return {
        **base,
        "latest_version": latest,
        "update_available": _version_parts(latest) > _version_parts(__version__),
        "release_name": str(release.get("name") or f"ClipFinder {latest}").strip(),
        # GitHub release descriptions are shown as plain text in the local UI,
        # never injected as HTML.
        "release_notes": str(release.get("body") or "").strip(),
        "download_url": installer.get("browser_download_url"),
        "release_url": release.get("html_url"),
        "published_at": release.get("published_at"),
        "asset_size": int(installer.get("size") or 0),
    }
