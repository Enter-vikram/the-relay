"""Summarize a baseline CSV (default: results/baseline_v2.csv).

    python -m experiments.summarize_baseline
    python -m experiments.summarize_baseline --file results/baseline.csv   (v1, old schema)

Latency percentiles use only outcome == ok rows; every other outcome is
counted separately so failures are never hidden inside a latency number.
"""
import argparse
from pathlib import Path

import pandas as pd

from app.config import settings


def stats(label: str, s: pd.Series) -> None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s):
        print(f"{label}: n={len(s)} min={s.min():.1f} p50={s.quantile(.5):.1f} "
              f"p95={s.quantile(.95):.1f} max={s.max():.1f} mean={s.mean():.1f}")
    else:
        print(f"{label}: not measured")


def main(path: Path) -> None:
    df = pd.read_csv(path)
    print("file:", path, "| rows:", len(df))
    print("outcomes:", df["outcome"].value_counts().to_dict())
    ok = df[df["outcome"] == "ok"]
    stats("first_token_ms", ok["first_token_ms"])
    if "first_reasoning_ms" in ok:
        stats("first_reasoning_ms", ok["first_reasoning_ms"])
    stats("total_ms", ok["total_ms"])
    stats("tokens_out (incl. reasoning tokens)", ok["tokens_out"])
    if "finish_reason" in df:
        print("finish_reason:", df["finish_reason"].fillna("").value_counts().to_dict())
    print("\nlimit headers seen (distinct):")
    for c in ("rl_limit_requests", "rl_limit_tokens"):
        print(" ", c, sorted(df[c].dropna().unique().tolist()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=settings.results_dir / "baseline_v2.csv")
    main(ap.parse_args().file)
