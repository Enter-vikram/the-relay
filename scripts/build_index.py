"""Build chunk sets + BM25 indexes for every distinct chunking in SETTINGS.

    python -m scripts.build_index

Reads research/extracted/*.units.jsonl, writes research/index/<chunking_id>/
{chunks.jsonl, bm25.json, info.json}. Deterministic: same units -> same files.
Settings that share a chunking (B and C) share one index.
"""
import json
import statistics
import sys

from app.bm25 import BM25Index
from app.chunking import SETTINGS, chunk_units
from app.papers import PAPERS, RESEARCH_DIR

EXTRACTED = RESEARCH_DIR / "extracted"
INDEX = RESEARCH_DIR / "index"
SMOKE = ["scaled dot-product attention", "Mimi neural audio codec", "dynamic chunking routing"]


def main() -> int:
    for p in PAPERS:
        if not (EXTRACTED / f"{p.key}.units.jsonl").is_file():
            print(f"ERROR: missing extraction for {p.key}. Run: python -m scripts.ingest_papers")
            return 1
    units = {p.key: [json.loads(line) for line in
                     (EXTRACTED / f"{p.key}.units.jsonl").read_text(encoding="utf-8").splitlines()]
             for p in PAPERS}
    summary = json.loads((EXTRACTED / "summary.json").read_text(encoding="utf-8"))

    done = {}
    for s in SETTINGS.values():
        cid = s.chunking_id
        if cid in done:
            continue
        chunks = []
        for p in PAPERS:
            meta = {"key": p.key, "title": p.title, "arxiv_id": p.arxiv_id, "version": p.version}
            chunks += chunk_units(units[p.key], meta, s.chunk_tokens, s.overlap_tokens, cid)
        idx = BM25Index.build(chunks)
        d = INDEX / cid
        d.mkdir(parents=True, exist_ok=True)
        with (d / "chunks.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for c in chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        idx.save(d / "bm25.json")
        toks = [c["n_tokens"] for c in chunks]
        info = {"chunking_id": cid, "chunk_tokens": s.chunk_tokens,
                "overlap_tokens": s.overlap_tokens, "n_chunks": len(chunks),
                "per_paper": {p.key: sum(c["paper"] == p.key for c in chunks) for p in PAPERS},
                "tokens_mean": round(statistics.mean(toks), 1), "tokens_max": max(toks),
                "chunks_sha256": idx.chunks_sha256,
                "source_pdf_sha256": {k: v["pdf_sha256"] for k, v in summary.items()}}
        (d / "info.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
        done[cid] = (idx, {c["chunk_id"]: c for c in chunks}, info)
        print(f"{cid}: {len(chunks)} chunks {info['per_paper']} "
              f"mean {info['tokens_mean']} max {info['tokens_max']} tokens")

    print("\nSmoke queries (inspect by eye; these are NOT quality measurements):")
    idx, by_id, _ = next(iter(done.values()))
    for q in SMOKE:
        print(f"  q: {q}")
        for cid_, score in idx.search(q, 3):
            c = by_id[cid_]
            print(f"     {score:6.2f}  {c['paper']} p{c['page_start']}-{c['page_end']} | "
                  f"{c['section'][:50]}")
    print("\nRESULT: indexes written to", INDEX)
    return 0


if __name__ == "__main__":
    sys.exit(main())
