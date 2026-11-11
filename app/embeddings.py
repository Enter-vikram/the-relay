"""Optional dense retrieval. Real embeddings or nothing: there is NO fake fallback.

If sentence-transformers / the model is unavailable, `get_embedder()` raises
EmbeddingsUnavailable and every caller reports hybrid retrieval as "not
measured". Lexical retrieval is never relabelled as embedding retrieval.

Default model: sentence-transformers/all-MiniLM-L6-v2 (Apache-2.0, 384 dims,
~90 MB). Install: pip install -r requirements-optional.txt
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import numpy as np

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingsUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    name: str

    def encode(self, texts: list[str]) -> np.ndarray: ...   # L2-normalised rows


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:                                  # ImportError or torch problems
            raise EmbeddingsUnavailable(
                "sentence-transformers is not installed. Run: pip install -r requirements-optional.txt") from e
        try:
            self._m = SentenceTransformer(model_name)
        except Exception as e:
            raise EmbeddingsUnavailable(f"could not load {model_name}: {e}") from e
        self.name = model_name

    def encode(self, texts: list[str]) -> np.ndarray:
        v = self._m.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(v, dtype=np.float32)


def get_embedder(model_name: str = DEFAULT_MODEL) -> Embedder:
    return SentenceTransformerEmbedder(model_name)


def save_vectors(dirpath: Path, vectors: np.ndarray, model: str, chunks_sha256: str) -> None:
    np.save(dirpath / "embeddings.npy", vectors)
    (dirpath / "embeddings.json").write_text(json.dumps(
        {"model": model, "dim": int(vectors.shape[1]), "n": int(vectors.shape[0]),
         "chunks_sha256": chunks_sha256}, indent=2) + "\n", encoding="utf-8")


def load_vectors(dirpath: Path, chunks_sha256: str) -> tuple[np.ndarray, dict]:
    meta_p, vec_p = dirpath / "embeddings.json", dirpath / "embeddings.npy"
    if not (meta_p.is_file() and vec_p.is_file()):
        raise EmbeddingsUnavailable(f"no embeddings in {dirpath}. Run: python -m scripts.build_embeddings")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if meta["chunks_sha256"] != chunks_sha256:
        raise EmbeddingsUnavailable("embeddings are stale (chunks changed). Rebuild them.")
    return np.load(vec_p), meta
