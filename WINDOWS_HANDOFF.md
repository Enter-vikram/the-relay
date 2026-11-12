# WINDOWS_HANDOFF (Windows 11, CMD, VS Code)

Run every command in **CMD** from your repo root (the folder containing `app\`). Nothing here asks for your API key in chat: you type it into `.env` yourself.

Legend: **[NO QUOTA]** uses no Groq tokens. **[QUOTA]** spends real Groq free-tier tokens.

Already done, do NOT repeat: the 5-request real baseline (`results\baseline_v2.csv`), and all stand-in load tests (`results\raw\lt-*`). If you already downloaded, extracted and indexed the papers, skip steps 4-5.

## 1. Extract the ZIP and merge it safely [NO QUOTA]
```cmd
cd /d C:\path\to\the-relay
git status
xcopy /E /I /Y . ..\the-relay-backup
mkdir ..\relay_zip
tar -xf C:\path\to\the-relay-handoff.zip -C ..\relay_zip
robocopy ..\relay_zip . /E /XF baseline.csv baseline_v2.csv .env /XD .git .venv research
git status
git diff --stat
```
- The backup copy comes first. `robocopy` exit codes 0-7 mean success.
- Your original `baseline.csv`, `baseline_v2.csv`, `.env`, `.git`, `.venv` and `research\` (your PDFs, extraction, indexes) are never overwritten.
- Four existing files are replaced by newer versions: `.gitignore`, `README.md`, `app\config.py`, `requirements.txt`. Review them with `git diff`.
- Nothing is committed or pushed for you.

## 2. Activate .venv [NO QUOTA]
```cmd
.venv\Scripts\activate.bat
python --version
```
Need Python 3.11+. (If `.venv` does not exist: `python -m venv .venv`, then activate.)

## 3. Install dependencies [NO QUOTA]
```cmd
python -m pip install -r requirements.txt
pip check
```
Create `.env` once if missing: `copy .env.example .env`, then open `.env` in VS Code and paste your key there. Never paste it into chat.

## 4. Download and verify the three PDFs [NO QUOTA] (skip if already done)
```cmd
python -m scripts.download_papers
python -m scripts.verify_papers
```
If the download is blocked, save the three exact-version PDFs into `research\papers` under the file names `verify_papers` reports, then rerun verify.

## 5. Extract, chunk and index [NO QUOTA] (skip if already done)
```cmd
python -m scripts.ingest_papers
python -m scripts.build_index
```
Index folders appear under `research\index\`.

## 6. Embeddings (optional hybrid retrieval) [NO QUOTA]
```cmd
pip install -r requirements-optional.txt
python -m scripts.build_embeddings
```
Downloads about 90 MB model plus PyTorch. Skip it and hybrid is reported "not measured", never faked.

## 7. Benchmark questions [NO QUOTA]
`benchmarks\questions.jsonl` (21 questions, your file) is included unchanged; `questions.original.jsonl` is a copy and `benchmarks\AUDIT.md` lists my checks and caveats.
```cmd
python -m experiments.validate_benchmark
```

## 8. Retrieval benchmark [NO QUOTA]
```cmd
python -m scripts.run_benchmarks --settings A B C --run-id rb-3
python -m scripts.run_benchmarks --settings B --sweep --run-id rb-sweep-2
```
Use a NEW `--run-id` each time (raw files are never overwritten). Reference results already exist: `results\raw\rb-2.rows.csv` (rb-1 is superseded). Add `--hybrid` after step 6. `--sweep` helps pick the sufficiency thresholds.

## 9. Real Groq experiments [QUOTA]
Plan: about 2 model calls per answered question, about 1,500 tokens per call, 8,000 tokens per minute. The tool paces itself and stops at 80% of the documented daily budgets (ledger: `results\real_usage_ledger.jsonl`).
```cmd
python -m experiments.quality_evaluation --settings A --dry-run
python -m experiments.quality_evaluation --settings A --yes
python -m experiments.quality_evaluation --settings B --yes
python -m experiments.quality_evaluation --settings C --yes
```
`--dry-run` is **[NO QUOTA]** and prints the estimate first. Run one setting at a time. `--limit N` caps the question count.

Optional, about 10K tokens, observes what Groq returns on a 429:
```cmd
python -m experiments.probe_429 --yes-spend-tokens
```
Also check your real limits at https://console.groq.com/settings/limits (resolves the RPM conflict, 30 documented vs 1,000 on the models page).

## 10. Generate results and the report tables [NO QUOTA]
```cmd
python -m scripts.generate_report
python -m scripts.build_report
python -m scripts.check_report
```
`build_report` copies the generated tables into `report\final_report.md`. Add `--pdf` to also write `report\final_report.pdf` (needs pandoc and `playwright install chromium`; otherwise export the Markdown to PDF yourself). Then fill the placeholders in `report\final_report.md`, export it to PDF and verify the two-page limit:
```cmd
python -m scripts.check_report --pdf report\final_report.pdf
```

## 11. Final test suite [NO QUOTA]
```cmd
python -m pytest -q
```
Last verified result in the build environment: 211 passed (Linux). Not yet run on Windows by me.

## What needs you personally
Benchmark questions (step 7), the Vaani feedback, the assistant-collaboration account (`report\conversation_history.md`), the report placeholders, and your approval before any commit or push.

## Added after the context-budget fix (all [NO QUOTA]; not yet run on Windows)
```cmd
python -m pytest -q
python -m experiments.quality_evaluation --settings A B C --provider mock --run-id mock-win-1
python -m scripts.compare_settings --run-id mock-win-1
python -m experiments.load_test --all --only fault_5xx fault_timeout fault_incomplete fault_mixed --run-id lt-fault-win-1
python -m scripts.load_report
python -m scripts.generate_report
python -m scripts.build_report
python -m scripts.check_report --file report\final_report.md --pdf report\final_report.pdf
python -m experiments.quality_evaluation --settings A --dry-run --run-id qa-A-2
```
Expected on Linux: 220 tests pass. A real Setting A run (`--yes`, own `.env`) is only for after you approve it; use a new run ID (not `qa-A-1`).
