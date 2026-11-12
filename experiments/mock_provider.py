"""Deterministic MOCK model provider for offline pipeline runs (no network, no quota).

It is NOT a language model. `complete()` answers a generation prompt by EXTRACTING the passage sentences with the
highest question-word overlap and citing them [n]; it answers a verification prompt with "supported". So a mock run
tests plumbing (retrieval -> gate -> gateway -> citation check -> grading -> accounting) and gives an extractive-
baseline lower bound; it says NOTHING about Groq's answer quality, latency, or limits."""
from __future__ import annotations

import re
import time

from app import verifier as V
from app.llm import LLMResult
from app.tokens import estimate_tokens

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("the a an of to in is are was were what which how does do did and or for on with by as at from that this it its be can".split())


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def parse_passages(user: str) -> list[tuple[int, str]]:
    block = user.split("PASSAGES:\n", 1)[-1].split("\n\nQUESTION:", 1)[0].split("\n\nANSWER:", 1)[0]
    out = []
    for part in re.split(r"\n\n(?=\[\d+\])", block):
        m = re.match(r"\[(\d+)\][^\n]*\n(.*)", part, re.S)
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
    return out


def extractive_answer(user: str, n_sentences: int = 2) -> str:
    q = _terms(user.rsplit("QUESTION:", 1)[-1].split("\n\nYour previous answer", 1)[0])
    scored = []
    for n, text in parse_passages(user):
        for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)):
            if 5 <= len(s.split()) <= 60:
                scored.append((len(q & _terms(s)) / (len(_terms(s)) ** 0.5 or 1), n, s.strip()))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked, used = [], set()
    for sc, n, s in scored:
        if sc > 0 and s not in used:
            picked.append(f"{s.rstrip('.')} [{n}].")
            used.add(s)
        if len(picked) == n_sentences:
            break
    return " ".join(picked) if picked else V.REFUSAL_MARKER


class ExtractiveMockClient:
    source, model = "stand-in", "mock-extractive"

    async def complete(self, messages, max_tokens, timeout_s, stream=True) -> LLMResult:
        t = time.monotonic()
        is_verify = messages[0]["content"] == V.VERIFY_SYSTEM
        text = '{"verdict": "supported", "unsupported_sentences": []}' if is_verify else extractive_answer(messages[-1]["content"])
        tin = estimate_tokens(sum(len(m["content"]) for m in messages))
        tout = estimate_tokens(len(text)) - 70 + 30                  # nominal; mock has no hidden reasoning
        ms = (time.monotonic() - t) * 1000
        return LLMResult("ok", "ok", 200, text, ttft_ms=ms, total_ms=ms, tokens_in=tin, tokens_out=max(tout, 1))

    async def aclose(self) -> None:
        return None
