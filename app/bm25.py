"""Small, deterministic BM25 (Okapi) index. No dependencies.

Indexed text for a chunk = section title + chunk text, so a heading such as
"Scaled Dot-Product Attention" helps retrieval without being repeated in every
chunk body. Tokenisation: lowercase alphanumerics, tiny stop-word list, and a
light plural strip ("heads" -> "head"). No other stemming.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

STOP = frozenset(
    "a an the of and or to in on for with is are was were be been by as at from that this "
    "these those it its we our can which what how why when where who do does did not no "
    "than then so such into over using use used via per".split())


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP]


def chunk_index_text(chunk: dict) -> str:
    return f"{chunk['section']} {chunk['text']}"


class BM25Index:
    def __init__(self, doc_ids: list[str], doc_len: list[int],
                 postings: dict[str, list[list[int]]], k1: float = 1.5, b: float = 0.75,
                 chunks_sha256: str = ""):
        self.doc_ids, self.doc_len, self.postings = doc_ids, doc_len, postings
        self.k1, self.b, self.chunks_sha256 = k1, b, chunks_sha256
        self.n = len(doc_ids)
        self.avgdl = sum(doc_len) / self.n if self.n else 0.0

    @classmethod
    def build(cls, chunks: list[dict], k1: float = 1.5, b: float = 0.75) -> "BM25Index":
        postings: dict[str, list[list[int]]] = {}
        lens = []
        for di, ch in enumerate(chunks):
            toks = tokenize(chunk_index_text(ch))
            lens.append(len(toks))
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                postings.setdefault(t, []).append([di, c])
        return cls([c["chunk_id"] for c in chunks], lens, postings, k1, b, chunks_digest(chunks))

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        scores: dict[int, float] = {}
        for t in set(tokenize(query)):
            plist = self.postings.get(t)
            if not plist:
                continue
            df = len(plist)
            idf = math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)
            for di, tf in plist:
                norm = self.k1 * (1 - self.b + self.b * self.doc_len[di] / self.avgdl)
                scores[di] = scores.get(di, 0.0) + idf * tf * (self.k1 + 1) / (tf + norm)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [(self.doc_ids[di], round(s, 6)) for di, s in ranked]

    def to_json(self) -> dict:
        return {"schema": 1, "k1": self.k1, "b": self.b, "doc_ids": self.doc_ids,
                "doc_len": self.doc_len, "postings": self.postings,
                "chunks_sha256": self.chunks_sha256}

    @classmethod
    def from_json(cls, d: dict) -> "BM25Index":
        return cls(d["doc_ids"], d["doc_len"], d["postings"], d["k1"], d["b"],
                   d.get("chunks_sha256", ""))

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def chunks_digest(chunks: list[dict]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c["chunk_id"].encode())
        h.update(c["text_sha1"].encode())
    return h.hexdigest()
