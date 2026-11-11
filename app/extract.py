"""PDF -> page-tagged text units with section labels.

Every unit keeps the PHYSICAL PDF page number (1-based) it came from, so a
citation can always be mapped back to a page of the original PDF. Printed page
numbers are not used. Known limits (documented, not hidden):
  * headings are detected heuristically (font size / bold / numbering);
  * equations come out as whatever text the PDF encodes, often garbled;
  * two-column reading order follows the PDF content stream, which is correct
    for typical LaTeX output but not guaranteed.
`scripts.ingest_papers` prints the heading outline so a human can check it.
"""
from __future__ import annotations

import collections
import re
from pathlib import Path

import pymupdf

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
NUMBERED = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z](?:\.\d+)*\.?)\s+\S")
KNOWN = {"abstract", "introduction", "conclusion", "conclusions", "references",
         "acknowledgements", "acknowledgments", "appendix", "bibliography"}
CAPTION = re.compile(r"^(?:Figure|Fig\.|Table)\s*\d+", re.IGNORECASE)
FRONT = "Front matter"


def _clean(s: str) -> str:
    for a, b in LIGATURES.items():
        s = s.replace(a, b)
    return s.replace("­", "")


def _join_lines(lines: list[str]) -> str:
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out.endswith("-") and ln[:1].islower() and len(out) > 1 and out[-2].isalpha():
            out = out[:-1] + ln          # de-hyphenate a wrapped word
        else:
            out = f"{out} {ln}" if out else ln
    return out


def _blocks(page: pymupdf.Page) -> list[dict]:
    """Text blocks of a page with style info; rotated margin text is dropped."""
    result = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        lines, sizes, bold_chars, chars = [], [], 0, 0
        for ln in b["lines"]:
            dx, _dy = ln.get("dir", (1.0, 0.0))
            if abs(dx) < 0.9:            # rotated text, e.g. the arXiv margin stamp
                continue
            txt = _clean("".join(sp["text"] for sp in ln["spans"]))
            if not txt.strip():
                continue
            lines.append(txt)
            for sp in ln["spans"]:
                t = sp["text"].strip()
                if not t:
                    continue
                n = len(t)
                chars += n
                sizes.append((round(sp["size"] * 2) / 2, n))
                if sp["flags"] & 16 or "bold" in sp["font"].lower():
                    bold_chars += n
        if not lines or chars == 0:
            continue
        text = _join_lines(lines)
        if re.fullmatch(r"\d{1,3}", text):   # bare page number
            continue
        result.append({"text": text, "n_lines": len(lines), "sizes": sizes,
                       "max_size": max(s for s, _ in sizes),
                       "bold": bold_chars / chars >= 0.8, "y0": b["bbox"][1]})
    return result


def _body_size(all_blocks: list[list[dict]]) -> float:
    c: collections.Counter = collections.Counter()
    for blocks in all_blocks:
        for b in blocks:
            for s, n in b["sizes"]:
                c[s] += n
    return c.most_common(1)[0][0] if c else 10.0


def _is_heading(b: dict, body: float) -> tuple[bool, bool]:
    """(is_heading, is_strong) - strong = numbered or a known section name."""
    t = b["text"]
    if len(t) > 120 or b["n_lines"] > 3 or CAPTION.match(t):
        return False, False
    evidence = b["bold"] or b["max_size"] >= body + 0.8
    numbered = bool(NUMBERED.match(t)) and not t.rstrip().endswith(".") or \
        bool(re.match(r"^\d+(?:\.\d+)+\.?\s+\S", t))
    known = re.sub(r"[^a-z]", "", t.lower().split("\n")[0]) in KNOWN or \
        t.strip().lower() in KNOWN
    if evidence and (numbered or known):
        return True, True
    if b["max_size"] >= body * 1.12 and not t.rstrip().endswith((".", ",")):
        return True, False
    return False, False


def extract_units(pdf_path: Path, paper_key: str) -> tuple[list[dict], dict]:
    """Return (units, meta). unit = {paper, page, section, kind, text}.
    kind is 'heading', 'para' or 'caption'."""
    with pymupdf.open(pdf_path) as doc:
        pages = [_blocks(p) for p in doc]
    body = _body_size(pages)
    units: list[dict] = []
    section, seen_real = FRONT, False
    for pno, blocks in enumerate(pages, start=1):
        for b in blocks:
            is_h, strong = _is_heading(b, body)
            if is_h and not seen_real and not strong:
                is_h = False         # big title/author lines before 'Abstract'
            if is_h:
                seen_real = seen_real or strong
                section = b["text"].strip()
                units.append({"paper": paper_key, "page": pno, "section": section,
                              "kind": "heading", "text": section})
            else:
                kind = "caption" if CAPTION.match(b["text"]) else "para"
                units.append({"paper": paper_key, "page": pno, "section": section,
                              "kind": kind, "text": b["text"]})
    meta = {"pages": len(pages), "body_font_size": body,
            "units": len(units), "headings": sum(u["kind"] == "heading" for u in units)}
    return units, meta


def outline(units: list[dict]) -> list[tuple[int, str]]:
    return [(u["page"], u["text"]) for u in units if u["kind"] == "heading"]
