"""Benchmark dataset: schema, loader, validator, grader and retrieval-relevance metrics.

The questions file is authored by a HUMAN from the real PDFs (benchmarks/questions.jsonl). Nothing in this
repo invents paper content. The validator makes fabrication hard: every `evidence.quote` must occur,
verbatim after whitespace/ligature/hyphenation normalisation, on the stated page of the extracted text of
the stated paper. A question whose "gold" evidence is not really in the PDF fails validation.

Row schema (one JSON object per line):
  id              unique string, e.g. "q01"
  type            direct | multi_passage | comparison | interpretation | unsupported | misleading
  question        the question as a user would ask it
  expect_refusal  true when the three papers do not establish an answer
  evidence        list of {paper, page, quote}; [] when expect_refusal
                    paper: attention | moshi | dynamic-chunking ; page: 1-based PDF page
                    quote: 6+ words copied from that page
  required_facts  list of groups; each group is a list of acceptable alternatives (case-insensitive substrings).
                  An answer must contain at least one alternative from EVERY group.   [] when expect_refusal
  min_cited       how many distinct evidence items the citations must cover (default: 1; for
                  multi_passage/comparison: len(evidence))
  trap            (misleading questions) one sentence: where a naive retriever goes wrong and why
  notes           free text for the reviewer
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from app.bm25 import tokenize

QTYPES = {"direct", "multi_passage", "comparison", "interpretation", "unsupported", "misleading"}
PAPER_KEYS = {"attention", "moshi", "dynamic-chunking"}
MIN_QUESTIONS = 15
MIN_QUOTE_WORDS = 6


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("­", "").replace("-\n", "").replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"').replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", " ", t).strip().lower()


def loose(text: str) -> str:
    """Alphanumerics only. Used to match gold quotes against page text: immune to line-break hyphenation,
    spacing, ligatures and quote styles. A 6+ word quote cannot plausibly match by accident."""
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", text).lower())


@dataclass
class Evidence:
    paper: str
    page: int
    quote: str


@dataclass
class Question:
    id: str
    type: str
    question: str
    expect_refusal: bool
    evidence: list[Evidence] = field(default_factory=list)
    required_facts: list[list[str]] = field(default_factory=list)
    min_cited: int = 1
    trap: str = ""
    notes: str = ""


class DatasetError(ValueError):
    pass


def load_questions(path: Path) -> list[Question]:
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"{path} does not exist. Author questions from the real PDFs; see benchmarks/README.md")
    out: list[Question] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            raise DatasetError(f"{path}:{n}: invalid JSON ({e})") from e
        if str(d.get("id", "")).upper().startswith("TEMPLATE"):
            raise DatasetError(f"{path}:{n}: template row present; delete it before benchmarking")
        ev = [Evidence(**e) for e in d.get("evidence", [])]
        multi = d.get("type") in ("multi_passage", "comparison")
        out.append(Question(id=d["id"], type=d["type"], question=d["question"],
                            expect_refusal=bool(d["expect_refusal"]), evidence=ev,
                            required_facts=[list(g) for g in d.get("required_facts", [])],
                            min_cited=int(d.get("min_cited", len(ev) if multi else 1)),
                            trap=d.get("trap", ""), notes=d.get("notes", "")))
    return out


def load_page_text(extracted_dir: Path) -> dict[tuple[str, int], str]:
    pages: dict[tuple[str, int], list[str]] = {}
    for p in sorted(Path(extracted_dir).glob("*.units.jsonl")):
        key = p.name.split(".units.jsonl")[0]
        for line in p.read_text(encoding="utf-8").splitlines():
            u = json.loads(line)
            pages.setdefault((key, u["page"]), []).append(u["text"])
    return {k: loose(" ".join(v)) for k, v in pages.items()}


def validate(questions: list[Question], page_text: dict[tuple[str, int], str]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors make the dataset unusable."""
    errs, warns = [], []
    seen: set[str] = set()
    for q in questions:
        w = f"{q.id}:"
        if q.id in seen:
            errs.append(f"{w} duplicate id")
        seen.add(q.id)
        if q.type not in QTYPES:
            errs.append(f"{w} unknown type {q.type!r}")
        if not q.question.strip():
            errs.append(f"{w} empty question")
        if q.expect_refusal:
            if q.evidence or q.required_facts:
                errs.append(f"{w} expect_refusal=true must have no evidence/required_facts")
            if q.type not in ("unsupported", "misleading", "interpretation", "direct", "multi_passage", "comparison"):
                errs.append(f"{w} bad type for refusal")
        else:
            if q.type == "unsupported":
                errs.append(f"{w} type 'unsupported' must have expect_refusal=true")
            if not q.evidence:
                errs.append(f"{w} answerable question needs evidence")
            if not q.required_facts or any(not g for g in q.required_facts):
                errs.append(f"{w} answerable question needs non-empty required_facts groups")
            if q.min_cited > len(q.evidence):
                errs.append(f"{w} min_cited exceeds evidence count")
        if q.type == "misleading" and not q.trap:
            warns.append(f"{w} misleading question without a 'trap' explanation")
        for ev in q.evidence:
            if ev.paper not in PAPER_KEYS:
                errs.append(f"{w} unknown paper {ev.paper!r}")
                continue
            text = page_text.get((ev.paper, ev.page))
            if text is None:
                errs.append(f"{w} {ev.paper} has no page {ev.page} in the extracted text")
                continue
            if len(ev.quote.split()) < MIN_QUOTE_WORDS:
                errs.append(f"{w} quote shorter than {MIN_QUOTE_WORDS} words")
            elif loose(ev.quote) not in text:
                errs.append(f"{w} quote not found on {ev.paper} p.{ev.page}: {ev.quote[:70]!r}")
    answerable = [q for q in questions if not q.expect_refusal]
    if len(questions) < MIN_QUESTIONS:
        errs.append(f"need at least {MIN_QUESTIONS} questions, have {len(questions)}")
    for t in QTYPES:
        if not any(q.type == t for q in questions):
            warns.append(f"no question of type {t!r}")
    covered = {e.paper for q in answerable for e in q.evidence}
    if covered != PAPER_KEYS:
        warns.append(f"answerable questions do not cover all papers (missing {sorted(PAPER_KEYS - covered)})")
    if not any(q.expect_refusal for q in questions):
        warns.append("no refusal questions")
    return errs, warns


# --------------------------------------------------------------------------- retrieval relevance
def _chunk_has_evidence(chunk: dict, ev: Evidence) -> bool:
    if chunk["paper"] != ev.paper or not (chunk["page_start"] <= ev.page <= chunk["page_end"]):
        return False
    ctext = loose(chunk["text"])
    if loose(ev.quote) in ctext:
        return True
    qt = set(tokenize(ev.quote))
    return bool(qt) and len(qt & set(tokenize(chunk["text"]))) / len(qt) >= 0.6    # quote split across a chunk edge


def retrieval_metrics(hits: list[dict], q: Question) -> dict:
    """hits: chunk dicts in rank order. For answerable questions only."""
    covered = [any(_chunk_has_evidence(c, ev) for c in hits) for ev in q.evidence]
    first = next((r for r, c in enumerate(hits, 1) if any(_chunk_has_evidence(c, ev) for ev in q.evidence)), None)
    return {"hit": any(covered), "recall": sum(covered) / len(covered) if covered else None,
            "full_recall": all(covered) if covered else None, "first_rank": first,
            "rr": (1.0 / first) if first else 0.0}


# --------------------------------------------------------------------------- grading
def cites_cover(citations: list[dict], q: Question) -> int:
    n = 0
    for ev in q.evidence:
        if any(c["paper"] == ev.paper and c["page_start"] <= ev.page <= c["page_end"] for c in citations):
            n += 1
    return n


def grade(q: Question, *, answered: bool, refused: bool, answer: str, citations: list[dict]) -> tuple[str, bool, str]:
    """Return (grade, needs_manual_review, why). Never grades from the model's own opinion of itself."""
    if refused:
        return ("refused_correctly", False, "refused as expected") if q.expect_refusal else \
               ("refused_wrongly", False, "refused an answerable question")
    if not answered:
        raise ValueError("neither answered nor refused: not gradable")
    if q.expect_refusal:
        return "wrong", False, "answered a question the papers do not establish"
    a = norm(answer)
    groups_hit = [any(norm(alt) in a for alt in g) for g in q.required_facts]
    facts_ok = all(groups_hit)
    covered = cites_cover(citations, q)
    cites_ok = covered >= q.min_cited
    if facts_ok and cites_ok:
        return "correct", False, "all required facts present and evidence cited"
    partial = any(groups_hit) and not facts_ok
    review = (facts_ok != cites_ok) or partial
    why = f"facts {sum(groups_hit)}/{len(groups_hit)} groups; cited evidence {covered}/{q.min_cited} needed"
    return "wrong", review, why
