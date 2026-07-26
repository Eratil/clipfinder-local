import numpy as np

_model = None
_model_device: str | None = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Lazy-load a multilingual model only after a completed transcription."""
    global _model, _model_device
    from sentence_transformers import SentenceTransformer
    if _model is None:
        try:
            _model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", device="cuda")
            _model_device = "cuda"
        except Exception:
            _model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", device="cpu")
            _model_device = "cpu"
    try:
        vectors = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    except Exception:
        if _model_device != "cuda":
            raise
        # Keep searching usable even if the CUDA sentence-embedding runtime is unavailable.
        _model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", device="cpu")
        _model_device = "cpu"
        vectors = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [vector.astype(float).tolist() for vector in vectors]


def cosine(left: list[float], right: list[float]) -> float:
    a, b = np.asarray(left), np.asarray(right)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator else 0.0
