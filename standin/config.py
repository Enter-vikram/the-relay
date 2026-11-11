"""Stand-in configuration with provenance for every parameter.

provenance values:
  observed(n=5)  derived from results/baseline_v2.csv (5 real Groq requests).
                 Treat as a sanity-level estimate, NOT a fitted distribution.
  documented     taken from Groq docs / response headers; plan-specific, may
                 differ from this account.
  assumed        no evidence; a modelling choice so overload behaviour can be
                 exercised. Never present results that depend on these as Groq's.

`StandinConfig.from_baseline()` re-derives every "observed" value from the CSV;
tests/test_standin.py asserts the hard-coded defaults equal that derivation, so
the defaults cannot silently drift from the evidence.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pandas as pd

BASELINE_V2 = Path(__file__).resolve().parent.parent / "results" / "baseline_v2.csv"


@dataclass
class StandinConfig:
    # ---- latency (observed n=5, reasoning model gpt-oss-20b, client in India) ----
    ttfr_median_ms: float = 1360.0        # time to first *reasoning* chunk, median
    ttfr_sigma: float = 0.274             # lognormal sigma of ttfr (sample sd of ln, n=5)
    ms_per_token: float = 2.139           # (content_ttft - first_reasoning)/tokens_out, mean
    content_tail_ms_mean: float = 121.8   # content arrives in a near-burst after reasoning
    tokens_out_median: float = 269.0      # includes hidden reasoning tokens
    tokens_out_sigma: float = 0.573       # lognormal sigma (sample sd of ln, n=5)
    reasoning_fraction: float = 0.523     # share of tokens_out that is reasoning (char-ratio proxy)
    prompt_overhead_tokens: float = 70.0  # template tokens added to any prompt (observed)
    chars_per_token: float = 4.0          # assumed for long prompts (only short ones observed)
    # ---- limits ----
    rpm: int = 30                         # documented (unobserved; docs pages disagree)
    tpm: int = 8000                       # observed header
    rpd: int = 1000                       # observed header
    tpd: int = 200_000                    # documented (unobserved)
    tpm_admit_includes_max_tokens: bool = False   # assumed
    # ---- load behaviour (all assumed: Groq's in-flight limit was NOT measured) ----
    max_inflight: int = 8
    provider_queue_limit: int = 16        # extra requests held before 503 overload
    load_sensitivity: float = 0.5         # latency x(1 + s * inflight/max_inflight)
    recovery_s: float = 5.0               # 503 window after an overload event
    count_rejected_rpm: bool = False       # assumed: a 429-rejected request still spends RPM budget
    # ---- fault injection (default 0: no failure was observed in 5 requests) ----
    p_500: float = 0.0
    p_503: float = 0.0
    p_hang: float = 0.0                   # request never answers (client timeout)
    p_interrupt: float = 0.0              # stream cut before [DONE]
    tail_prob: float = 0.0                # long-tail latency mixture
    tail_multiplier: float = 3.0
    hang_s: float = 120.0
    seed: int = 0
    provenance: dict = field(default_factory=lambda: {
        "ttfr_median_ms": "observed(n=5)", "ttfr_sigma": "observed(n=5)",
        "ms_per_token": "observed(n=5)", "content_tail_ms_mean": "observed(n=5)",
        "tokens_out_median": "observed(n=5)", "tokens_out_sigma": "observed(n=5)",
        "reasoning_fraction": "observed(n=5) proxy",
        "prompt_overhead_tokens": "observed(n=5)", "chars_per_token": "assumed",
        "rpm": "documented", "tpm": "observed header", "rpd": "observed header",
        "tpd": "documented", "tpm_admit_includes_max_tokens": "assumed",
        "max_inflight": "assumed", "provider_queue_limit": "assumed",
        "load_sensitivity": "assumed", "count_rejected_rpm": "assumed (Groq behaviour unknown)", "recovery_s": "assumed",
        "p_500": "assumed", "p_503": "assumed", "p_hang": "assumed", "p_interrupt": "assumed",
        "tail_prob": "assumed", "tail_multiplier": "assumed", "hang_s": "assumed",
        "response_body_shapes": "assumed (no 429 was ever observed)"})

    def with_(self, **kw) -> "StandinConfig":
        return replace(self, **kw)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "StandinConfig":
        d = json.loads(s)
        return cls(**d)

    @classmethod
    def derive_observed(cls, csv_path: Path = BASELINE_V2) -> dict:
        """Recompute every observed-from-baseline parameter."""
        df = pd.read_csv(csv_path)
        ok = df[df["outcome"] == "ok"].copy()
        n = len(ok)
        if n < 2:
            raise ValueError("need at least 2 ok rows to derive")
        ttfr = ok["first_reasoning_ms"].astype(float)
        tout = ok["tokens_out"].astype(float)
        ttft = ok["first_token_ms"].astype(float)
        total = ok["total_ms"].astype(float)
        rc = ok["reasoning_chars"].astype(float)
        cc = ok["content_chars"].astype(float)
        lnsd = lambda s: float(pd.Series([math.log(v) for v in s]).std(ddof=1))
        slope = ((ttft - ttfr) / tout).mean()
        # intercept of tokens_in vs prompt_chars at the smallest prompt (overhead proxy)
        pc = ok["prompt_chars"].astype(float)
        tin = ok["tokens_in"].astype(float)
        b = float(pd.Series(tin).corr(pd.Series(pc)))
        fit = pd.Series(tin).values
        import numpy as np
        coef = np.polyfit(pc.values, tin.values, 1)
        return {
            "ttfr_median_ms": round(float(ttfr.median()), 1),
            "ttfr_sigma": round(lnsd(ttfr), 3),
            "ms_per_token": round(float(slope), 3),
            "content_tail_ms_mean": round(float((total - ttft).mean()), 1),
            "tokens_out_median": round(float(tout.median()), 1),
            "tokens_out_sigma": round(lnsd(tout), 3),
            "reasoning_fraction": round(float((rc / (rc + cc)).mean()), 3),
            "prompt_overhead_tokens": round(float(coef[1]), 0),
            "_n": n,
        }

    @classmethod
    def from_baseline(cls, csv_path: Path = BASELINE_V2, **overrides) -> "StandinConfig":
        d = cls.derive_observed(csv_path)
        d.pop("_n")
        return cls(**{**d, **overrides})
