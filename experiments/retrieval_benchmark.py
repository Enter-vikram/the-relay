"""Retrieval-only benchmark (NO model calls, NO quota): how well does each setting find the gold evidence,
and how does the sufficiency gate separate answerable from unsupported questions?

    python -m experiments.retrieval_benchmark                 # settings A, B, C with BM25
    python -m experiments.retrieval_benchmark --hybrid        # also BM25+dense (needs requirements-optional.txt)
    python -m experiments.retrieval_benchmark --sweep         # threshold sweep for the sufficiency gate

Runs the SAME retrieval code path the pipeline uses (Pipeline.retrieve_only). Needs benchmarks/questions.jsonl,
validated against the extracted PDFs. Writes results/raw/rb-<run>.rows.csv (one row per setting x method x question)
and prints a summary. Hybrid retrieval that cannot run is reported "not measured", never faked.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.chunking import SETTINGS
from app.embeddings import EmbeddingsUnavailable, get_embedder
from app.limiter import Limiter
from app.metrics import percentile
from app.papers import RESEARCH_DIR
from app.pipeline import Pipeline, PipelineConfig
from app.queue_manager import QueueManager
from app.retrieval import Library, Retriever, SufficiencyConfig
from app.tokens import estimate_tokens
from experiments.benchmark_data import (DatasetError, load_page_text, load_questions, retrieval_metrics, validate)

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "benchmarks" / "questions.jsonl"
RAW = ROOT / "results" / "raw"
INDEX = RESEARCH_DIR / "index"


class _NullGateway:
    """Retrieval never calls the model; the pipeline only needs a limiter for budget maths."""
    def __init__(self):
        self.limiter = Limiter(30, 8000, 1000, 200_000)
        self.queue = QueueManager(1, 1)


def setting_label(s) -> str:
    return f"{s.name}: {s.chunk_tokens}tok/{s.top_k}/{'rerank' if s.rerank else 'no-rerank'}"


async def evaluate(setting, mode: str, questions, lib: Library, embedder, suff: SufficiencyConfig) -> list[dict]:
    cfg = PipelineConfig(setting, mode=mode, sufficiency=suff)
    pipe = Pipeline(Retriever(lib, embedder), _NullGateway(), cfg)
    rows = []
    for q in questions:
        t = time.monotonic()
        info, hits, res = await pipe.retrieve_only(q.question)
        ms = (time.monotonic() - t) * 1000
        gated = hits is None
        row = {"setting": setting_label(setting), "mode": mode, "id": q.id, "type": q.type,
               "expect_refusal": q.expect_refusal, "question_kind": info.kind, "gated_refusal": gated,
               "gate_reason": res.reason if gated else "", "expanded": res.sufficiency.get("expanded", ""),
               "coverage": res.sufficiency.get("coverage", ""), "top_score": res.sufficiency.get("top_score", ""),
               "n_hits": len(hits or []), "retrieval_ms": round(ms, 2),
               "context_tokens_est": sum(estimate_tokens(len(h.chunk["text"])) - 70 for h in (hits or [])),
               "hit": "", "recall": "", "first_rank": "", "rr": "", "shown_hit": ""}
        if not q.expect_refusal:
            # Retrieval quality is measured on the raw first-pass top-k, BEFORE the sufficiency gate, so a gate
            # refusal is not miscounted as a retrieval miss. `shown_hit` = evidence actually reached the model.
            m = retrieval_metrics([h.chunk for h in res.first_pass], q)
            shown = retrieval_metrics([h.chunk for h in (hits or [])], q)
            row.update(hit=m["hit"], recall=round(m["recall"], 3), first_rank=m["first_rank"] or "",
                       rr=round(m["rr"], 3), shown_hit=shown["hit"])
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if not r["expect_refusal"]]
    uns = [r for r in rows if r["expect_refusal"]]
    pct = lambda n, d: round(100.0 * n / d, 1) if d else None
    return {"questions": len(rows), "answerable": len(ans), "expect_refusal": len(uns),
            "hit_at_k_pct": pct(sum(r["hit"] is True for r in ans), len(ans)),
            "full_evidence_recall_pct": pct(sum(float(r["recall"]) == 1.0 for r in ans if r["recall"] != ""), len(ans)),
            "mrr": round(sum(float(r["rr"] or 0) for r in ans) / len(ans), 3) if ans else None,
            "shown_hit_after_gate_pct": pct(sum(r["shown_hit"] is True for r in ans), len(ans)),
            "gate_blocks_unsupported_pct": pct(sum(r["gated_refusal"] for r in uns), len(uns)),
            "gate_blocks_answerable_pct": pct(sum(r["gated_refusal"] for r in ans), len(ans)),
            "mean_context_tokens_est": round(sum(r["context_tokens_est"] for r in rows) / len(rows), 0),
            "retrieval_ms_p50": round(percentile([r["retrieval_ms"] for r in rows], 50), 2),
            "retrieval_ms_p95": round(percentile([r["retrieval_ms"] for r in rows], 95), 2)}


async def main_async(a) -> int:
    try:
        questions = load_questions(QUESTIONS)
    except DatasetError as e:
        print("ERROR:", e)
        return 2
    errs, warns = validate(questions, load_page_text(RESEARCH_DIR / "extracted"))
    if errs:
        print("Dataset invalid; run python -m experiments.validate_benchmark")
        return 2
    for w in warns:
        print("WARN:", w)
    embedder = None
    modes = ["bm25"]
    if a.hybrid:
        try:
            embedder = get_embedder()
            modes.append("hybrid")
        except EmbeddingsUnavailable as e:
            print(f"hybrid: NOT MEASURED ({e})")
    run_id = a.run_id or datetime.now(timezone.utc).strftime("rb%Y%m%dT%H%M%S")
    allrows: list[dict] = []
    for sname in a.settings:
        setting = SETTINGS[sname]
        for mode in modes:
            try:
                lib = Library.load(INDEX, setting.chunking_id, with_vectors=(mode == "hybrid"))
            except (FileNotFoundError, EmbeddingsUnavailable) as e:
                print(f"{sname}/{mode}: NOT MEASURED ({e})")
                continue
            rows = await evaluate(setting, mode, questions, lib, embedder, SufficiencyConfig())
            allrows += rows
            print(f"\n{setting_label(setting)} [{mode}]")
            for k, v in summarize(rows).items():
                print(f"  {k:30s} {v}")
    if a.sweep:
        print("\nSufficiency gate sweep (BM25, per setting): coverage x top_score -> (blocks unsupported %, blocks answerable %)")
        for sname in a.settings:
            setting = SETTINGS[sname]
            lib = Library.load(INDEX, setting.chunking_id)
            print(f"  {setting_label(setting)}")
            for cov in (0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8):
                for top in (0.0, 2.0, 4.0, 6.0):
                    s = summarize(await evaluate(setting, "bm25", questions, lib, None, SufficiencyConfig(cov, top)))
                    print(f"    min_coverage={cov:<4} min_top_score={top:<4} -> "
                          f"{s['gate_blocks_unsupported_pct']}% / {s['gate_blocks_answerable_pct']}%")
    if allrows:
        RAW.mkdir(parents=True, exist_ok=True)
        out = RAW / f"{run_id}.rows.csv"
        if out.exists():
            print(f"ERROR: {out} exists; raw results are immutable")
            return 1
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(allrows[0].keys()))
            w.writeheader()
            w.writerows(allrows)
        print("\nraw rows ->", out)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", nargs="+", default=["A", "B", "C"], choices=sorted(SETTINGS))
    ap.add_argument("--hybrid", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--run-id")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
