"""Optional hybrid retrieval: embed every chunk of every built index with a REAL sentence-transformers model.
    pip install -r requirements-optional.txt
    python -m scripts.build_embeddings
No fake fallback: if the model cannot load this fails loudly (hybrid retrieval is then simply unavailable)."""
import json
import sys

from app.bm25 import chunks_digest
from app.embeddings import EmbeddingsUnavailable, get_embedder, save_vectors
from scripts.build_index import INDEX


def main() -> int:
    dirs = sorted(d for d in INDEX.glob("*") if (d / "chunks.jsonl").is_file())
    if not dirs:
        print("No indexes found. Run: python -m scripts.build_index")
        return 1
    try:
        emb = get_embedder()
    except EmbeddingsUnavailable as e:
        print("EMBEDDINGS UNAVAILABLE:", e)
        return 1
    for d in dirs:
        chunks = [json.loads(l) for l in (d / "chunks.jsonl").open(encoding="utf-8") if l.strip()]
        vecs = emb.encode([c["text"] for c in chunks])
        save_vectors(d, vecs, emb.name, chunks_digest(chunks))
        print(f"{d.name}: {len(chunks)} chunks embedded with {emb.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
