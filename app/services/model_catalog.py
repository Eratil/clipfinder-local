"""Pinned model identities shared by analysis, caching and build diagnostics."""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def runtime_compatibility() -> dict:
    if getattr(sys, "frozen", False):
        path = Path(getattr(sys, "_MEIPASS")) / "assets" / "runtime-compatibility.json"
    else:
        path = Path(__file__).resolve().parents[2] / "installer" / "runtime-compatibility.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != 1 or not value.get("contract_id") or not isinstance(value.get("models"), dict):
        raise RuntimeError("runtime-compatibility.json has an unsupported format")
    return value


def model_identity(kind: str) -> tuple[str, str]:
    value = runtime_compatibility()["models"].get(kind)
    if not isinstance(value, dict) or not value.get("id") or not value.get("revision"):
        raise RuntimeError(f"Pinned model definition is missing: {kind}")
    return str(value["id"]), str(value["revision"])


def whisper_identity(model_name: str) -> tuple[str, str | None]:
    normalized = model_name.lower().strip()
    if normalized in {"large-v3", "systran/faster-whisper-large-v3"}:
        return model_identity("transcription_default")
    if normalized in {"small", "systran/faster-whisper-small"}:
        return model_identity("transcription_fast")
    # Advanced/custom model paths remain supported, but cannot claim the same
    # reproducibility as the two application-managed models.
    return model_name, None


def whisper_model_source(model_name: str) -> str:
    model_id, revision = whisper_identity(model_name)
    if revision is None or Path(model_id).is_dir():
        return model_id
    from huggingface_hub import snapshot_download

    return snapshot_download(
        repo_id=model_id,
        revision=revision,
        allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"],
    )
