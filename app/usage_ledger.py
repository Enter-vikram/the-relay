"""Local ledger of real-provider usage so experiments can stay inside the daily budgets.

Groq exposes no header for the daily token budget (only per-minute tokens and daily requests), so daily token
use can only be tracked locally. The window is a rolling 24 h (the baseline headers imply a continuously
refilling budget, not a midnight reset). Entries are append-only JSON lines; the file is git-ignored
(`results/real_usage_ledger.jsonl`) because it describes one account.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def append(path: Path, *, requests: int, tokens: int, tag: str, now: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now if now is not None else time.time(), "requests": requests,
                            "tokens": tokens, "tag": tag}) + "\n")


def used_last_24h(path: Path, now: float | None = None) -> tuple[int, int]:
    """Return (requests, tokens) used in the last 24 h."""
    now = now if now is not None else time.time()
    if not Path(path).is_file():
        return 0, 0
    reqs = toks = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if now - e["ts"] <= 86_400:
            reqs += e["requests"]
            toks += e["tokens"]
    return reqs, toks
