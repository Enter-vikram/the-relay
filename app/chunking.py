"""Units -> retrievable chunks with paper / page / section metadata.

"Tokens" here are cheap regex tokens (words and punctuation), NOT Groq's BPE
tokens; they are only used to size chunks. Real prompt cost is measured later
from the provider's `usage.prompt_tokens`.

Rules: chunks never cross a section boundary; every chunk records every page
it touches; sentences are never split unless one alone exceeds the chunk size.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

TOKEN_RE = re.compile(r"\w+|[^\w\s]")
SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")


def count_tokens(text: str) -> int:
    return len(TOKEN_RE.findall(text))


@dataclass(frozen=True)
class RetrievalSetting:
    name: str
    chunk_tokens: int
    overlap_tokens: int
    top_k: int
    rerank: bool

    @property
    def chunking_id(self) -> str:
        return f"c{self.chunk_tokens}o{self.overlap_tokens}"


# Starting points from the brief; overlap values are my initial choice and are
# one of the things the benchmark will question.
SETTINGS: dict[str, RetrievalSetting] = {
    "A": RetrievalSetting("A", 300, 50, 3, False),
    "B": RetrievalSetting("B", 600, 100, 5, False),
    "C": RetrievalSetting("C", 600, 100, 5, True),
}


def _hard_split(text: str, limit: int) -> list[str]:
    """Split a single over-long sentence on word boundaries."""
    pieces, cur, n = [], [], 0
    for w in text.split():
        k = count_tokens(w)
        if cur and n + k > limit:
            pieces.append(" ".join(cur))
            cur, n = [], 0
        cur.append(w)
        n += k
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def _sentences(units: list[dict], limit: int) -> list[tuple[str, int, int]]:
    out = []
    for u in units:
        for s in SENT_RE.split(u["text"]):
            s = s.strip()
            if not s:
                continue
            n = count_tokens(s)
            if n > limit:
                for piece in _hard_split(s, limit):
                    out.append((piece, u["page"], count_tokens(piece)))
            else:
                out.append((s, u["page"], n))
    return out


def chunk_units(units: list[dict], paper: dict, chunk_tokens: int, overlap_tokens: int,
                chunking_id: str) -> list[dict]:
    """`paper` needs: key, title, arxiv_id, version."""
    if overlap_tokens >= chunk_tokens:
        raise ValueError("overlap must be smaller than chunk size")
    # group consecutive body units by section (headings only set the label)
    groups: list[tuple[str, list[dict]]] = []
    for u in units:
        if u["kind"] == "heading":
            continue
        if groups and groups[-1][0] == u["section"]:
            groups[-1][1].append(u)
        else:
            groups.append((u["section"], [u]))

    chunks: list[dict] = []
    for section, body in groups:
        sents = _sentences(body, chunk_tokens)
        i = 0
        while i < len(sents):
            cur, n, j = [], 0, i
            while j < len(sents) and (not cur or n + sents[j][2] <= chunk_tokens):
                cur.append(sents[j])
                n += sents[j][2]
                j += 1
            text = " ".join(s for s, _, _ in cur)
            pages = sorted({p for _, p, _ in cur})
            idx = len(chunks)
            chunks.append({
                "chunk_id": f"{paper['key']}-{chunking_id}-{idx:04d}",
                "paper": paper["key"], "title": paper["title"],
                "arxiv_id": paper["arxiv_id"], "version": paper["version"],
                "section": section, "page_start": pages[0], "page_end": pages[-1],
                "pages": pages, "text": text, "n_tokens": n,
                "text_sha1": hashlib.sha1(text.encode("utf-8")).hexdigest(),
            })
            if j >= len(sents):
                break
            k, ov = j, 0                      # step back to create the overlap
            while k - 1 > i and ov + sents[k - 1][2] <= overlap_tokens:
                k -= 1
                ov += sents[k][2]
            i = k
    # chunk ids must be globally ordered per paper, which the running idx gives.
    return chunks
