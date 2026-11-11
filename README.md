# The Relay

Question answering over exactly three arXiv papers (1706.03762v7, 2410.00037v1, 2507.07955v2) with page citations and honest refusals,
behind a rate-limited gateway (queue, limiter, admission control, retries). Real Groq (`openai/gpt-oss-20b`) plus a calibrated stand-in server.

**Honesty rule:** stand-in results are SIMULATED and are always labelled. Only `baseline_v2.csv` (5 requests) and anything you run with `--source real` are measured on Groq.

## Windows CMD setup
```cmd
setup.bat
```
or manually:
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
python -m scripts.download_papers
python -m scripts.verify_papers
python -m scripts.ingest_papers
python -m scripts.build_index
python -m pytest -q
```
Paste your key into `.env` yourself. It is git-ignored; never share it.
If the download fails (proxy/network), put the three PDFs in `data\papers` using the names printed by the script and rerun verify.

## Run
```cmd
uvicorn app.main:app --port 8000
```
`POST /ask`, `GET /health`, `GET /stats`.

## Stand-in load tests (simulated, no quota)
```cmd
python -m scripts.run_load_tests --run-id lt-2
python -m scripts.generate_report
```
Raw files in `results\raw` are immutable; derived `results.csv`, `diagnostics.csv`, `report\tables.md` are rebuilt.

## Remaining manual steps
1. `python -m scripts.build_embeddings` (optional hybrid; needs `pip install -r requirements-optional.txt`).
2. Author `benchmarks\questions.jsonl` from the real PDFs (see `benchmarks\README.md`), then `python -m experiments.validate_benchmark`.
3. `python -m scripts.run_benchmarks` (retrieval only, no quota). Add `--sweep` to tune sufficiency thresholds.
4. Real quality run, one setting at a time: `python -m experiments.quality_evaluation --settings A --dry-run`, then without `--dry-run --yes`. A ledger guard stops at 80% of documented daily budgets.
5. Optional (~10K tokens): `python -m experiments.probe_429 --yes-spend-tokens` to observe what Groq does on a 429.
6. Check your real limits at https://console.groq.com/settings/limits, give honest Vaani feedback, write the collaboration section, review the report in `report\final_report.md`.

## Limits of this repo
Load tests exercise the gateway only (no retrieval/verification). Retrieval and pipeline were tested only on SYNTHETIC text because arXiv was unreachable when they were built. PyMuPDF is AGPL/commercial: fine for private coursework, note it in the report.

Design rationale: `docs/DECISIONS.md`.
