"""Prompts, citation parsing/validation and the verdict parser. No network, no model calls.

Two independent layers (cheap one first):
  1. analyze(): deterministic checks on the answer text against the passages the
     model was shown: every substantive sentence cites; markers resolve to real
     passages; cited pages exist in the PDF; every number appears in the cited
     text; lexical support (share of the sentence's terms found in the cited
     passages). Lexical support is a WEAK NECESSARY condition, not proof.
  2. A separate model call (verify prompt) judges semantic support, using only
     the cited passages so the call stays small.
Passing layer 1 never replaces layer 2 when LLM verification is enabled.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.bm25 import tokenize

REFUSAL_MARKER = "INSUFFICIENT_EVIDENCE"
REFUSAL_TEXT = ("The available research library (the three specified papers) does not establish an "
                "answer to this question, so I am not answering it.")
MARK = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
NUM = re.compile(r"\d+(?:\.\d+)?")

GEN_SYSTEM = (
    "You answer questions about three specific research papers using ONLY the numbered passages provided. "
    "Rules: (1) Use no outside knowledge. (2) Be concise: at most 5 sentences. "
    "(3) End every sentence with the number(s) of the supporting passage(s) in square brackets, e.g. [1] or [1][3]. "
    "(4) Copy numbers exactly as written in the passages. "
    f"(5) If the passages do not contain enough information to answer, reply with exactly: {REFUSAL_MARKER}")

VERIFY_SYSTEM = (
    "You are a strict fact checker. You get an ANSWER and the numbered PASSAGES it cites. "
    "Decide whether every sentence of the answer is directly supported by the passages it cites. "
    "Respond with ONLY a JSON object: "
    '{"verdict": "supported" | "unsupported", "unsupported_sentences": ["..."]}')


@dataclass(frozen=True)
class Passage:
    n: int
    chunk: dict

    @property
    def header(self) -> str:
        c = self.chunk
        pages = f"p.{c['page_start']}" if c["page_start"] == c["page_end"] else f"pp.{c['page_start']}-{c['page_end']}"
        return f"[{self.n}] {c['title']} (arXiv:{c['arxiv_id']}v{c['version']}), {pages}, section: {c['section']}"


def passages_from_hits(hits) -> list[Passage]:
    return [Passage(i + 1, h.chunk) for i, h in enumerate(hits)]


def passage_block(passages: list[Passage]) -> str:
    return "\n\n".join(f"{p.header}\n{p.chunk['text']}" for p in passages)


def gen_messages(question: str, passages: list[Passage], feedback: str = "", previous: str = "") -> list[dict]:
    user = f"PASSAGES:\n{passage_block(passages)}\n\nQUESTION: {question}"
    if feedback:
        user += (f"\n\nYour previous answer was:\n{previous}\n\nIt had these problems:\n{feedback}\n"
                 "Rewrite it using only the passages. Remove any claim you cannot support. "
                 f"If nothing can be supported, reply exactly {REFUSAL_MARKER}.")
    return [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": user}]


def verify_messages(answer: str, passages: list[Passage], cited: set[int]) -> list[dict]:
    used = [p for p in passages if p.n in cited] or passages
    user = f"PASSAGES:\n{passage_block(used)}\n\nANSWER:\n{answer}"
    return [{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": user}]


def is_refusal(text: str) -> bool:
    return REFUSAL_MARKER in text.upper().replace(" ", "_") or text.strip().upper() == REFUSAL_MARKER


# ---------------------------------------------------------------- layer 1
@dataclass
class Problem:
    kind: str      # empty | uncited_sentence | unknown_marker | bad_page | unsupported_number | weak_support
    sentence: str
    detail: str = ""


@dataclass
class CitationReport:
    ok: bool
    problems: list[Problem] = field(default_factory=list)
    cited: set[int] = field(default_factory=set)
    sentences: int = 0

    def feedback(self) -> str:
        return "\n".join(f"- {p.kind}: {p.sentence[:160]} {p.detail}".strip() for p in self.problems)


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    parts = re.split(r"(?<=[.!?])(?:\s*(?:\[[\d,\s]+\])+)?\s+(?=[A-Z0-9(\[])", text)
    # a trailing marker group belongs to the sentence before it: re-attach when split dropped it
    return [p.strip() for p in parts if p.strip()]


def _numbers(s: str) -> set[str]:
    return set(NUM.findall(s.replace(",", "")))


def analyze(answer: str, passages: list[Passage], *, support_threshold: float = 0.5,
            page_counts: dict[str, int] | None = None) -> CitationReport:
    by_n = {p.n: p for p in passages}
    rep = CitationReport(ok=True)
    if not answer.strip():
        return CitationReport(False, [Problem("empty", "")])
    for sent in split_sentences(answer):
        if sent.lower().startswith("sources:"):
            break
        rep.sentences += 1
        marks = [int(x) for m in MARK.findall(sent) for x in re.split(r"\s*,\s*", m)]
        body = MARK.sub(" ", sent)
        terms = [t for t in tokenize(body)]
        if not marks:
            if len(terms) >= 3 or NUM.search(body):
                rep.problems.append(Problem("uncited_sentence", sent))
            continue
        bad = [m for m in marks if m not in by_n]
        if bad:
            rep.problems.append(Problem("unknown_marker", sent, f"markers {bad} do not exist"))
        good = [by_n[m] for m in marks if m in by_n]
        rep.cited |= {p.n for p in good}
        if page_counts:
            for p in good:
                c = p.chunk
                total = page_counts.get(c["paper"])
                if total is not None and not (1 <= c["page_start"] <= c["page_end"] <= total):
                    rep.problems.append(Problem("bad_page", sent, f"{c['paper']} has {total} pages"))
        if not good:
            continue
        text = " ".join(p.chunk["section"] + " " + p.chunk["text"] for p in good)
        extra_nums = _numbers(body) - _numbers(text)
        if extra_nums:
            rep.problems.append(Problem("unsupported_number", sent, f"numbers not in cited passages: {sorted(extra_nums)}"))
        pool = set(tokenize(text))
        if terms:
            support = sum(t in pool for t in terms) / len(terms)
            if support < support_threshold:
                rep.problems.append(Problem("weak_support", sent, f"only {support:.0%} of its terms appear in the cited passages"))
    if rep.sentences == 0:
        rep.problems.append(Problem("empty", answer))
    rep.ok = not rep.problems
    return rep


def citations_for(passages: list[Passage], cited: set[int]) -> list[dict]:
    out = []
    for p in passages:
        if p.n in cited:
            c = p.chunk
            out.append({"n": p.n, "paper": c["paper"], "title": c["title"],
                        "arxiv": f"arXiv:{c['arxiv_id']}v{c['version']}", "pages": c["pages"],
                        "page_start": c["page_start"], "page_end": c["page_end"],
                        "section": c["section"], "chunk_id": c["chunk_id"]})
    return out


def render_sources(cites: list[dict]) -> str:
    lines = []
    for c in cites:
        pg = f"p.{c['page_start']}" if c["page_start"] == c["page_end"] else f"pp.{c['page_start']}-{c['page_end']}"
        lines.append(f"[{c['n']}] {c['title']} ({c['arxiv']}), {pg}, section: {c['section']}")
    return "Sources:\n" + "\n".join(lines)


# ---------------------------------------------------------------- layer 2
@dataclass
class Verdict:
    parsed: bool
    supported: bool = False
    unsupported: list[str] = field(default_factory=list)


def parse_verdict(text: str) -> Verdict:
    """Anything that is not clearly 'supported' counts as NOT verified (unparseable => unverified)."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return Verdict(False)
    try:
        d = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return Verdict(False)
    v = str(d.get("verdict", "")).strip().lower()
    if v not in ("supported", "unsupported"):
        return Verdict(False)
    uns = [str(x) for x in (d.get("unsupported_sentences") or [])]
    return Verdict(True, v == "supported" and not uns, uns)
