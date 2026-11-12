# Design decisions (interview notes)

Every number below is either from `results/results.csv` (measured on the real service or SIMULATED on the
stand-in, always labelled) or from `docs/documented_limits.json` (DOCUMENTED, fetched 2026-11-10).

## 1. Queue policy (`app/queue_manager.py`)
- Bounded priority queue, two classes: INTERACTIVE (0) before BATCH (1); FIFO within a class.
- Concurrency cap (default 4) plus `max_queue` (50). A full queue rejects immediately (`queue_full`) instead of growing latency without bound.
- A waiting request's queue timeout equals its deadline: a request that can no longer be answered in time leaves the queue (status `timeout`) rather than occupying a slot.
- Why: the binding limit is TPM (8,000), not concurrency, so an unbounded queue only converts overload into 30 s waits. Shedding early is cheaper than queuing late.
- Trade-off: strict priority can starve BATCH under sustained interactive load. Accepted: batch work is deferrable.

## 2. Limiter and admission control (`app/limiter.py`, `app/gateway.py`)
- Token buckets for RPM/TPM/RPD/TPD at 0.9 of the believed limit (safety margin) with continuous refill (inferred from the baseline headers: 86.4 s per request, ~133.3 tokens/s).
- Reservation before sending (estimated tokens = 70 + chars/3.5 + expected output), settle with real usage afterwards.
- A 429 `Retry-After` sets a shared cool-down for all requests (one rejection informs every caller).
- Admission control rejects at submit time when: the request can never fit (`too_large`), the daily budget is spent (`daily_budget`), the queue is full (`queue_full`), or the required wait would exceed the deadline (`admission_wait`). Rejections are reported as `rate_limited`, deadline misses as `timeout`.
- Why: with ~1,570 tokens per call the token-bound capacity is about 5 calls/min and a full answer needs 2 calls, so most overload must be refused honestly.

## 3. Retry strategy (`app/retry.py`)
- Hardened default: jittered exponential backoff, honours `Retry-After`, max 3 attempts, never retries invalid requests or daily exhaustion, a retry budget (30% of traffic + burst 3), backoff sleeps do not hold a concurrency slot, one retry for an empty response.
- Compared with naive immediate / fixed / exponential / jitter clients in the retry scenarios (SIMULATED, run `lt-retry2`).
- The decisive caveat: the result depends on ASSUMPTION `count_rejected_rpm` (rejected attempts consume RPM budget). Not verified for Groq. The control scenario `retry_naive_immediate_nocount` shows what remains true without that assumption: retries still multiply provider attempts, but the lock-out stops growing.
- Bug found by the experiment: a per-minute 429 with a huge Retry-After was first misclassified as a daily limit; classification now trusts the body text first (regression test in `tests/test_gateway.py`). The first retry runs (`lt-main__retry_*`) are kept on disk but excluded via `results/raw/SUPERSEDED.json`.

## 4. Retrieval configuration (`app/chunking.py`, `app/retrieval.py`)
- Settings: A = 300 tokens / overlap 50 / top-3; B = 600 / 100 / top-5; C = B + lexical re-rank. Same BM25 index and gold questions for all three.
- Optional hybrid (BM25 + real sentence-transformers via RRF). Never silently replaced by lexical: absent embeddings raise an error.
- Evidence sufficiency check (coverage 0.55, top score 2.0) decides refusal with ZERO model calls. These thresholds are uncalibrated placeholders until the real benchmark is run (`--sweep`).
- Which setting is best is NOT decided here: it needs the real PDFs and questions (Table 3 is "not measured").

## 5. Verification and refusal
Deterministic citation checks (uncited sentences, unknown markers, wrong pages, unsupported numbers, lexical support >= 0.5) then a separate LLM verifier (unparseable = unverified), one correction loop, else refuse. Max 4 model calls per question.

## 6. Status mapping
Capacity sheds -> `rate_limited`; deadline/queue/provider timeout -> `timeout`; everything else -> `error`; `final_reason` in diagnostics.

## 7. Assumptions (all labelled in code and tables)
Stand-in: max in-flight 8, provider queue 16, load sensitivity 0.5, recovery 5 s, 429 body text, `count_rejected_rpm`. Real Groq in-flight limit and 429 behaviour are unmeasured (`experiments/probe_429.py` measures the latter, ~10K tokens).

## 8. Context budget: temporary depletion is not permanent exhaustion (fix after real run `qa-A-1`)
**Symptom (real run qa-A-1, Setting A):** only 3 of 21 questions reached the model (5 API requests, 7,698 ledger tokens); several answerable questions were refused. The `qa-A-1` raw files were not available when this fix was written, so the cause below is established by code trace plus an offline reproduction, not by inspecting that run's files.

**Root cause (in the pipeline, not the limiter or gateway):** `Pipeline._context_budget` sized the prompt from the token bucket's *current level*. The bucket holds 7,200 tokens (8,000 TPM x 0.9) and one answered question costs ~5K, so after one or two questions it was drained. The old code then shrank the context and, when less than ~200 tokens of room remained, refused with `budget_exhausted` *before calling the gateway*, although a ~20-40 s refill would have admitted the request. Depletion that clears itself was treated as a permanent limit. `max_budget_wait_s=5` also contradicted the 30 s (evaluation: 120 s) deadline.
**Reproduction:** `tests/test_retrieval_pipeline.py::test_root_cause_old_policy_refused_answerable_questions_once_the_bucket_was_drained` freezes the old policy and, on a virtual clock, answers 2 of 8 questions; the new policy answers all 8.

**New policy**
- Context is sized from CAPACITY (`tpm_capacity`), the permanent ceiling. Refuse with `too_large` (zero model calls) only if even a minimal prompt can never fit.
- Temporary depletion is paced by the gateway (it waits for the bucket, or sheds with `admission_wait` -> status `rate_limited` if the wait would exceed the deadline). That is a capacity outcome, not an "insufficient evidence" refusal.
- Context shrinks only when the pacing wait would exceed `budget_wait_fraction` (0.5) of the remaining deadline, to what the bucket will hold after that wait, never below `min_context_tokens` (600). The decision is recorded in `PipelineResult.budget`.
- Daily exhaustion stays in the gateway (`daily_budget`). Budget checks were not removed; they were moved to the layer that knows the clock.
- `budget_exhausted` no longer exists as a pipeline refusal. New refusal reason: `too_large`.
**Known approximation:** the gateway reserves `min(max_tokens, 400)` output tokens while hidden reasoning can use up to `gen_max_tokens`=900; settlement corrects it afterwards, so the bucket debt is bounded.

## 9. Offline mock provider
`python -m experiments.quality_evaluation --provider mock` runs the full pipeline against `experiments/mock_provider.ExtractiveMockClient` (copies the best-matching passage sentences, always verifies "supported"). It needs no key, no `--yes`, writes only to `results/mock/`, and labels rows `source=stand-in`. It validates plumbing and the retrieval/gate behaviour; it says nothing about Groq answer quality, latency or limits.
