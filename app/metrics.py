"""Percentiles, per-request records and the results.csv / diagnostics.csv writers.

results.csv  : exactly the 12 required columns, one row per request.
diagnostics.csv : same request_id plus everything else (stage timings, reasons).
Missing values are EMPTY cells (never 0, never a guess).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field, fields
from pathlib import Path

RESULT_COLUMNS = ["request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms",
                  "status", "retries", "tokens_in", "tokens_out", "retrieval_setting",
                  "answer_grade"]
SOURCES = {"real", "stand-in"}
STATUSES = {"ok", "rate_limited", "timeout", "error"}
GRADES = {"correct", "wrong", "refused_correctly", "refused_wrongly"}

DIAG_COLUMNS = ["request_id", "scenario", "final_reason", "priority", "attempts", "queue_wait_ms",
                "limiter_wait_ms", "model_ttft_ms", "model_total_ms", "retry_delay_ms",
                "est_tokens_in", "queue_len_at_submit", "stage_retrieval_ms",
                "stage_verification_ms", "stage_generation_ms", "model_calls", "user_message",
                "run_id", "seed"]


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy default). q in [0, 100]. None if empty."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def summarize(values: list[float]) -> dict:
    xs = [v for v in values if v is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "min": min(xs), "mean": sum(xs) / len(xs), "max": max(xs),
            "p50": percentile(xs, 50), "p95": percentile(xs, 95), "p99": percentile(xs, 99)}


@dataclass
class Record:
    """One request. None = not measured / not applicable (written as an empty cell)."""
    request_id: str
    scenario: str
    source: str
    start_time: str
    status: str
    first_token_ms: float | None = None
    total_ms: float | None = None
    retries: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    retrieval_setting: str | None = None
    answer_grade: str | None = None
    # diagnostics
    final_reason: str = ""
    priority: int | None = None
    attempts: int = 1
    queue_wait_ms: float | None = None
    limiter_wait_ms: float | None = None
    model_ttft_ms: float | None = None
    model_total_ms: float | None = None
    retry_delay_ms: float | None = None
    est_tokens_in: int | None = None
    queue_len_at_submit: int | None = None
    stage_retrieval_ms: float | None = None
    stage_verification_ms: float | None = None
    stage_generation_ms: float | None = None
    model_calls: int | None = None
    user_message: str = ""
    run_id: str = ""
    seed: int | None = None

    def validate(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"bad source {self.source!r}")
        if self.status not in STATUSES:
            raise ValueError(f"bad status {self.status!r}")
        if self.answer_grade is not None and self.answer_grade not in GRADES:
            raise ValueError(f"bad answer_grade {self.answer_grade!r}")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 1)
    return v


def append_records(records: list[Record], results_path: Path, diag_path: Path) -> None:
    """Append-only. Refuses to write if an existing file has a different header."""
    for path, cols in ((results_path, RESULT_COLUMNS), (diag_path, DIAG_COLUMNS)):
        path.parent.mkdir(parents=True, exist_ok=True)
        has = path.exists() and path.stat().st_size > 0
        if has:
            with path.open(newline="", encoding="utf-8") as f:
                if next(csv.reader(f), []) != cols:
                    raise ValueError(f"{path}: header mismatch; refusing to append")
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            if not has:
                w.writeheader()
            for r in records:
                r.validate()
                w.writerow({c: _cell(getattr(r, c)) for c in cols})


ALL_FIELDS = [f.name for f in fields(Record)]
