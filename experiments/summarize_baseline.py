"""Summarize results/baseline.csv: latency percentiles + observed limit headers."""
import pandas as pd

from app.config import settings


def main() -> None:
    df = pd.read_csv(settings.results_dir / "baseline.csv")
    print("rows:", len(df), "| outcomes:", df["outcome"].value_counts().to_dict())
    ok = df[df["outcome"] == "ok"]
    for col in ("first_token_ms", "total_ms", "tokens_out"):
        s = pd.to_numeric(ok[col], errors="coerce").dropna()
        if len(s):
            print(f"{col}: n={len(s)} min={s.min():.1f} p50={s.quantile(.5):.1f} "
                  f"p95={s.quantile(.95):.1f} max={s.max():.1f} mean={s.mean():.1f}")
    if len(ok):
        gen = (pd.to_numeric(ok["total_ms"]) - pd.to_numeric(ok["first_token_ms"])) / 1000
        tps = pd.to_numeric(ok["tokens_out"]) / gen
        print(f"decode tokens/s (after first token): median={tps.median():.0f}")
    print("\nlimit headers seen (distinct):")
    for c in ("rl_limit_requests", "rl_limit_tokens"):
        print(" ", c, sorted(df[c].dropna().unique().tolist()))


if __name__ == "__main__":
    main()
