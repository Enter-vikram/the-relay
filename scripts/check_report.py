"""Check report/final_report.md: the 12 required headings in order, remaining placeholders, size.
    python -m scripts.check_report [--pdf report\\final_report.pdf]
Page count is only VERIFIED when you pass an exported PDF; the word-count estimate is not a page count."""
import argparse
import re
import sys
from pathlib import Path

# Order follows the assignment brief: 1-5, then the two headings it inserts after 5, then 6-8 (Retries, What the
# person sees, What I'm unsure about), then "Your feedback on Vaani", and finally "How I worked with the assistant".
HEADINGS = [
    "What I built and why", "The real service and my stand-in", "Results", "Where the time goes",
    "When a limit is hit", "Retrieval: quality versus cost", "How this library will mislead a naive retriever",
    "Retries", "What the person sees",
    "What I'm unsure about, what I left out, what I learned", "Your feedback on Vaani",
    "How I worked with the assistant"]
MARKERS = ("[TODO", "[NOT MEASURED")


def check(text: str) -> dict:
    found = [m.group(2).strip() for m in re.finditer(r"^##\s+(\d+)\.\s+(.*)$", text, re.M)]
    prose = [l for l in text.splitlines() if not l.lstrip().startswith(("|", "#"))]
    return {"headings_ok": found == HEADINGS, "found": found,
            "placeholders": sum(l.count(m) for l in prose for m in MARKERS),
            "words": len(" ".join(prose).split())}


def pdf_pages(path: Path) -> int:
    import pymupdf
    with pymupdf.open(path) as d:
        return d.page_count


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=Path("report/final_report.md"))
    ap.add_argument("--pdf", type=Path)
    a = ap.parse_args(argv)
    r = check(a.file.read_text(encoding="utf-8"))
    print("required 12 headings, in order:", "OK" if r["headings_ok"] else "MISMATCH")
    if not r["headings_ok"]:
        print("  found:", r["found"])
    print("open placeholders:", r["placeholders"], "| prose words (tables excluded):", r["words"],
          "(roughly 500-600 words per page with tables: an ESTIMATE, not a page count)")
    ok = r["headings_ok"]
    if a.pdf:
        n = pdf_pages(a.pdf)
        print("PDF pages:", n, "(limit 2)")
        ok = ok and n <= 2
    else:
        print("page count: NOT VERIFIED (export the report to PDF and pass --pdf)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
