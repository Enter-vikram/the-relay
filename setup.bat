@echo off
REM The Relay - one-shot setup for Windows CMD. Never reads or prints your API key.
setlocal
set PYTHONUTF8=1
python --version || (echo Python 3.11+ is required & exit /b 1)
if not exist .venv ( python -m venv .venv || exit /b 1 )
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt || exit /b 1
if not exist .env ( copy .env.example .env & echo Created .env - open it and paste your key yourself. )
python -m scripts.download_papers || echo [!] Download failed: download the 3 PDFs manually into data\papers (see README).
python -m scripts.verify_papers || exit /b 1
python -m scripts.ingest_papers || exit /b 1
python -m scripts.build_index || exit /b 1
python -m pytest -q
echo.
echo Setup finished. Next: see README.md section "Remaining manual steps".
endlocal
