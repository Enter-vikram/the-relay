"""Derive limit evidence and latency summaries from results/baseline_v2.csv.

    python -m experiments.calibrate_from_baseline
    python -m experiments.calibrate_from_baseline --file results/baseline_v2.csv --out results/calibration_baseline_v2.json

READ-ONLY on the raw CSV. The JSON it writes is a DERIVED file (safe to
regenerate). Every number is labelled as observed (read straight from the
data) or inferred (arithmetic on observed headers), plus the sample size.

Header semantics (Groq docs): x-ratelimit-*-requests refer to the DAILY request
budget (RPD); x-ratelimit-*-tokens refer to the PER-MINUTE token budget (TPM).
No header exposes the per-minute request limit (RPM) or the daily token limit.
"""
import argparse
import json
import math
import re
from pathlib import Path

import pandas as pd

from app.config import settings

_DUR = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?$")
SECONDS_PER_DAY = 86_400.0


def parse_duration(s) -> float | None:
    """'1m26.4s' -> 86.4, '2.107s' -> 2.107, '7m12s' -> 432, '120ms' -> 0.12, '' -> None."""
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return None
    s = str(s).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?ms", s):
        return float(s[:-2]) / 1000.0
    m = _DUR.match(s)
    if not m or not any(m.groups()):
        raise ValueError(f"unparseable duration: {s!r}")
    h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def _stats(s: pd.Series) -> dict:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"n": 0}
    return {"n": int(len(s)), "min": round(float(s.min()), 1),
            "p50": round(float(s.quantile(0.5)), 1), "p95": round(float(s.quantile(0.95)), 1),
            "max": round(float(s.max()), 1), "mean": round(float(s.mean()), 1)}


def _consistent(values: list[float], expected: float, tol: float) -> bool:
    return bool(values) and all(abs(v - expected) <= tol * expected for v in values)


def analyze(df: pd.DataFrame) -> dict:
    ok = df[df["outcome"] == "ok"]
    out: dict = {
        "rows": int(len(df)),
        "outcomes": {k: int(v) for k, v in df["outcome"].value_counts().items()},
        "warning": ("n<30: percentiles are a sanity check, not a distribution; "
                    "do not fit stand-in latency distributions to this sample")
        if len(ok) < 30 else "",
        "latency_ms_ok_rows": {
            "content_ttft": _stats(ok["first_token_ms"]),
            "first_reasoning": _stats(ok["first_reasoning_ms"]),
            "total": _stats(ok["total_ms"]),
            "after_first_content": _stats(pd.to_numeric(ok["total_ms"], errors="coerce")
                                          - pd.to_numeric(ok["first_token_ms"], errors="coerce")),
        },
        "tokens_ok_rows": {"tokens_in": _stats(ok["tokens_in"]),
                           "tokens_out_incl_reasoning": _stats(ok["tokens_out"])},
        "finish_reasons": {str(k): int(v) for k, v in
                           df["finish_reason"].fillna("").value_counts().items()},
    }

    # ---- daily request budget (RPD) ----
    lim_r = pd.to_numeric(df["rl_limit_requests"], errors="coerce")
    rem_r = pd.to_numeric(df["rl_remaining_requests"], errors="coerce")
    rst_r = df["rl_reset_requests"].map(parse_duration)
    rows = []
    for lim, rem, rst in zip(lim_r, rem_r, rst_r):
        if pd.notna(lim) and pd.notna(rem) and rst is not None and lim - rem > 0:
            rows.append((lim, lim - rem, rst))
    rpd: dict = {"observed_limit_header": sorted({int(x) for x in lim_r.dropna()}),
                 "n_rows_used": len(rows)}
    if rows:
        per_req = [rst / used for _, used, rst in rows]
        limit = rows[0][0]
        expected = SECONDS_PER_DAY / limit
        rpd.update(seconds_per_request_refill=[round(x, 3) for x in per_req],
                   expected_if_limit_is_per_day=round(expected, 3),
                   consistent_with_continuous_daily_refill=_consistent(per_req, expected, 0.01),
                   inference="reset-requests == (limit - remaining) * 86400/limit seconds "
                             "=> a daily budget that refills continuously")
    out["requests_per_day"] = rpd

    # ---- per-minute token budget (TPM) ----
    lim_t = pd.to_numeric(df["rl_limit_tokens"], errors="coerce")
    rem_t = pd.to_numeric(df["rl_remaining_tokens"], errors="coerce")
    rst_t = df["rl_reset_tokens"].map(parse_duration)
    trows = []
    for lim, rem, rst in zip(lim_t, rem_t, rst_t):
        if pd.notna(lim) and pd.notna(rem) and rst and lim - rem > 0:
            trows.append((lim, (lim - rem) / rst))
    tpm: dict = {"observed_limit_header": sorted({int(x) for x in lim_t.dropna()}),
                 "n_rows_used": len(trows)}
    if trows:
        rates = [r for _, r in trows]
        expected = trows[0][0] / 60.0
        tpm.update(implied_refill_tokens_per_s=[round(x, 1) for x in rates],
                   expected_if_limit_is_per_minute=round(expected, 1),
                   consistent_with_continuous_per_minute_refill=_consistent(rates, expected, 0.02),
                   inference="(limit - remaining) / reset-tokens ~= limit/60 tokens/s => "
                             "a per-minute token bucket refilling continuously")
        mean_tokens = float((pd.to_numeric(ok["tokens_in"]) + pd.to_numeric(ok["tokens_out"])).mean()) \
            if len(ok) else float("nan")
        if mean_tokens == mean_tokens and mean_tokens > 0:
            tpm["tokens_per_request_mean_ok_rows"] = round(mean_tokens, 1)
            tpm["max_requests_per_min_by_tpm_at_that_size"] = round(trows[0][0] / mean_tokens, 1)
    out["tokens_per_minute"] = tpm
    out["not_observable_from_headers"] = [
        "requests per minute (RPM)", "tokens per day (TPD)",
        "requests in flight at once", "behaviour on 429 and recovery time (needs a 429)"]
    return out


def main(path: Path, out_path: Path) -> None:
    df = pd.read_csv(path)
    res = analyze(df)
    res["source_file"] = str(path)
    out_path.write_text(json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"source: {path}  rows={res['rows']}  outcomes={res['outcomes']}")
    if res["warning"]:
        print("WARNING:", res["warning"])
    for k, v in res["latency_ms_ok_rows"].items():
        print(f"  {k:<20} {v}")
    r, t = res["requests_per_day"], res["tokens_per_minute"]
    print("\n[RPD] limit header(s):", r["observed_limit_header"], "(OBSERVED header value)")
    if "seconds_per_request_refill" in r:
        print("  refill s/request:", r["seconds_per_request_refill"], "expected:",
              r["expected_if_limit_is_per_day"], "-> consistent:",
              r["consistent_with_continuous_daily_refill"], "(INFERENCE)")
    print("[TPM] limit header(s):", t["observed_limit_header"], "(OBSERVED header value)")
    if "implied_refill_tokens_per_s" in t:
        print("  implied refill tok/s:", t["implied_refill_tokens_per_s"], "expected:",
              t["expected_if_limit_is_per_minute"], "-> consistent:",
              t["consistent_with_continuous_per_minute_refill"], "(INFERENCE)")
    if "max_requests_per_min_by_tpm_at_that_size" in t:
        print(f"  at {t['tokens_per_request_mean_ok_rows']} tokens/request the TPM budget allows "
              f"~{t['max_requests_per_min_by_tpm_at_that_size']} requests/min")
    print("not observable from headers:", "; ".join(res["not_observable_from_headers"]))
    print("derived file written:", out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=settings.results_dir / "baseline_v2.csv")
    ap.add_argument("--out", type=Path, default=settings.results_dir / "calibration_baseline_v2.json")
    a = ap.parse_args()
    main(a.file, a.out)
