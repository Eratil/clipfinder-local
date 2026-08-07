"""Release preflight, build provenance and frozen-folder validation.

This module intentionally uses only the standard library so it can validate a
fresh build environment before importing any of ClipFinder's native packages.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.metadata
import json
import platform
import re
import struct
import sys
from pathlib import Path


EXACT_PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;]+)")
FORBIDDEN_BASE_DLLS = (
    "c10_cuda.dll",
    "torch_cuda.dll",
    "caffe2_nvrtc.dll",
    "cublas*.dll",
    "cudart*.dll",
    "cudnn*.dll",
    "cufft*.dll",
    "cupti*.dll",
    "curand*.dll",
    "cusolver*.dll",
    "cusparse*.dll",
    "nvrtc*.dll",
    "nvjitlink*.dll",
    "nvtoolsext*.dll",
)
REQUIRED_SOURCE_FILES = (
    "ClipFinder.spec",
    "ClipFinderUpdateHelper.spec",
    "assets/clipfinder.ico",
    "assets/close-pop.wav",
    "app/static/index.html",
    "installer/runtime-compatibility.json",
)


def canonical_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def exact_requirements(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    """Read exact pins, following local ``-r`` and ``-c`` includes."""
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return {}
    seen.add(path)
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
            include = line.split(maxsplit=1)[1].strip()
            pins.update(exact_requirements(path.parent / include, seen))
            continue
        match = EXACT_PIN.match(line)
        if match:
            name = canonical_distribution(match.group(1))
            version = match.group(2)
            existing = pins.get(name)
            if existing and existing != version:
                raise ValueError(f"Conflicting pins for {name}: {existing} and {version}")
            pins[name] = version
            continue
        if not line.startswith("-"):
            raise ValueError(f"Requirement is not pinned exactly in {path.name}: {line}")
    return pins


def compatibility_problems(value: object, pins: dict[str, str]) -> list[str]:
    if not isinstance(value, dict):
        return ["runtime-compatibility.json must contain a JSON object."]
    problems: list[str] = []
    if value.get("schema") != 1 or not value.get("contract_id"):
        problems.append("runtime-compatibility.json has an unsupported schema or no contract_id.")
    if value.get("architecture") != "x64":
        problems.append("runtime-compatibility.json must target x64.")
    if str(value.get("ctranslate2") or "") != pins.get("ctranslate2"):
        problems.append("runtime-compatibility.json does not match the pinned CTranslate2 version.")
    for component in ("cuda", "cudnn"):
        section = value.get(component)
        if not isinstance(section, dict) or not section.get("required_dlls") or len(set(section.get("required_dlls", []))) != len(section.get("required_dlls", [])):
            problems.append(f"runtime-compatibility.json has invalid {component} required_dlls.")
    models = value.get("models")
    for name in ("transcription_default", "transcription_fast", "similarity"):
        model = models.get(name) if isinstance(models, dict) else None
        if not isinstance(model, dict) or not model.get("id") or not re.fullmatch(r"[0-9a-f]{40}", str(model.get("revision") or "")):
            problems.append(f"Pinned model definition is invalid: {name}.")
    return problems


def installed_versions(names: set[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def source_app_version(project_root: Path) -> str:
    text = (project_root / "app" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise ValueError("Could not read app/version.py")
    return match.group(1)


def preflight_problems(
    project_root: Path,
    expected_version: str,
    *,
    versions: dict[str, str | None] | None = None,
    python_version: tuple[int, int] | None = None,
    pointer_bits: int | None = None,
) -> list[str]:
    try:
        pins = exact_requirements(project_root / "requirements-dev.txt")
    except (OSError, ValueError) as exc:
        return [str(exc)]
    stale_names = {"opencv-python", "torchaudio", "torchvision", "easyocr"}
    versions = versions if versions is not None else installed_versions(set(pins) | stale_names)
    problems: list[str] = []
    actual_python = python_version or (sys.version_info.major, sys.version_info.minor)
    actual_bits = pointer_bits or struct.calcsize("P") * 8
    if actual_python != (3, 11):
        problems.append(f"Release Python must be 3.11, got {actual_python[0]}.{actual_python[1]}.")
    if actual_bits != 64:
        problems.append(f"Release Python must be 64-bit, got {actual_bits}-bit.")
    actual_version = source_app_version(project_root)
    if actual_version != expected_version:
        problems.append(f"app/version.py is {actual_version}, expected {expected_version}.")
    for relative in REQUIRED_SOURCE_FILES:
        if not (project_root / relative).is_file():
            problems.append(f"Required release input is missing: {relative}")
    compatibility_path = project_root / "installer" / "runtime-compatibility.json"
    try:
        compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"Could not read runtime-compatibility.json: {exc}")
    else:
        problems.extend(compatibility_problems(compatibility, pins))
    for name, expected in sorted(pins.items()):
        actual = versions.get(name)
        if actual != expected:
            problems.append(f"{name} must be {expected}, got {actual or 'not installed'}.")
    for stale in sorted(stale_names):
        if versions.get(stale) is not None:
            problems.append(f"Stale/conflicting package must not be installed in the build environment: {stale}")
    return problems


def write_build_info(project_root: Path, dist_root: Path, version: str, git_sha: str) -> Path:
    pins = exact_requirements(project_root / "requirements-dev.txt")
    compatibility_path = project_root / "installer" / "runtime-compatibility.json"
    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
    inputs = (
        "requirements.txt",
        "requirements-dev.txt",
        "constraints-win-py311.txt",
        "ClipFinder.spec",
        "ClipFinderUpdateHelper.spec",
        "installer/runtime-compatibility.json",
    )
    metadata = {
        "schema": 1,
        "app": "ClipFinder",
        "version": version,
        "git_sha": git_sha or "unknown",
        "artifact_profile": "windows-x64-base-cpu",
        "python": platform.python_version(),
        "python_architecture": platform.architecture()[0],
        "gpu_runtime_contract": compatibility["contract_id"],
        "models": compatibility.get("models", {}),
        "dependencies": {name: importlib.metadata.version(name) for name in sorted(pins)},
        "input_sha256": {name: sha256(project_root / name) for name in inputs},
    }
    output = dist_root / "build-info.json"
    output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def distribution_problems(dist_root: Path) -> list[str]:
    problems: list[str] = []
    required = (
        "ClipFinder.exe",
        "ClipFinderUpdateHelper.exe",
        "build-info.json",
        "_internal/app/static/index.html",
        "_internal/assets/clipfinder.ico",
        "_internal/assets/close-pop.wav",
        "_internal/assets/runtime-compatibility.json",
        "Configure-ClipFinder.ps1",
        "TESTER-INSTALLATION.md",
    )
    for relative in required:
        if not (dist_root / relative).is_file():
            problems.append(f"Packaged file is missing: {relative}")
    all_files = [item for item in dist_root.rglob("*") if item.is_file()]
    for pattern in FORBIDDEN_BASE_DLLS:
        matches = [item for item in all_files if fnmatch.fnmatch(item.name.lower(), pattern.lower())]
        if matches:
            problems.append(f"CPU base package contains forbidden GPU runtime file: {matches[0].relative_to(dist_root)}")
    opencv_metadata = [item.name.lower() for item in dist_root.rglob("*.dist-info") if item.is_dir() and item.name.lower().startswith("opencv_python")]
    if any("headless" not in name for name in opencv_metadata):
        problems.append("CPU package contains the non-headless OpenCV distribution.")
    try:
        build_info = json.loads((dist_root / "build-info.json").read_text(encoding="utf-8"))
        compatibility = json.loads((dist_root / "_internal/assets/runtime-compatibility.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"Packaged release metadata is invalid: {exc}")
    else:
        if not isinstance(build_info, dict) or build_info.get("schema") != 1:
            problems.append("Packaged build-info.json has an unsupported format.")
        elif not isinstance(compatibility, dict) or build_info.get("gpu_runtime_contract") != compatibility.get("contract_id"):
            problems.append("Packaged build metadata and GPU runtime contract do not match.")
    return problems


def _report(problems: list[str]) -> int:
    if not problems:
        print("Release verification passed.")
        return 0
    for problem in problems:
        print(f"[error] {problem}", file=sys.stderr)
    return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--project-root", type=Path, default=Path.cwd())
    preflight.add_argument("--version", required=True)
    info = commands.add_parser("write-build-info")
    info.add_argument("--project-root", type=Path, default=Path.cwd())
    info.add_argument("--dist", type=Path, required=True)
    info.add_argument("--version", required=True)
    info.add_argument("--git-sha", default="")
    verify = commands.add_parser("verify-dist")
    verify.add_argument("--dist", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "preflight":
        return _report(preflight_problems(args.project_root.resolve(), args.version))
    if args.command == "write-build-info":
        output = write_build_info(args.project_root.resolve(), args.dist.resolve(), args.version, args.git_sha)
        print(f"Build metadata created: {output}")
        return 0
    return _report(distribution_problems(args.dist.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
