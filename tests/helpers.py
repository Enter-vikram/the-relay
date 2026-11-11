"""Synthetic PDFs for tests. These are fixtures, NOT the real papers."""
import math
from pathlib import Path

import pymupdf

BODY, HEAD = 10, 14


def make_pdf(path: Path, pages: list[list[tuple[str, str]]], stamp: str | None = None,
             title: str | None = None, pad_to: int = 0) -> Path:
    """pages = [[(style, text), ...], ...]; style in {'h','p','c'} (heading/para/caption).
    A rotated arXiv-style margin stamp is added to page 1 when `stamp` is given."""
    doc = pymupdf.open()
    for pi, items in enumerate(pages):
        page = doc.new_page(width=612, height=792)
        y = 60.0
        if pi == 0 and title:
            page.insert_textbox(pymupdf.Rect(72, y, 540, y + 50), title, fontsize=20,
                                fontname="hebo")
            y += 70
        if pi == 0 and stamp:
            page.insert_text(pymupdf.Point(25, 600), stamp, fontsize=18, rotate=90)
        for style, text in items:
            size = HEAD if style == "h" else BODY
            font = "hebo" if style == "h" else "helv"
            lines = max(1, math.ceil(len(text) * size * 0.5 / 468))
            h = lines * size * 1.4 + 4
            rc = page.insert_textbox(pymupdf.Rect(72, y, 540, y + h), text, fontsize=size,
                                     fontname=font)
            assert rc >= 0, f"fixture text did not fit: {text[:30]!r}"
            y += h + 14                      # generous gap => separate PDF blocks
    if pad_to:
        # embedded blob so synthetic files can satisfy the production size check
        doc.embfile_add("pad.bin", b"0" * pad_to)
    doc.save(path, garbage=0, deflate=False)
    doc.close()
    return path
