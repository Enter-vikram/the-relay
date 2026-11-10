# The Relay

Phase 1 setup (Windows PowerShell; use `python3` / `source .venv/bin/activate` on macOS/Linux):

```powershell
python --version                      # need 3.11+
python -m venv .venv
.venv\Scripts\Activate.ps1            # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env                # then edit .env and paste your key
python -m experiments.list_models
python -m experiments.baseline --n 5
python -m experiments.summarize_baseline
```

Never commit `.env`. Rotate the key at https://console.groq.com/keys if it leaks.
