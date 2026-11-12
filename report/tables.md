## Table 1: limits found in the real service
| Limit | Value found | How it was found | Confidence |
|---|---|---|---|
| Requests per minute | 30 documented; not observed | Groq docs (rate-limits page, 2026-11-10). No response header exposes RPM; the models page lists 1,000 (conflict) | low |
| Tokens per minute | 8,000 (limit header, 5/5 responses) | x-ratelimit-limit-tokens in baseline_v2.csv; remaining/reset headers imply a bucket refilling ~133.3 tokens/s | high (limit), medium (refill model) |
| Requests per day | 1,000 (limit header, 5/5 responses) | x-ratelimit-limit-requests; reset header = used x 86.4 s, i.e. continuous refill (inferred) | high (limit), medium (refill model) |
| Tokens per day | 200,000 documented; not observed | Groq docs only; no header exposes the daily token budget; tracked locally by the usage ledger | low |
| Requests in flight at once | not measured | deliberately not probed (would risk the free-tier quota); stand-in uses an ASSUMED 8 | n/a |
| What it returns when a limit is hit | not observed (0 of 5 baseline requests were limited). Docs: HTTP 429 + retry-after | documented only; run experiments/probe_429.py to observe; stand-in body text is ASSUMED | low |
| Time until it recovers | inferred from headers: tokens (limit-remaining)/133.3 s; requests 86.4 s per request | arithmetic on baseline_v2.csv reset headers; never observed after a real 429 | low-medium |

## Table 2a: latency (ms; TTFT and total over successful requests only)
| Scenario | Real/stand-in | Requests | Duration s | Offered/min | TTFT p50/p95/p99 | Total p50/p95/p99 | n ok |
|---|---|---|---|---|---|---|---|
| real-baseline-v2 | real | 5 | 24 | 13.5 | 2359 / 2616 / 2623† | 2359 / 2838 / 2880† | 5 |
| steady | stand-in | 28 | 462 | 3.6 | 2005 / 2827 / 2940† | 2123 / 3031 / 3215† | 28 |
| burst | stand-in | 32 | 331 | 5.8 | 2532 / 14699 / 22357† | 2574 / 14962 / 22576† | 17 |
| overload | stand-in | 60 | 319 | 12.2 | 7473 / 24442 / 28470† | 7528 / 24505 / 28522† | 29 |
| limit_halved | stand-in | 21 | 340 | 3.7 | 2565 / 19408 / 24130† | 2600 / 19504 / 24202† | 17 |
| daily_exhausted | stand-in | 30 | 290 | 6.2 | 1995 / 2541 / 2560† | 2170 / 2725 / 2848† | 12 |
| fault_5xx | stand-in | 21 | 341 | 3.7 | 2189 / 3075 / 3223† | 2263 / 3242 / 3265† | 21 |
| fault_incomplete | stand-in | 21 | 341 | 3.7 | 2164 / 4710 / 5757† | 2291 / 4805 / 5960† | 21 |
| fault_mixed | stand-in | 21 | 344 | 3.7 | 2427 / 5951 / 19425† | 2530 / 6092 / 19475† | 20 |
| fault_timeout | stand-in | 21 | 341 | 3.7 | 2018 / 21890 / 22325† | 2204 / 21925 / 22420† | 21 |
| retry_exponential | stand-in | 120 | 194 | 40.3 | 2178 / 3128 / 4665† | 2312 / 3422 / 4758† | 24 |
| retry_fixed | stand-in | 120 | 184 | 40.3 | 2176 / 3128 / 5822† | 2310 / 3421 / 5913† | 24 |
| retry_hardened | stand-in | 120 | 200 | 40.3 | 14209 / 26314 / 29260† | 14437 / 26780 / 29528† | 51 |
| retry_jitter | stand-in | 120 | 189 | 40.3 | 2185 / 3099 / 3137† | 2319 / 3271 / 3410† | 24 |
| retry_naive_immediate | stand-in | 120 | 179 | 40.3 | 2153 / 3015 / 3118† | 2330 / 3270 / 3411† | 23 |
| retry_naive_immediate_nocount | stand-in | 120 | 179 | 40.3 | 2152 / 3231 / 3381† | 2333 / 3467 / 3539† | 55 |
† fewer than 100 successful requests: p99 is interpolated between the largest observations and is not a stable estimate.

## Table 2b: outcomes
| Scenario | Source | n | Succeeded | Rate-limited | Timed out / given up | Other error | Retries/request | Peak queue (at submit) | Tokens/answer | Cost/1,000 answers |
|---|---|---|---|---|---|---|---|---|---|---|
| real-baseline-v2 | real | 5 | 100.0% | 0.0% | 0.0% | 0.0% | 0.00 | not measured | 383 | $0.10 (hypothetical; free tier billed $0) |
| steady | stand-in | 28 | 100.0% | 0.0% | 0.0% | 0.0% | 0.00 | 0 | 1545 | $0.17 (simulated) |
| burst | stand-in | 32 | 53.1% | 46.9% | 0.0% | 0.0% | 0.00 | 13 | 1545 | $0.17 (simulated) |
| overload | stand-in | 60 | 48.3% | 51.7% | 0.0% | 0.0% | 0.00 | 1 | 1568 | $0.18 (simulated) |
| limit_halved | stand-in | 21 | 81.0% | 19.0% | 0.0% | 0.0% | 0.43 | 0 | 1522 | $0.16 (simulated) |
| daily_exhausted | stand-in | 30 | 40.0% | 60.0% | 0.0% | 0.0% | 0.00 | 0 | 1561 | $0.18 (simulated) |
| fault_5xx | stand-in | 21 | 100.0% | 0.0% | 0.0% | 0.0% | 0.29 | 0 | 1619 | $0.19 (simulated) |
| fault_incomplete | stand-in | 21 | 100.0% | 0.0% | 0.0% | 0.0% | 0.19 | 0 | 1569 | $0.18 (simulated) |
| fault_mixed | stand-in | 21 | 95.2% | 0.0% | 0.0% | 4.8% | 0.33 | 0 | 1554 | $0.17 (simulated) |
| fault_timeout | stand-in | 21 | 100.0% | 0.0% | 0.0% | 0.0% | 0.10 | 0 | 1540 | $0.17 (simulated) |

## Retry strategies (stand-in; assumption: rejected attempts count against RPM)
| Scenario | n | Succeeded | Rate-limited | Timed out | Provider attempts/request | Attempts per ok answer | OK total p50 ms |
|---|---|---|---|---|---|---|---|
| retry_exponential | 120 | 20.0% | 80.0% | 0.0% | 5.02 | 25.1 | 2312 |
| retry_fixed | 120 | 20.0% | 80.0% | 0.0% | 5.03 | 25.1 | 2310 |
| retry_hardened | 120 | 42.5% | 38.3% | 19.2% | 0.43 | 1.0 | 14437 |
| retry_jitter | 120 | 20.0% | 80.0% | 0.0% | 5.02 | 25.1 | 2319 |
| retry_naive_immediate | 120 | 19.2% | 80.8% | 0.0% | 5.04 | 26.3 | 2330 |
| retry_naive_immediate_nocount | 120 | 45.8% | 54.2% | 0.0% | 3.71 | 8.1 | 2333 |

## Table 3: retrieval settings (real Groq, graded against gold evidence)
not measured: needs benchmarks/questions.jsonl authored from the real PDFs and a real-Groq run (python -m experiments.quality_evaluation).
