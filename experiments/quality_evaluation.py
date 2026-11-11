"""Real-Groq answer-quality evaluation (RUN ON YOUR MACHINE, uses your key and quota).

    python -m experiments.quality_evaluation --settings A --dry-run     # free: retrieval + token estimate only
    python -m experiments.quality_evaluation --settings A --yes         # spends quota: one setting at a time
    python -m experiments.quality_evaluation --settings B --yes --run-id qe-main
    python -m experiments.quality_evaluation --settings C --yes --run-id qe-main

Why one setting per run: each answer costs 2 to 4 model calls of several thousand tokens, and the DOCUMENTED
daily token budget (200 000, unverified for this account) can be exhausted by an unguarded full run. A local
24 h ledger (results/real_usage_ledger.jsonl) is checked before every question; the run stops at 80% of the
documented daily budgets instead of exhausting them. Runs are resumable: re-run with the same --run-id and
finished questions are skipped.

Output (append-only, one result row per question, source=real):
    results/raw/<run_id>__<setting>.results.csv / .diagnostics.csv
    results/raw/<run_id>__<setting>.answers.jsonl      full answers + citations for manual review
    results/grading_review.csv                          borderline grades to inspect by hand
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import usage_ledger
from app import verifier as V
from app.chunking import SETTINGS
from app.config import gateway_settings as G, settings
from app.main import build_gateway
from app.metrics import Record, append_records
from app.papers import RESEARCH_DIR
from app.pipeline import Pipeline, PipelineConfig
from app.queue_manager import BATCH
from app.retrieval import Library, Retriever
from app.tokens import DEFAULT_EXPECTED_OUT, estimate_tokens
from experiments.benchmark_data import DatasetError, grade, load_page_text, load_questions, validate
from experiments.retrieval_benchmark import INDEX, QUESTIONS, RAW, _NullGateway, setting_label

LEDGER = Path(__file__).resolve().parent.parent / "results" / "real_usage_ledger.jsonl"
REVIEW = Path(__file__).resolve().parent.parent / "results" / "grading_review.csv"
BUDGET_FRACTION = 0.8


def estimate_question_tokens(pipe: Pipeline, hits) -> int:
    """Worst-case-ish estimate: generation call + verification call + one correction round (conservative)."""
    ps = V.passages_from_hits(hits)
    gen_in = estimate_tokens(sum(len(m["content"]) for m in V.gen_messages("x" * 150, ps)))
    ver_in = estimate_tokens(len(V.VERIFY_SYSTEM) + len(V.passage_block(ps)) // 2 + 600)
    one_round = gen_in + DEFAULT_EXPECTED_OUT + ver_in + 250
    return int(one_round * 1.5)


async def main_async(a) -> int:
    try:
        questions = load_questions(QUESTIONS)
    except DatasetError as e:
        print("ERROR:", e)
        return 2
    errs, _ = validate(questions, load_page_text(RESEARCH_DIR / "extracted"))
    if errs:
        print("Dataset invalid; run python -m experiments.validate_benchmark")
        return 2
    if a.limit:
        questions = questions[:a.limit]
    run_id = a.run_id or datetime.now(timezone.utc).strftime("qe%Y%m%dT%H%M%S")
    used_req, used_tok = usage_ledger.used_last_24h(LEDGER)
    print(f"ledger (last 24 h): {used_req} requests, {used_tok} tokens | documented daily limits: "
          f"{G.rpd} requests, {G.tpd} tokens (unverified for this account)")
    gw = None
    for sname in a.settings:
        setting = SETTINGS[sname]
        lib = Library.load(INDEX, setting.chunking_id)
        retriever = Retriever(lib)
        cfg = PipelineConfig(setting, gen_max_tokens=G.gen_max_tokens, deadline_s=a.deadline)
        probe = Pipeline(retriever, _NullGateway(), cfg)
        est_total = 0
        for q in questions:
            _, hits, _ = await probe.retrieve_only(q.question)
            est_total += estimate_question_tokens(probe, hits) if hits else 0
        remaining = int(G.tpd * BUDGET_FRACTION) - used_tok
        print(f"\n{setting_label(setting)}: {len(questions)} questions, estimated up to ~{est_total} tokens "
              f"(remaining safe budget ~{remaining}).")
        if a.dry_run:
            continue
        if not a.yes:
            print("Refusing to spend quota without --yes.")
            return 3
        if est_total > remaining and not a.ignore_estimate:
            print("Estimate exceeds the safe remaining daily budget. Use --limit N, run this setting tomorrow, "
                  "or pass --ignore-estimate (the per-question ledger guard still applies).")
            return 3
        if gw is None:
            gw, source = build_gateway("real")
        pipe = Pipeline(retriever, gw, cfg)
        base = RAW / f"{run_id}__{sname}"
        res_p, diag_p, ans_p = (Path(str(base) + s) for s in (".results.csv", ".diagnostics.csv", ".answers.jsonl"))
        done = set()
        if res_p.is_file():
            done = {r["request_id"] for r in csv.DictReader(res_p.open(encoding="utf-8"))}
        for q in questions:
            rid = f"{run_id}-{sname}-{q.id}"
            if rid in done:
                continue
            _, hits, _ = await probe.retrieve_only(q.question)
            need = estimate_question_tokens(probe, hits) if hits else 0
            req_used, tok_used = usage_ledger.used_last_24h(LEDGER)
            if tok_used + need > G.tpd * BUDGET_FRACTION or req_used + 4 > G.rpd * BUDGET_FRACTION:
                print(f"STOP: next question would pass {int(BUDGET_FRACTION * 100)}% of the daily budget "
                      f"({tok_used} tokens, {req_used} requests used). Resume tomorrow with --run-id {run_id}.")
                return 4
            start = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            r = await pipe.ask(q.question, rid, priority=BATCH)
            calls = r.model_calls + r.retries
            usage_ledger.append(LEDGER, requests=calls, tokens=(r.tokens_in or 0) + (r.tokens_out or 0), tag=rid)
            g, review, why = (None, False, "")
            if r.status == "ok":
                g, review, why = grade(q, answered=r.answered, refused=r.refused, answer=r.answer, citations=r.citations)
            rec = Record(
                request_id=rid, scenario=f"quality-{sname}", source="real", start_time=start, status=r.status,
                first_token_ms=r.first_token_ms, total_ms=r.total_ms, retries=r.retries, tokens_in=r.tokens_in,
                tokens_out=r.tokens_out, retrieval_setting=setting_label(setting), answer_grade=g,
                final_reason=r.reason or ("answered" if r.answered else ""), priority=BATCH, attempts=calls,
                queue_wait_ms=r.stage_ms.get("queue_ms"), limiter_wait_ms=r.stage_ms.get("limiter_ms"),
                retry_delay_ms=r.stage_ms.get("retry_delay_ms"), est_tokens_in=r.est_tokens_in,
                stage_retrieval_ms=r.stage_ms.get("retrieval_ms"), stage_generation_ms=r.stage_ms.get("generation_ms"),
                stage_verification_ms=r.stage_ms.get("verification_ms"), model_calls=r.model_calls,
                user_message=(r.message or "")[:200].replace("\n", " "), run_id=run_id)
            append_records([rec], res_p, diag_p)
            with ans_p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"request_id": rid, "id": q.id, "type": q.type, "grade": g, "review": review,
                                    "why": why, "question": q.question, "expect_refusal": q.expect_refusal,
                                    "answer": r.answer, "reason": r.reason, "verification": r.verification,
                                    "citations": r.citations, "retrieved": r.retrieved, "status": r.status},
                                   ensure_ascii=False) + "\n")
            if review or (g == "wrong"):
                new = not REVIEW.exists()
                with REVIEW.open("a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["request_id", "setting", "question_id", "grade", "why", "answer"])
                    w.writerow([rid, sname, q.id, g, why, (r.answer or "")[:1500]])
            print(f"[{sname}] {q.id}: status={r.status} grade={g} calls={r.model_calls} retries={r.retries} "
                  f"tokens={(r.tokens_in or 0) + (r.tokens_out or 0)} total={r.total_ms}ms")
            if r.status == "rate_limited" and r.reason in ("daily_exhausted", "daily_budget"):
                print("STOP: provider daily capacity reported exhausted.")
                return 4
    if gw is not None:
        await gw.client.aclose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", nargs="+", default=["A"], choices=sorted(SETTINGS))
    ap.add_argument("--yes", action="store_true", help="confirm that real quota will be spent")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--run-id")
    ap.add_argument("--deadline", type=float, default=120.0, help="per-question deadline seconds (pacing may wait)")
    ap.add_argument("--ignore-estimate", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
