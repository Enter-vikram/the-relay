"""SYNTHETIC fixture library. Invented nonsense text; NOT the real papers.

Paper keys reuse the real keys only so metadata plumbing is exercised. Nothing
here says anything true or false about the real papers, and no test result built
on it is a retrieval-quality measurement.
"""
from app.chunking import chunk_units
from app.retrieval import Library, Retriever

SYNTHETIC = True
PAPERS = {
    "attention": {"key": "attention", "title": "Synthetic Paper One", "arxiv_id": "0000.00001", "version": 1},
    "moshi": {"key": "moshi", "title": "Synthetic Paper Two", "arxiv_id": "0000.00002", "version": 1},
    "dynamic-chunking": {"key": "dynamic-chunking", "title": "Synthetic Paper Three", "arxiv_id": "0000.00003", "version": 1},
}
TEXT = {
    "attention": [
        (1, "1 Zorblax Layers", "The zorblax layer applies quasi-widget folding to every token. "
                                "It uses 7 folding heads and a latent width of 512. Each fold is normalised by a gimbal term."),
        (2, "2 Training Setup", "Training of the zorblax model used a plumbus optimiser for 90000 steps. "
                                "The warmup schedule ramps the learning rate linearly. Dropout of 0.15 was applied."),
        (3, "3 Results", "The zorblax model reached a score of 28.4 on the synthetic benchmark. "
                         "Ablations show that removing the gimbal term lowers the score by 1.2 points."),
    ],
    "moshi": [
        (1, "1 Blorpt Codec", "The blorpt codec compresses audio into 8 coarse streams at a frame rate of 12.5 hertz. "
                              "Quantisation of the streams uses residual snorkel codebooks."),
        (2, "2 Dialogue Stack", "The dialogue stack keeps two parallel channels so the model can talk while listening. "
                                "Latency of the stack is measured in milliseconds on a synthetic device."),
        (3, "3 Evaluation", "Listeners rated the blorpt dialogue stack at 4.1 on a five point scale in the synthetic study."),
    ],
    "dynamic-chunking": [
        (1, "1 Wibble Routing", "Wibble routing predicts boundaries between chunks from cosine similarity of neighbouring vectors. "
                                "A smoothing step keeps the boundaries differentiable."),
        (2, "2 Hierarchy", "The hierarchy stacks 3 stages, each compressing the sequence by a learned ratio. "
                           "Higher stages see fewer but richer chunks."),
        (3, "3 Results", "The synthetic hierarchy matched a baseline using 2.0 times fewer chunks on the synthetic corpus."),
    ],
}


def units(paper: str) -> list[dict]:
    out = []
    for page, section, body in TEXT[paper]:
        out.append({"paper": paper, "page": page, "section": section, "kind": "heading", "text": section})
        out.append({"paper": paper, "page": page, "section": section, "kind": "para", "text": body})
    return out


def chunks(chunk_tokens=60, overlap=10) -> list[dict]:
    allc = []
    for p, meta in PAPERS.items():
        allc += chunk_units(units(p), meta, chunk_tokens, overlap, f"syn{chunk_tokens}")
    return allc


def retriever() -> Retriever:
    return Retriever(Library.from_chunks(chunks()))
