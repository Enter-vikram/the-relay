# Submission checklist (verified on Linux only; Windows NOT verified)
Legend: V = verified against files/scripts in this environment; U = not verified; B = blocked on you.

| Item | Status | Path |
|---|---|---|
| Source code (gateway, limiter, queue, retry, pipeline, retrieval) | V | `app/` |
| Context-budget fix + regression tests | V | `app/pipeline.py`, `tests/test_retrieval_pipeline.py`, `docs/DECISIONS.md` s8 |
| Automated tests (220 passed, Linux) | V | `tests/` |
| Windows test run | U | run `python -m pytest -q` (see `WINDOWS_HANDOFF.md`) |
| Research ingestion + 3 exact papers | V | `scripts/ingest_papers.py`, `research/` (excluded from ZIP) |
| Benchmark (21 questions, unmodified, ambiguities kept) | V | `benchmarks/questions.jsonl`, `benchmarks/AUDIT.md` |
| Retrieval benchmark (real papers, no model) | V | `results/raw/rb-2.rows.csv` |
| A/B/C offline run, 21 questions each (MOCK provider) | V | `results/mock/mock-1__{A,B,C}.*` |
| A/B/C comparison | V | `report/ab_comparison.md` |
| Real Groq A/B/C answer quality | B (needs your approval + run) | not measured; dry-run: A ~82K, B ~136K, C ~122K tokens |
| Load tests (steady, burst, overload, limits, 429, daily, retry, faults) | V (simulated) | `results/raw/lt-*`, `report/load_test_report.md` |
| Recovery / reservation reconciliation | V | `tests/test_recovery.py` |
| Derived data | V | `results/results.csv` (980 rows), `results/diagnostics.csv`, `report/tables.md` |
| Two-page report PDF | V (2 pages, 12 headings) | `report/final_report.pdf` |
| Report source | V | `report/final_report.md` |
| README, setup, reproduction | V (commands resolve; Windows U) | `README.md`, `setup.bat`, `WINDOWS_HANDOFF.md` |
| Manifest | V | `MANIFEST.txt` |
| Submission ZIP | V | `/home/claude/out/the-relay-handoff.zip` |
| Report s6 final chosen setting | B | needs real quality run |
| Report s10 "what I learned" | B | your input |
| Report s11 Vaani feedback | B | your input |
| Report s12 collaboration account | B | your input (history: `report/conversation_history.md`) |
| `qa-A-1` raw files | U | not available in this environment; root cause proven by trace + reproduction |
| Real Groq calls / commits / pushes by me | none | |
