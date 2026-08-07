import threading
from importlib.metadata import PackageNotFoundError, version

import numpy as np

from app.services import diagnostics
from app.services.model_catalog import model_identity

_model = None
_model_device: str | None = None
_model_lock = threading.RLock()
EMBEDDING_MODEL_NAME, EMBEDDING_MODEL_REVISION = model_identity("similarity")


def current_device() -> str | None:
    """The device of the already loaded similarity model, if any."""
    return _model_device


def installation_supports_cuda() -> bool:
    """Check wheel capability without importing the large Torch runtime."""
    try:
        return "+cpu" not in version("torch").lower()
    except PackageNotFoundError:
        return False


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Lazy-load a multilingual model only after a completed transcription."""
    global _model, _model_device

    # SentenceTransformer initialization and encode are not guaranteed to be
    # thread-safe, especially when CUDA is involved. Keep both operations in
    # one critical section so concurrent API/background requests cannot load
    # the model twice or use the same model instance at the same time.
    with _model_lock:
        from sentence_transformers import SentenceTransformer
        if _model is None and installation_supports_cuda():
            try:
                _model = SentenceTransformer(EMBEDDING_MODEL_NAME, revision=EMBEDDING_MODEL_REVISION, device="cuda")
                _model_device = "cuda"
            except Exception as exc:
                diagnostics.log_failure("CUDA similarity model initialization failed", exc)
                _model = SentenceTransformer(EMBEDDING_MODEL_NAME, revision=EMBEDDING_MODEL_REVISION, device="cpu")
                _model_device = "cpu"
        elif _model is None:
            _model = SentenceTransformer(EMBEDDING_MODEL_NAME, revision=EMBEDDING_MODEL_REVISION, device="cpu")
            _model_device = "cpu"
        try:
            vectors = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        except Exception as exc:
            if _model_device != "cuda":
                raise
            diagnostics.log_failure("CUDA similarity encoding failed", exc)
            # Keep searching usable even if the CUDA sentence-embedding runtime is unavailable.
            _model = SentenceTransformer(EMBEDDING_MODEL_NAME, revision=EMBEDDING_MODEL_REVISION, device="cpu")
            _model_device = "cpu"
            vectors = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [vector.astype(float).tolist() for vector in vectors]


def cosine(left: list[float], right: list[float]) -> float:
    a, b = np.asarray(left), np.asarray(right)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator else 0.0
