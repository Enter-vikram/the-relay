"""Consolidated A/B/C comparison + integrity checks -> report/ab_comparison.md (all numbers computed here).
    python -m scripts.compare_settings [--run-id mock-1] [--retrieval-run rb-2]
Sections are labelled by provenance: retrieval-only on the real papers; MOCK (extractive stand-in, not an LLM)
pipeline run for plumbing; real Groq results only if a results/raw/qa-* run exists (otherwise "not measured")."""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import pandas as pd

from app.chunking import SETTINGS
from experiments.benchmark_data import load_questions

ROOT = Path(__file__).resolve().parent.parent
GRADES = ("correct", "wrong", "refused_correctly", "refused_wrongly")
STATUSES = {"ok", "rate_limited", "timeout", "error"}


def load_run(dir_: Path, run_id: str, s: str) -> tuple[list[dict], pd.DataFrame] | None:
    ans = dir_ / f"{run_id}__{s}.answers.jsonl"
    res = dir_ / f"{run_id}__{s}.results.csv"
    if not (ans.is_file() and res.is_file()):
        return None
    return [json.loads(l) for l in ans.open(encoding="utf-8") if l.strip()], pd.read_csv(res)


def integrity(answers: list[dict], res: pd.DataFrame, qids: list[str], pages: dict | None) -> list[str]:
    """Return a list of problems (empty = every check passed)."""
    errs = []
    ids = [a["id"] for a in answers]
    if sorted(ids) != sorted(qids):
        errs.append(f"question ids differ: missing {sorted(set(qids) - set(ids))}, extra {sorted(set(ids) - set(qids))}")
    if len(set(ids)) != len(ids):
        errs.append("duplicate question ids")
    if len(res) != len(qids) or res.request_id.duplicated().any():
        errs.append(f"results.csv has {len(res)} rows / duplicates for {len(qids)} questions")
    if not set(res.status) <= STATUSES:
        errs.append(f"invalid status values {sorted(set(res.status) - STATUSES)}")
    for a in answers:
        if a["status"] == "ok" and a["grade"] not in GRADES:
            errs.append(f"{a['id']}: ok status without a valid grade")
        refused = not a["answer"]
        if refused and not a["reason"]:
            errs.append(f"{a['id']}: refusal without a reason")
        if not refused:
            if not a["citations"]:
                errs.append(f"{a['id']}: answer without citations")
            for c in a["citations"]:
                ok_page = (pages is None or c["paper"] not in pages or
                           1 <= c.get("page_start", 0) <= c.get("page_end", 0) <= pages[c["paper"]])
                if not ok_page:
                    errs.append(f"{a['id']}: citation page outside {c['paper']}")
    return errs


def mock_table(dir_: Path, run_id: str, qs, pages) -> tuple[str, dict]:
    nq = len(qs)
    qids = [q.id for q in qs]
    answerable = [q for q in qs if not q.expect_refusal]
    uns = [q for q in qs if q.expect_refusal]
    mis = [q for q in qs if q.type == "misleading"]
    rows, checks = [], {}
    for s in SETTINGS:
        got = load_run(dir_, run_id, s)
        if got is None:
            rows.append(f"| {s} | not run | | | | | | | | | |")
            continue
        answers, res = got
        checks[s] = integrity(answers, res, qids, pages)
        g = pd.Series([a["grade"] for a in answers])
        by = {a["id"]: a for a in answers}
        n_ans = len(answerable)
        correct = int((g == "correct").sum())
        wrong = int((g == "wrong").sum())
        rr = int((g == "refused_correctly").sum())
        rw = int((g == "refused_wrongly").sum())
        mis_ok = sum(by[q.id]["grade"] == "correct" for q in mis)
        gate = sum(1 for a in answers if a["reason"] == "insufficient_evidence" and not a["expect_refusal"])
        vfail = sum(1 for a in answers if a["reason"] == "verification_failed")
        diag = pd.read_csv(dir_ / f"{run_id}__{s}.diagnostics.csv").set_index("request_id")
        mc = res.request_id.map(diag.model_calls).fillna(0)
        calls = int(mc.sum())
        toks = (res.tokens_in.fillna(0) + res.tokens_out.fillna(0))
        tpa = toks[mc > 0].mean()
        rows.append(f"| {s} | {correct}/{n_ans} ({100 * correct / n_ans:.1f}%) | {wrong}/{n_ans} | "
                    f"{rr}/{len(uns)} | {rw}/{n_ans} (gate-blocked {gate}) | {mis_ok}/{len(mis)} | "
                    f"{calls} | {int(res.retries.sum())} | {vfail} | {int((res.status != 'ok').sum())}/{nq} | {tpa:.0f} |")
    head = ["| Setting | Correct and cited | Wrong | Unsupported refused correctly | Answerable refused wrongly | Misleading handled correctly | Model calls | Retries | Verification failures | Non-ok status | Mean tokens per answered question (mock nominal) |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    return "\n".join(head + rows), checks


def retrieval_table(raw: Path, run: str) -> str:
    f = raw / f"{run}.rows.csv"
    if not f.is_file():
        return "retrieval-only benchmark: not measured"
    d = pd.read_csv(f)
    out = ["| Setting | Hit@k | Full-evidence recall | MRR | Mean context tokens (est.) |", "|---|---|---|---|---|"]
    for label, g in d.groupby("setting", sort=False):
        a = g[g.expect_refusal == False]
        n = len(a)
        out.append(f"| {label} | {int((a.hit == True).sum())}/{n} | {int((a.recall == 1.0).sum())}/{n} | "
                   f"{a.rr.mean():.3f} | {g.context_tokens_est.mean():.0f} |")
    return "\n".join(out)


def build(run_id: str, retrieval_run: str) -> str:
    qs = load_questions(ROOT / "benchmarks" / "questions.jsonl")
    summ = ROOT / "research" / "extracted" / "summary.json"
    pages = {k: v["pages"] for k, v in json.loads(summ.read_text(encoding="utf-8")).items()} if summ.is_file() else None
    mt, checks = mock_table(ROOT / "results" / "mock", run_id, qs, pages)
    real = sorted((ROOT / "results" / "raw").glob("qa-*__*.results.csv"))
    lines = ["# A/B/C comparison (generated by scripts/compare_settings.py)", "",
             f"## 1. Retrieval only, real papers, BM25, no model ({retrieval_run}) [MEASURED, retrieval only]", "",
             retrieval_table(ROOT / "results" / "raw", retrieval_run), "",
             f"## 2. Offline pipeline run with the MOCK extractive provider ({run_id}) [SIMULATED, not an LLM]", "",
             "The mock answers by copying passage sentences, so these grades test plumbing and set an extractive-baseline "
             "floor. They say nothing about Groq answer quality, latency (not meaningful: microseconds) or limits.", "", mt, ""]
    lines += ["### Integrity checks (21 questions per setting, valid status/grade, refusal reasons, citations present and in range)", ""]
    for s, errs in checks.items():
        lines.append(f"- {s}: {'PASS' if not errs else 'FAIL: ' + '; '.join(errs)}")
    lines += ["", "## 3. Real Groq quality results", ""]
    lines.append("not measured in this environment for the corrected pipeline." if not real else
                 "Raw files present: " + ", ".join(p.name for p in real))
    lines += ["", "The earlier real Setting A run (`qa-A-1`) is historical evidence of the pre-fix `budget_exhausted` behaviour; "
              "its raw files were not available to this audit, and the fixed pipeline has not been run against Groq."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="mock-1")
    ap.add_argument("--retrieval-run", default="rb-2")
    a = ap.parse_args()
    out = ROOT / "report" / "ab_comparison.md"
    out.write_text(build(a.run_id, a.retrieval_run), encoding="utf-8", newline="\n")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
