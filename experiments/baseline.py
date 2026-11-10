"""Phase 1: modest, sequential real-Groq baseline.

Streams chat completions with httpx (no SDK, no hidden retries), recording per
request: TTFT, total time, status, token usage and every rate-limit header.
Raw rows go to results/baseline.csv (append-only; one row per HTTP attempt).

Usage:
    python -m experiments.baseline --n 5          # first run: keep it small
    python -m experiments.baseline --n 30 --gap 4 # later: ~15 req/min
"""
import argparse
import asyncio
import csv
import json
import time
from datetime import datetime, timezone

import httpx

from app.config import settings

PROMPTS = [
    "In two sentences, what is self-attention?",
    "Explain what a residual connection does, in about 60 words.",
    "List three differences between RNNs and Transformers, briefly.",
    "What is positional encoding and why is it needed? Keep it under 80 words.",
    "Describe multi-head attention in one short paragraph.",
    "Why do speech models care about latency? Answer in 3 sentences.",
]

FIELDS = [
    "run_id", "seq", "start_time_utc", "model", "prompt_chars", "max_tokens",
    "status", "outcome", "first_token_ms", "total_ms", "tokens_in", "tokens_out",
    "out_chars", "retry_after", "rl_limit_requests", "rl_limit_tokens",
    "rl_remaining_requests", "rl_remaining_tokens", "rl_reset_requests",
    "rl_reset_tokens", "error_body",
]


def rl(h: httpx.Headers, name: str) -> str:
    return h.get(name, "")


async def one_request(client: httpx.AsyncClient, run_id: str, seq: int, prompt: str,
                      max_tokens: int, timeout_s: float) -> dict:
    row = {k: "" for k in FIELDS}
    row.update(run_id=run_id, seq=seq, model=settings.model, prompt_chars=len(prompt),
               max_tokens=max_tokens,
               start_time_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    body = {
        "model": settings.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    t0 = time.monotonic()
    first = None
    out_chars = 0
    try:
        async with client.stream(
            "POST", f"{settings.base_url}/chat/completions", json=body,
            headers={"Authorization": f"Bearer {settings.require_key()}"},
            timeout=timeout_s,
        ) as r:
            row["status"] = r.status_code
            h = r.headers
            row.update(retry_after=rl(h, "retry-after"),
                       rl_limit_requests=rl(h, "x-ratelimit-limit-requests"),
                       rl_limit_tokens=rl(h, "x-ratelimit-limit-tokens"),
                       rl_remaining_requests=rl(h, "x-ratelimit-remaining-requests"),
                       rl_remaining_tokens=rl(h, "x-ratelimit-remaining-tokens"),
                       rl_reset_requests=rl(h, "x-ratelimit-reset-requests"),
                       rl_reset_tokens=rl(h, "x-ratelimit-reset-tokens"))
            if r.status_code != 200:
                row["error_body"] = (await r.aread()).decode(errors="replace")[:500]
                row["outcome"] = "rate_limited" if r.status_code == 429 else "error"
            else:
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    evt = json.loads(payload)
                    usage = evt.get("usage") or evt.get("x_groq", {}).get("usage")
                    if usage:
                        row["tokens_in"] = usage.get("prompt_tokens", "")
                        row["tokens_out"] = usage.get("completion_tokens", "")
                    for ch in evt.get("choices", []):
                        delta = ch.get("delta", {})
                        # reasoning models may emit "reasoning" before "content"
                        text = delta.get("content") or delta.get("reasoning") or ""
                        if text:
                            if first is None:
                                first = time.monotonic()
                            out_chars += len(text)
                row["outcome"] = "ok"
    except httpx.TimeoutException:
        row["outcome"] = "timeout"
    except httpx.HTTPError as e:
        row["outcome"] = "error"
        row["error_body"] = type(e).__name__
    t1 = time.monotonic()
    row["total_ms"] = round((t1 - t0) * 1000, 1)
    if first is not None:
        row["first_token_ms"] = round((first - t0) * 1000, 1)
    row["out_chars"] = out_chars
    return row


async def main(n: int, gap: float, max_tokens: int, timeout_s: float) -> None:
    settings.results_dir.mkdir(exist_ok=True)
    path = settings.results_dir / "baseline.csv"
    new = not path.exists()
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S")
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        async with httpx.AsyncClient() as client:
            for i in range(n):
                row = await one_request(client, run_id, i, PROMPTS[i % len(PROMPTS)],
                                        max_tokens, timeout_s)
                w.writerow(row)
                f.flush()
                print(f"[{i+1}/{n}] status={row['status']} outcome={row['outcome']} "
                      f"ttft={row['first_token_ms']}ms total={row['total_ms']}ms "
                      f"in/out={row['tokens_in']}/{row['tokens_out']} "
                      f"rem_req={row['rl_remaining_requests']} rem_tok={row['rl_remaining_tokens']}")
                if row["outcome"] == "rate_limited":
                    print("  429 hit. retry-after:", row["retry_after"], "| stopping (no auto-retry).")
                    break
                if i < n - 1:
                    await asyncio.sleep(gap)
    print("raw rows appended to", path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between sequential requests")
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()
    asyncio.run(main(a.n, a.gap, a.max_tokens, a.timeout))
