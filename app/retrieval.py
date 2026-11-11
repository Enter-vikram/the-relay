"""Retrieval engine: BM25 (+ optional dense hybrid, + lexical re-ranker), dedupe,
context-budget fitting and the evidence-sufficiency check.

Honest naming: "rerank" here is a *lexical* re-ranker (idf-weighted term coverage
+ term proximity + section-title match blended with the BM25 score). It is not a
cross-encoder. A neural reranker would need a model download; it can be added
behind the same interface if the benchmark shows lexical re-ranking is not enough.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.bm25 import BM25Index, chunks_digest, tokenize
from app.embeddings import Embedder, EmbeddingsUnavailable, load_vectors
from app.question import query_terms
from app.tokens import OVERHEAD_TOKENS, estimate_tokens


@dataclass(frozen=True)
class Hit:
    chunk: dict
    score: float
    rank: int
    method: str

    @property
    def chunk_id(self) -> str:
        return self.chunk["chunk_id"]


@dataclass(frozen=True)
class SufficiencyConfig:
    # PLACEHOLDER thresholds: not calibrated on real paper questions yet.
    # `experiments/retrieval_benchmark.py --sweep` prints the trade-off on the
    # real benchmark so these can be chosen from evidence, then set here.
    min_coverage: float = 0.55        # idf-weighted share of question terms found in top-m hits
    min_top_score: float = 2.0        # BM25 score of the best hit
    top_m: int = 3


@dataclass
class Sufficiency:
    sufficient: bool
    coverage: float
    top_score: float
    missing_terms: list[str]
    reason: str


class Library:
    """Chunks + BM25 (+ vectors) for one chunking id."""

    def __init__(self, chunks: list[dict], bm25: BM25Index, vectors: np.ndarray | None = None,
                 embed_model: str = ""):
        self.chunks, self.bm25 = chunks, bm25
        self.by_id = {c["chunk_id"]: c for c in chunks}
        self.index_of = {c["chunk_id"]: i for i, c in enumerate(chunks)}
        self.vectors, self.embed_model = vectors, embed_model
        self._tok = {c["chunk_id"]: set(tokenize(c["section"] + " " + c["text"])) for c in chunks}

    @classmethod
    def from_chunks(cls, chunks: list[dict]) -> "Library":
        return cls(chunks, BM25Index.build(chunks))

    @classmethod
    def load(cls, index_root: Path, chunking_id: str, with_vectors: bool = False) -> "Library":
        d = Path(index_root) / chunking_id
        if not (d / "chunks.jsonl").is_file():
            raise FileNotFoundError(f"{d}/chunks.jsonl missing. Run: python -m scripts.build_index")
        chunks = [json.loads(l) for l in (d / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
        bm = BM25Index.load(d / "bm25.json")
        if bm.chunks_sha256 != chunks_digest(chunks):
            raise ValueError(f"{d}: bm25.json does not match chunks.jsonl (stale index). Rebuild.")
        vecs, model = None, ""
        if with_vectors:
            vecs, meta = load_vectors(d, bm.chunks_sha256)
            model = meta["model"]
        return cls(chunks, bm, vecs, model)

    def idf(self, term: str) -> float:
        df = len(self.bm25.postings.get(term, ()))
        return math.log((self.bm25.n - df + 0.5) / (df + 0.5) + 1.0)

    def tokens_of(self, chunk_id: str) -> set[str]:
        return self._tok[chunk_id]


def _min_window(positions: list[list[int]]) -> int | None:
    """Smallest token window containing at least one occurrence of every list that is non-empty."""
    lists = [p for p in positions if p]
    if not lists:
        return None
    merged = sorted((pos, i) for i, ps in enumerate(lists) for pos in ps)
    need, have, best, left = len(lists), {}, None, 0
    for right, (pos, i) in enumerate(merged):
        have[i] = have.get(i, 0) + 1
        while len(have) == need:
            w = pos - merged[left][0] + 1
            best = w if best is None or w < best else best
            li = merged[left][1]
            have[li] -= 1
            if not have[li]:
                del have[li]
            left += 1
    return best


class Retriever:
    def __init__(self, lib: Library, embedder: Embedder | None = None):
        self.lib, self.embedder = lib, embedder

    # ---------------- candidate generation ----------------
    def _bm25(self, query: str, n: int) -> list[tuple[str, float]]:
        return self.lib.bm25.search(query, n)

    def _dense(self, query: str, n: int) -> list[tuple[str, float]]:
        if self.embedder is None or self.lib.vectors is None:
            raise EmbeddingsUnavailable("hybrid retrieval needs an embedder and stored vectors")
        if self.embedder.name != self.lib.embed_model:
            raise EmbeddingsUnavailable(f"embedder {self.embedder.name} != index model {self.lib.embed_model}")
        q = self.embedder.encode([query])[0]
        sims = self.lib.vectors @ q
        order = np.argsort(-sims, kind="stable")[:n]
        return [(self.lib.chunks[i]["chunk_id"], float(sims[i])) for i in order]

    # ---------------- re-ranking ----------------
    def _rerank(self, query: str, cands: list[tuple[str, float]]) -> list[tuple[str, float]]:
        terms = query_terms(query)
        if not terms or not cands:
            return cands
        idfs = {t: self.lib.idf(t) for t in terms}
        total_idf = sum(idfs.values()) or 1.0
        top = max(s for _, s in cands) or 1.0
        out = []
        for cid, s in cands:
            c = self.lib.by_id[cid]
            toks = tokenize(c["text"])
            pos = {t: [i for i, x in enumerate(toks) if x == t] for t in terms}
            cov = sum(idfs[t] for t in terms if pos[t]) / total_idf
            found = [t for t in terms if pos[t]]
            win = _min_window([pos[t] for t in found])
            prox = (len(found) / win) if win else 0.0         # 1.0 = all found terms adjacent
            sec = set(tokenize(c["section"]))
            sec_hit = sum(idfs[t] for t in terms if t in sec) / total_idf
            out.append((cid, 0.5 * (s / top) + 0.3 * cov + 0.15 * min(prox, 1.0) + 0.05 * sec_hit))
        return sorted(out, key=lambda kv: (-kv[1], kv[0]))

    # ---------------- dedupe ----------------
    def _dedupe(self, ids: list[str]) -> list[str]:
        kept: list[str] = []
        for cid in ids:
            t = self.lib.tokens_of(cid)
            c = self.lib.by_id[cid]
            dup = any(self.lib.by_id[k]["paper"] == c["paper"] and t and
                      len(t & self.lib.tokens_of(k)) / len(t | self.lib.tokens_of(k)) >= 0.8 for k in kept)
            if not dup:
                kept.append(cid)
        return kept

    # ---------------- public ----------------
    def search(self, query: str, k: int, *, rerank: bool = False, mode: str = "bm25",
               per_paper: int | None = None, exclude: frozenset[str] = frozenset()) -> list[Hit]:
        """mode: 'bm25' or 'hybrid' (RRF of BM25 + dense). per_paper: guarantee that many hits
        from every paper (used for comparison questions)."""
        pool = max(4 * k, 20) if rerank else max(2 * k, k + 4)
        if per_paper:
            pool = self.lib.bm25.n
        lex = self._bm25(query, pool)
        if mode == "hybrid":
            dense = self._dense(query, pool)
            fused: dict[str, float] = {}
            for lst in (lex, dense):
                for r, (cid, _) in enumerate(lst):
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + r)
            cands = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
            method = "hybrid"
        elif mode == "bm25":
            cands, method = lex, "bm25"
        else:
            raise ValueError(f"unknown mode {mode!r}")
        cands = [(c, s) for c, s in cands if c not in exclude]
        if rerank:
            cands = self._rerank(query, cands[:max(4 * k, 20)])
            method += "+rerank"
        if per_paper:
            papers = sorted({c["paper"] for c in self.lib.chunks})
            chosen: list[tuple[str, float]] = []
            for p in papers:
                chosen += [(c, s) for c, s in cands if self.lib.by_id[c]["paper"] == p][:per_paper]
            cands = sorted(chosen, key=lambda kv: (-kv[1], kv[0]))
        ids = self._dedupe([c for c, _ in cands])
        score = dict(cands)
        ids = ids[:max(k, per_paper * 3 if per_paper else k)]
        return [Hit(self.lib.by_id[i], round(score[i], 6), r + 1, method) for r, i in enumerate(ids)]

    # ---------------- sufficiency ----------------
    def assess(self, query: str, hits: list[Hit], cfg: SufficiencyConfig = SufficiencyConfig()) -> Sufficiency:
        terms = query_terms(query)
        if not hits or not terms:
            return Sufficiency(False, 0.0, 0.0, terms, "no_hits" if not hits else "no_terms")
        cover: set[str] = set()
        for h in hits[:cfg.top_m]:
            cover |= self.lib.tokens_of(h.chunk_id)
        idfs = {t: self.lib.idf(t) for t in terms}
        total = sum(idfs.values()) or 1.0
        cov = sum(w for t, w in idfs.items() if t in cover) / total
        bm_top = self.lib.bm25.search(query, 1)
        top = bm_top[0][1] if bm_top else 0.0
        missing = [t for t in terms if t not in cover]
        if cov < cfg.min_coverage:
            return Sufficiency(False, round(cov, 3), top, missing, "low_term_coverage")
        if top < cfg.min_top_score:
            return Sufficiency(False, round(cov, 3), top, missing, "low_top_score")
        return Sufficiency(True, round(cov, 3), top, missing, "ok")


# ---------------- budget policy ----------------
def fit_to_budget(hits: list[Hit], max_context_tokens: int, min_chunks: int = 2) -> tuple[list[Hit], bool]:
    """Keep the best hits whose estimated tokens fit. Drops from the bottom (least relevant),
    never below `min_chunks`. Returns (hits, trimmed?)."""
    kept = list(hits)
    def cost(hs): return sum(estimate_tokens(len(h.chunk["text"])) - OVERHEAD_TOKENS for h in hs)
    trimmed = False
    while len(kept) > min_chunks and cost(kept) > max_context_tokens:
        kept.pop()
        trimmed = True
    return kept, trimmed
