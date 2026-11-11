"""Extract page-tagged text units from the verified PDFs.

    python -m scripts.ingest_papers

Writes research/extracted/<paper>.units.jsonl (+ summary.json) and prints the
detected heading outline per paper. READ THE OUTLINE: heading detection is a
heuristic, and section names in citations come from it.
Refuses to run if a PDF fails verification or differs from the manifest.
"""
import json
import sys

from app.extract import extract_units, outline
from app.papers import PAPERS, PAPERS_DIR, RESEARCH_DIR, load_manifest, verify_pdf

OUT = RESEARCH_DIR / "extracted"


def main() -> int:
    manifest = load_manifest().get("papers", {})
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    for p in PAPERS:
        pdf = PAPERS_DIR / p.filename
        v = verify_pdf(pdf, p)
        entry = manifest.get(p.key)
        if not v.ok or entry is None or entry.get("sha256") != v.info.get("sha256"):
            print(f"ERROR: {p.key}: PDF not verified against manifest "
                  f"({v.summary() if not v.ok else 'missing/mismatched manifest entry'}). "
                  f"Run: python -m scripts.download_papers")
            return 1
        units, meta = extract_units(pdf, p.key)
        with (OUT / f"{p.key}.units.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for u in units:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        meta.update(pdf_sha256=v.info["sha256"], arxiv=f"{p.arxiv_id}v{p.version}")
        summary[p.key] = meta
        print(f"\n== {p.key} ({meta['arxiv']}): {meta['pages']} pages, {meta['units']} units, "
              f"{meta['headings']} headings, body font {meta['body_font_size']}pt")
        for page, text in outline(units):
            print(f"   p{page:>2}  {text[:90]}")
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
    print("\nRESULT: extraction written to", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
