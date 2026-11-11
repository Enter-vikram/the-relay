"""Question understanding: cheap, deterministic, no model call.

Kinds: direct | multi | comparison | ambiguous. "Unsupported by the papers" is
NOT decided here: it can only be judged against retrieved evidence, so it is
decided by the sufficiency check in app/retrieval.py. (A classifier that
guessed "unsupported" from the question text alone would be a fabricated
judgement about paper content.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.bm25 import tokenize

MAX_QUESTION_CHARS = 1000
# Words that make a *question* a question but carry no topical signal. Kept
# deliberately small and generic; nothing here is paper-specific.
QUESTION_STOP = frozenset(
    "explain describe tell paper papers work works model models show shows say says "
    "according about mean means give list summarize summary please between difference "
    "differences compare compared comparison versus vs both each other".split())
COMPARE_RE = re.compile(r"\b(compare|compared|comparison|versus|vs\.?|differ(?:s|ence|ences)?|"
                        r"difference between|similar(?:ity|ities)?|both)\b", re.I)
PRONOUN_LED = re.compile(r"^\s*(it|this|that|they|these|those|he|she)\b", re.I)
# Matches only the titles themselves, so a hint can never smuggle in paper content.
TITLE_HINTS = {"attention": re.compile(r"attention is all you need", re.I),
               "moshi": re.compile(r"\bmoshi\b", re.I),
               "dynamic-chunking": re.compile(r"dynamic chunking", re.I)}


class InvalidQuestion(ValueError):
    pass


@dataclass
class QuestionInfo:
    text: str
    kind: str
    terms: list[str]
    papers_named: list[str] = field(default_factory=list)
    reason: str = ""


def normalize(q: str) -> str:
    if not isinstance(q, str):
        raise InvalidQuestion("question must be text")
    q = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        raise InvalidQuestion("question is empty")
    if len(q) > MAX_QUESTION_CHARS:
        raise InvalidQuestion(f"question is longer than {MAX_QUESTION_CHARS} characters")
    return q


def query_terms(q: str) -> list[str]:
    return [t for t in dict.fromkeys(tokenize(q)) if t not in QUESTION_STOP]


def classify(q: str) -> QuestionInfo:
    q = normalize(q)
    terms = query_terms(q)
    named = [k for k, rx in TITLE_HINTS.items() if rx.search(q)]
    if len(terms) < 2 or (PRONOUN_LED.match(q) and len(terms) < 4):
        return QuestionInfo(q, "ambiguous", terms, named, "too little topical content to search")
    if COMPARE_RE.search(q) or len(named) >= 2:
        return QuestionInfo(q, "comparison", terms, named, "comparison wording")
    if q.count("?") > 1 or re.search(r"\band\b.+\b(why|how|what|which)\b", q, re.I):
        return QuestionInfo(q, "multi", terms, named, "several sub-questions")
    return QuestionInfo(q, "direct", terms, named)
