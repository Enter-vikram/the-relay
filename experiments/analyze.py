"""Build results/results.csv (+ diagnostics.csv) from the immutable raw files and compute every report table
FROM THOSE TWO FILES ONLY. Nothing in the tables comes from memory or from the stand-in config.

    python -m experiments.analyze --build         # merge raw -> results.csv, then write tables
    python -m experiments.analyze --tables-only   # recompute tables from the existing results.csv

Inputs  : results/raw/*.results.csv + *.diagnostics.csv (load tests, quality evaluation),
          results/baseline_v2.csv (the 5 real baseline requests, normalised; baseline.csv v1 is NOT merged because its
          TTFT counts reasoning chunks and is not comparable),
          docs/documented_limits.json (prices and documented limits), results/calibration_baseline_v2.json.
Outputs : results/results.csv, results/diagnostics.csv, results/raw_manifest.json (sha256 of every raw file),
          results/experiment_summary.json, report/tables.md.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

from app.metrics import DIAG_COLUMNS, RESULT_COLUMNS, Record, append_records, percentile

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
RAW = RES / "raw"
NM = "not measured"
STATUS_OF_BASELINE = {"ok": "ok", "rate_limited": "rate_limited", "timeout": "timeout"}


# ------------------------------------------------------------------ merge
def normalize_baseline(path: Path) -> list[Record]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    num = lambda v: float(v) if v != "" else None
    out = []
    for _, r in df.iterrows():
        out.append(Record(
            request_id=f"{r['run_id']}-{int(r['seq']):04d}", scenario="real-baseline-v2", source="real",
            start_time=r["start_time_utc"], status=STATUS_OF_BASELINE.get(r["outcome"], "error"),
            first_token_ms=num(r["first_token_ms"]), total_ms=num(r["total_ms"]), retries=0,
            tokens_in=int(r["tokens_in"]) if r["tokens_in"] != "" else None,
            tokens_out=int(r["tokens_out"]) if r["tokens_out"] != "" else None,
            final_reason=r["outcome"], attempts=1, model_ttft_ms=num(r["first_token_ms"]),
            model_total_ms=num(r["total_ms"]), run_id=r["run_id"]))
    return out


def rel(p: Path) -> str:
    for base in (ROOT, RES.parent):
        try:
            return str(p.relative_to(base))
        except ValueError:
            continue
    return p.name


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_results_csv() -> dict:
    res_p, diag_p = RES / "results.csv", RES / "diagnostics.csv"
    for p in (res_p, diag_p):
        if p.exists():
            p.unlink()                      # derived files: safe to rebuild; raw files are never touched
    manifest: dict = {}
    n_total = 0
    base = RES / "baseline_v2.csv"
    if base.is_file():
        recs = normalize_baseline(base)
        append_records(recs, res_p, diag_p)
        manifest[rel(base)] = sha256(base)
        n_total += len(recs)
    sup_p = RAW / "SUPERSEDED.json"          # raw files stay on disk; listed ones are excluded from derived data
    superseded = json.loads(sup_p.read_text(encoding="utf-8")) if sup_p.is_file() else {}
    for rp in sorted(RAW.glob("*.results.csv")):
        if rp.name.replace(".results.csv", "") in superseded:
            manifest[rel(rp)] = sha256(rp) + "  (SUPERSEDED, excluded: " + superseded[rp.name.replace(".results.csv", "")] + ")"
            continue
        dp = Path(str(rp).replace(".results.csv", ".diagnostics.csv"))
        if not dp.is_file():
            raise FileNotFoundError(f"{dp} missing for {rp.name}")
        rrows = list(csv.DictReader(rp.open(encoding="utf-8")))
        drows = list(csv.DictReader(dp.open(encoding="utf-8")))
        if [r["request_id"] for r in rrows] != [d["request_id"] for d in drows]:
            raise ValueError(f"{rp.name}: results/diagnostics request ids differ")
        for path, rows, cols, dest in ((rp, rrows, RESULT_COLUMNS, res_p), (dp, drows, DIAG_COLUMNS, diag_p)):
            new = not dest.exists()
            with dest.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                if new:
                    w.writeheader()
                w.writerows(rows)
        manifest[rel(rp)] = sha256(rp)
        manifest[rel(dp)] = sha256(dp)
        n_total += len(rrows)
    (RES / "raw_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"rows": n_total, "files": len(manifest)}


# ------------------------------------------------------------------ helpers
def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(RES / "results.csv")
    dg = pd.read_csv(RES / "diagnostics.csv")
    df["t0"] = pd.to_datetime(df["start_time"], utc=True, format="ISO8601")
    return df, dg


def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.1f}%" if d else NM


def ms(x) -> str:
    return NM if x is None or pd.isna(x) else f"{x:.0f}"


def triple(values, n_min_p99: int = 100) -> tuple[str, str, str, bool]:
    xs = [v for v in values if not pd.isna(v)]
    if not xs:
        return NM, NM, NM, False
    return ms(percentile(xs, 50)), ms(percentile(xs, 95)), ms(percentile(xs, 99)), len(xs) < n_min_p99


def pricing() -> dict:
    return json.loads((ROOT / "docs" / "documented_limits.json").read_text(encoding="utf-8"))["pricing_usd_per_1M_tokens"]


def cost_per_1000(ok: pd.DataFrame) -> str:
    ok = ok.dropna(subset=["tokens_in", "tokens_out"])
    if ok.empty:
        return NM
    p = pricing()
    per = (ok["tokens_in"].mean() * p["input"] + ok["tokens_out"].mean() * p["output"]) / 1e6
    return f"${per * 1000:.2f}"


def scenario_order(df: pd.DataFrame) -> list[str]:
    pref = ["real-baseline-v2", "steady", "burst", "overload", "limit_halved", "daily_exhausted"]
    names = list(dict.fromkeys(df["scenario"]))
    return [s for s in pref if s in names] + sorted(s for s in names if s not in pref and not s.startswith("quality-")
                                                    and not s.startswith("retry_")) \
        + sorted(s for s in names if s.startswith("retry_"))


# ------------------------------------------------------------------ tables
def table_2a(df: pd.DataFrame) -> tuple[list[dict], bool]:
    rows, any_small = [], False
    for sc in scenario_order(df):
        d = df[df["scenario"] == sc]
        if d.empty:
            continue
        ok = d[d["status"] == "ok"]
        end = (d["t0"] + pd.to_timedelta(d["total_ms"].fillna(0), unit="ms")).max()
        dur = (end - d["t0"].min()).total_seconds()
        span = (d["t0"].max() - d["t0"].min()).total_seconds()
        offered = f"{len(d) / span * 60:.1f}" if span > 0 and len(d) > 1 else NM
        f50, f95, f99, s1 = triple(ok["first_token_ms"])
        t50, t95, t99, s2 = triple(ok["total_ms"])
        any_small |= s1 or s2
        rows.append({"scenario": sc, "source": d["source"].iloc[0], "requests": len(d), "duration_s": f"{dur:.0f}",
                     "offered_per_min": offered, "ttft": (f50, f95, f99), "total": (t50, t95, t99),
                     "n_ok": len(ok), "p99_unstable": s1 or s2})
    return rows, any_small


def table_2b(df: pd.DataFrame, dg: pd.DataFrame) -> list[dict]:
    m = df.merge(dg[["request_id", "queue_len_at_submit", "attempts"]], on="request_id", how="left")
    rows = []
    for sc in scenario_order(df):
        d = m[m["scenario"] == sc]
        if d.empty:
            continue
        n, ok = len(d), d[d["status"] == "ok"]
        tok = ok.dropna(subset=["tokens_in", "tokens_out"])
        tpa = f"{(tok['tokens_in'] + tok['tokens_out']).mean():.0f}" if len(tok) else NM
        peak_q = d["queue_len_at_submit"].max()
        rows.append({"scenario": sc, "source": d["source"].iloc[0], "n": n,
                     "ok": pct((d["status"] == "ok").sum(), n), "rate_limited": pct((d["status"] == "rate_limited").sum(), n),
                     "timeout": pct((d["status"] == "timeout").sum(), n), "error": pct((d["status"] == "error").sum(), n),
                     "retries_per_request": f"{d['retries'].mean():.2f}",
                     "peak_queue": NM if pd.isna(peak_q) else f"{int(peak_q)}", "tokens_per_answer": tpa,
                     "cost_per_1000": cost_per_1000(ok) + (" (simulated)" if d["source"].iloc[0] == "stand-in"
                                                          else " (hypothetical; free tier billed $0)")})
    return rows


def table_3(df: pd.DataFrame) -> list[dict]:
    q = df[df["scenario"].str.startswith("quality-") & (df["source"] == "real")]
    rows = []
    for setting in sorted(q["retrieval_setting"].dropna().unique()):
        d = q[q["retrieval_setting"] == setting]
        n = len(d)
        g = d["answer_grade"].value_counts()
        ok = d[d["status"] == "ok"]
        f50, f95, _, _ = triple(ok["first_token_ms"])
        t50, t95, _, _ = triple(ok["total_ms"])
        tok = ok.dropna(subset=["tokens_in", "tokens_out"])
        rows.append({"setting": setting, "questions": n, "graded": int(d["answer_grade"].notna().sum()),
                     "correct": pct(g.get("correct", 0), n), "wrong": pct(g.get("wrong", 0), n),
                     "refused_correctly": pct(g.get("refused_correctly", 0), n),
                     "refused_wrongly": pct(g.get("refused_wrongly", 0), n),
                     "ungraded": int(d["answer_grade"].isna().sum()),
                     "tokens_per_answer": f"{(tok['tokens_in'] + tok['tokens_out']).mean():.0f}" if len(tok) else NM,
                     "ttft": (f50, f95), "total": (t50, t95)})
    return rows


def breakdown(df: pd.DataFrame, dg: pd.DataFrame, scenario: str) -> dict | None:
    d = df[(df["scenario"] == scenario) & (df["status"] == "ok")]
    if d.empty:
        return None
    slow = d.sort_values("total_ms").iloc[-1]
    g = dg[dg["request_id"] == slow["request_id"]].iloc[0]
    z = lambda v: 0.0 if pd.isna(v) else float(v)
    total, ttft = float(slow["total_ms"]), float(slow["first_token_ms"])
    comp = {"queue wait": z(g["queue_wait_ms"]), "limiter pacing wait": z(g["limiter_wait_ms"]),
            "retry delays": z(g["retry_delay_ms"]),
            "model wait before first token": z(g["model_ttft_ms"]),
            "model generation after first token": z(g["model_total_ms"]) - z(g["model_ttft_ms"])}
    attributed = sum(comp.values())
    comp["unattributed (earlier failed attempts, loop overhead)"] = total - attributed
    return {"scenario": scenario, "request_id": slow["request_id"], "total_ms": total, "first_token_ms": ttft,
            "after_first_token_ms": total - ttft, "attempts": int(g["attempts"]), "components_ms": comp,
            "retrieval/verification": "not applicable: gateway-level load test has no retrieval or verification step"}


def retry_table(df: pd.DataFrame, dg: pd.DataFrame) -> list[dict]:
    m = df.merge(dg[["request_id", "attempts"]], on="request_id", how="left")
    rows = []
    for sc in [s for s in scenario_order(df) if s.startswith("retry_")]:
        d = m[m["scenario"] == sc]
        ok = d[d["status"] == "ok"]
        rows.append({"scenario": sc, "n": len(d), "ok": pct(len(ok), len(d)),
                     "rate_limited": pct((d["status"] == "rate_limited").sum(), len(d)),
                     "timeout": pct((d["status"] == "timeout").sum(), len(d)),
                     "provider_attempts_per_request": f"{d['attempts'].mean():.2f}",
                     "attempts_per_ok_answer": f"{d['attempts'].sum() / len(ok):.1f}" if len(ok) else NM,
                     "ok_total_p50_ms": ms(percentile(list(ok["total_ms"]), 50)) if len(ok) else NM})
    return rows


# ------------------------------------------------------------------ rendering
def md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def limits_rows() -> list[list]:
    cal = json.loads((RES / "calibration_baseline_v2.json").read_text(encoding="utf-8"))
    doc = json.loads((ROOT / "docs" / "documented_limits.json").read_text(encoding="utf-8"))
    tpm, rpd = cal["tokens_per_minute"], cal["requests_per_day"]
    probe = RES / "probe_429.csv"
    seen429 = None
    if probe.is_file():
        pr = pd.read_csv(probe, dtype=str, keep_default_na=False)
        h = pr[pr["status"] == "429"]
        if len(h):
            seen429 = h.iloc[0]
    rows = [
        ["Requests per minute", f"{doc['rate_limits_page']['rpm']} documented; not observed",
         "Groq docs (rate-limits page, 2026-11-10). No response header exposes RPM; the models page lists 1,000 (conflict)", "low"],
        ["Tokens per minute", f"{tpm['observed_limit_header'][0]:,} (limit header, {tpm['n_rows_used']}/5 responses)",
         "x-ratelimit-limit-tokens in baseline_v2.csv; remaining/reset headers imply a bucket refilling "
         f"~{tpm['expected_if_limit_is_per_minute']} tokens/s", "high (limit), medium (refill model)"],
        ["Requests per day", f"{rpd['observed_limit_header'][0]:,} (limit header, {rpd['n_rows_used']}/5 responses)",
         "x-ratelimit-limit-requests; reset header = used x 86.4 s, i.e. continuous refill (inferred)", "high (limit), medium (refill model)"],
        ["Tokens per day", f"{doc['rate_limits_page']['tpd']:,} documented; not observed",
         "Groq docs only; no header exposes the daily token budget; tracked locally by the usage ledger", "low"],
        ["Requests in flight at once", NM, "deliberately not probed (would risk the free-tier quota); stand-in uses an ASSUMED 8", "n/a"],
    ]
    if seen429 is not None:
        rows.append(["What it returns when a limit is hit", f"HTTP 429, retry-after: {seen429['retry_after']} s, body: {seen429['error_body'][:90]}",
                     "directly observed by experiments/probe_429.py", "high"])
        rows.append(["Time until it recovers", f"retry-after {seen429['retry_after']} s (observed on that 429)",
                     "probe_429.csv; recovery after waiting not separately timed", "medium"])
    else:
        rows.append(["What it returns when a limit is hit", "not observed (0 of 5 baseline requests were limited). Docs: HTTP 429 + retry-after",
                     "documented only; run experiments/probe_429.py to observe; stand-in body text is ASSUMED", "low"])
        rows.append(["Time until it recovers", "inferred from headers: tokens (limit-remaining)/133.3 s; requests 86.4 s per request",
                     "arithmetic on baseline_v2.csv reset headers; never observed after a real 429", "low-medium"])
    return rows


def render_tables(df, dg) -> str:
    t2a, small = table_2a(df)
    t2b, t3 = table_2b(df, dg), table_3(df)
    parts = ["## Table 1: limits found in the real service", md_table(["Limit", "Value found", "How it was found", "Confidence"], limits_rows()), ""]
    parts += ["## Table 2a: latency (ms; TTFT and total over successful requests only)",
              md_table(["Scenario", "Real/stand-in", "Requests", "Duration s", "Offered/min", "TTFT p50/p95/p99", "Total p50/p95/p99", "n ok"],
                       [[r["scenario"], r["source"], r["requests"], r["duration_s"], r["offered_per_min"],
                         " / ".join(r["ttft"]) + ("†" if r["p99_unstable"] else ""),
                         " / ".join(r["total"]) + ("†" if r["p99_unstable"] else ""), r["n_ok"]] for r in t2a])]
    if small:
        parts.append("† fewer than 100 successful requests: p99 is interpolated between the largest observations and is not a stable estimate.")
    parts += ["", "## Table 2b: outcomes",
              md_table(["Scenario", "Source", "n", "Succeeded", "Rate-limited", "Timed out / given up", "Other error", "Retries/request",
                        "Peak queue (at submit)", "Tokens/answer", "Cost/1,000 answers"],
                       [[r["scenario"], r["source"], r["n"], r["ok"], r["rate_limited"], r["timeout"], r["error"], r["retries_per_request"],
                         r["peak_queue"], r["tokens_per_answer"], r["cost_per_1000"]] for r in t2b if not r["scenario"].startswith("retry_")]), ""]
    rt = retry_table(df, dg)
    if rt:
        parts += ["## Retry strategies (stand-in; assumption: rejected attempts count against RPM)",
                  md_table(["Scenario", "n", "Succeeded", "Rate-limited", "Timed out", "Provider attempts/request", "Attempts per ok answer", "OK total p50 ms"],
                           [[r["scenario"], r["n"], r["ok"], r["rate_limited"], r["timeout"], r["provider_attempts_per_request"],
                             r["attempts_per_ok_answer"], r["ok_total_p50_ms"]] for r in rt]), ""]
    parts += ["## Table 3: retrieval settings (real Groq, graded against gold evidence)"]
    if t3:
        parts.append(md_table(["Setting", "Questions", "Correct + cited", "Wrong", "Correctly refused", "Refused wrongly", "Ungraded (failed)",
                               "Tokens/answer", "TTFT p50/p95", "Total p50/p95"],
                              [[r["setting"], r["questions"], r["correct"], r["wrong"], r["refused_correctly"], r["refused_wrongly"], r["ungraded"],
                                r["tokens_per_answer"], " / ".join(r["ttft"]), " / ".join(r["total"])] for r in t3]))
    else:
        parts.append("not measured: needs benchmarks/questions.jsonl authored from the real PDFs and a real-Groq run "
                     "(python -m experiments.quality_evaluation).")
    return "\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--tables-only", action="store_true")
    ap.add_argument("--breakdown", default="burst")
    a = ap.parse_args()
    if a.build:
        print("merged:", build_results_csv())
    df, dg = load()
    text = render_tables(df, dg)
    bd = breakdown(df, dg, a.breakdown)
    (ROOT / "report").mkdir(exist_ok=True)
    (ROOT / "report" / "tables.md").write_text(text, encoding="utf-8")
    summary = {"table_2a": table_2a(df)[0], "table_2b": table_2b(df, dg), "table_3": table_3(df), "retry": retry_table(df, dg),
               "latency_breakdown_slowest_request": bd, "rows": int(len(df))}
    (RES / "experiment_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(text)
    if bd:
        print("slowest-request breakdown:", json.dumps(bd, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
