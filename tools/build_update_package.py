"""Create verified ClipFinder release manifests and file-level update patches.

The normal setup EXE remains the installation fallback.  This tool produces a
small ZIP only when it can compare the new PyInstaller folder with the exact
previous release folder saved in ``release-cache``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


PATCH_MANIFEST_PATH = "__clipfinder_patch__/manifest.json"
PATCH_FILES_PREFIX = "files/"


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe relative path: {value!r}")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_index(root: Path) -> dict[str, dict]:
    if not root.is_dir():
        raise ValueError(f"Release folder does not exist: {root}")
    files: dict[str, dict] = {}
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix()
        safe_relative(relative)
        files[relative] = {"path": relative, "sha256": sha256(item), "size": item.stat().st_size}
    if not files:
        raise ValueError(f"Release folder is empty: {root}")
    return files


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_release_manifest(source: Path, version: str, output: Path) -> Path:
    manifest = {
        "schema": 1,
        "app": "ClipFinder",
        "version": version,
        "files": list(file_index(source).values()),
    }
    write_json(output, manifest)
    return output


def cache_release(source: Path, version: str, output: Path) -> Path:
    """Store the just-built application folder compactly for the next patch."""
    index = file_index(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in index:
            archive.write(source / relative, relative)
    return output


def _extract_cache(archive_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = safe_relative(member.filename)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    return destination


def build_patch(from_archive: Path, from_version: str, to_directory: Path, to_version: str, output_dir: Path) -> tuple[Path, Path]:
    """Build a patch ZIP and a complete target manifest for one exact upgrade."""
    if not from_archive.is_file():
        raise ValueError(f"Previous release cache is missing: {from_archive}")
    target = file_index(to_directory)
    with tempfile.TemporaryDirectory(prefix="clipfinder-patch-") as temporary:
        previous_root = _extract_cache(from_archive, Path(temporary) / "previous")
        previous = file_index(previous_root)
        changed = []
        for relative, metadata in target.items():
            before = previous.get(relative)
            if before and before["sha256"] == metadata["sha256"]:
                continue
            changed.append({**metadata, "previous_sha256": before["sha256"] if before else None})
        removed = [
            {"path": relative, "previous_sha256": metadata["sha256"]}
            for relative, metadata in previous.items()
            if relative not in target
        ]

        output_dir.mkdir(parents=True, exist_ok=True)
        patch_path = output_dir / f"ClipFinder-patch-{from_version}-to-{to_version}.zip"
        target_manifest_path = output_dir / f"ClipFinder-manifest-{to_version}.json"
        patch_manifest = {
            "schema": 1,
            "app": "ClipFinder",
            "from_version": from_version,
            "to_version": to_version,
            "files": changed,
            "remove": removed,
        }
        with zipfile.ZipFile(patch_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(PATCH_MANIFEST_PATH, json.dumps(patch_manifest, ensure_ascii=False, indent=2) + "\n")
            for item in changed:
                relative = safe_relative(item["path"])
                archive.write(to_directory.joinpath(*relative.parts), PATCH_FILES_PREFIX + relative.as_posix())
        write_json(target_manifest_path, {"schema": 1, "app": "ClipFinder", "version": to_version, "files": list(target.values())})
    return patch_path, target_manifest_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    cache = commands.add_parser("cache", help="Archive a release folder for the next patch")
    cache.add_argument("--source", required=True, type=Path)
    cache.add_argument("--version", required=True)
    cache.add_argument("--output", required=True, type=Path)
    manifest = commands.add_parser("manifest", help="Write the complete manifest for a release")
    manifest.add_argument("--source", required=True, type=Path)
    manifest.add_argument("--version", required=True)
    manifest.add_argument("--output", required=True, type=Path)
    patch = commands.add_parser("patch", help="Create a patch from a cached previous release")
    patch.add_argument("--from-archive", required=True, type=Path)
    patch.add_argument("--from-version", required=True)
    patch.add_argument("--to-directory", required=True, type=Path)
    patch.add_argument("--to-version", required=True)
    patch.add_argument("--output-dir", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "cache":
        output = cache_release(args.source, args.version, args.output)
        print(f"Release cache created: {output} ({output.stat().st_size} bytes)")
    elif args.command == "manifest":
        output = write_release_manifest(args.source, args.version, args.output)
        print(f"Release manifest created: {output}")
    else:
        patch, manifest = build_patch(args.from_archive, args.from_version, args.to_directory, args.to_version, args.output_dir)
        print(f"Update patch created: {patch} ({patch.stat().st_size} bytes)")
        print(f"Release manifest created: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
