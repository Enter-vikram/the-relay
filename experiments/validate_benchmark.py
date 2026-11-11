"""Validate benchmarks/questions.jsonl against the extracted PDF text.   python -m experiments.validate_benchmark"""
import sys
from pathlib import Path

from app.papers import RESEARCH_DIR
from experiments.benchmark_data import DatasetError, load_page_text, load_questions, validate

QUESTIONS = Path(__file__).resolve().parent.parent / "benchmarks" / "questions.jsonl"


def main() -> int:
    try:
        qs = load_questions(QUESTIONS)
    except DatasetError as e:
        print("ERROR:", e)
        return 2
    pages = load_page_text(RESEARCH_DIR / "extracted")
    if not pages:
        print("ERROR: no extracted text. Run: python -m scripts.ingest_papers")
        return 2
    errs, warns = validate(qs, pages)
    for w in warns:
        print("WARN :", w)
    for e in errs:
        print("ERROR:", e)
    by = {}
    for q in qs:
        by[q.type] = by.get(q.type, 0) + 1
    print(f"{len(qs)} questions {by}; {sum(q.expect_refusal for q in qs)} expect refusal")
    print("RESULT:", "INVALID" if errs else "VALID")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
