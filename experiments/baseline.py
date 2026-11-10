"""Phase 1: modest, sequential real-Groq baseline (schema v2).

Streams chat completions with httpx (no SDK, NO retries of any kind), recording
per request: latency, status, token usage, rate-limit headers and an accurate
outcome. One row per HTTP attempt, appended to results/baseline_v2.csv.

Measurement definitions
-----------------------
first_token_ms     Time from request start to the first NON-EMPTY `content`
                   delta (the first user-visible text). This is a chunk, not
                   necessarily one token. Reasoning deltas are NOT counted.
                   (v1 baseline.csv counted reasoning or content; those
                   numbers are not comparable to this column.)
first_reasoning_ms Time to the first non-empty `reasoning` delta, if any.
total_ms           Request start until the stream ended or the call failed.
                   Monotonic clock throughout.
tokens_out         Provider-reported completion tokens. For reasoning models
                   this INCLUDES hidden reasoning tokens.

Outcomes
--------
ok                 HTTP 200, `[DONE]` received, non-empty content.
incomplete_stream  HTTP 200 but the stream ended/broke before `[DONE]`.
empty_response     `[DONE]` received but no answer content (e.g. reasoning
                   used the whole max_tokens budget; see finish_reason).
timeout            Per-read timeout (httpx) or total deadline (asyncio).
rate_limited       HTTP 429.
error              Any other HTTP status, transport error before a 200,
                   malformed SSE JSON, or an error event inside the stream.

Usage (Windows CMD or PowerShell):
    python -m experiments.baseline --n 5
    python -m experiments.baseline --n 30 --gap 4
"""
import argparse
import asyncio
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings

SCHEMA_VERSION = 2

PROMPTS = [
    "In two sentences, what is self-attention?",
    "Explain what a residual connection does, in about 60 words.",
    "List three differences between RNNs and Transformers, briefly.",
    "What is positional encoding and why is it needed? Keep it under 80 words.",
    "Describe multi-head attention in one short paragraph.",
    "Why do speech models care about latency? Answer in 3 sentences.",
]

FIELDS = [
    "schema_version", "run_id", "seq", "start_time_utc", "model", "prompt_chars",
    "max_tokens", "status", "outcome", "first_token_ms", "first_reasoning_ms",
    "total_ms", "tokens_in", "tokens_out", "content_chars", "reasoning_chars",
    "finish_reason", "done_received", "usage_received", "retry_after",
    "rl_limit_requests", "rl_limit_tokens", "rl_remaining_requests",
    "rl_remaining_tokens", "rl_reset_requests", "rl_reset_tokens", "error_body",
]

DEFAULT_OUT = settings.results_dir / "baseline_v2.csv"


class StreamParseError(Exception):
    """Malformed SSE payload or an error event delivered inside the stream."""


@dataclass
class StreamState:
    first_content: float | None = None      # monotonic timestamps
    first_reasoning: float | None = None
    content_chars: int = 0
    reasoning_chars: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    finish_reason: str = ""
    done: bool = False
    usage: bool = False


def _rate_headers(h: httpx.Headers) -> dict:
    g = h.get
    return {
        "retry_after": g("retry-after", ""),
        "rl_limit_requests": g("x-ratelimit-limit-requests", ""),
        "rl_limit_tokens": g("x-ratelimit-limit-tokens", ""),
        "rl_remaining_requests": g("x-ratelimit-remaining-requests", ""),
        "rl_remaining_tokens": g("x-ratelimit-remaining-tokens", ""),
        "rl_reset_requests": g("x-ratelimit-reset-requests", ""),
        "rl_reset_tokens": g("x-ratelimit-reset-tokens", ""),
    }


async def _consume(resp: httpx.Response, st: StreamState) -> None:
    """Read the SSE stream into `st`. Raises StreamParseError on bad data."""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue  # blank separators, SSE comments, event: lines
        payload = line[5:].strip()
        if payload == "[DONE]":
            st.done = True
            return
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError as e:
            raise StreamParseError(f"bad SSE JSON ({e}); payload={payload[:100]!r}") from e
        if isinstance(evt, dict) and "error" in evt:
            raise StreamParseError(f"error event in stream: {json.dumps(evt['error'])[:300]}")

        usage = evt.get("usage") or (evt.get("x_groq") or {}).get("usage")
        if usage:
            st.tokens_in = usage.get("prompt_tokens")
            st.tokens_out = usage.get("completion_tokens")
            st.usage = True

        for ch in evt.get("choices") or []:
            delta = ch.get("delta") or {}
            content = delta.get("content") or ""
            reasoning = delta.get("reasoning") or ""
            if reasoning:
                st.reasoning_chars += len(reasoning)
                if st.first_reasoning is None:
                    st.first_reasoning = time.monotonic()
            if content:
                st.content_chars += len(content)
                if st.first_content is None:
                    st.first_content = time.monotonic()
            if ch.get("finish_reason"):
                st.finish_reason = ch["finish_reason"]


def _classify(st: StreamState) -> str:
    """Outcome for a request that returned 200 and raised nothing."""
    if not st.done:
        return "incomplete_stream"
    if st.content_chars == 0:
        return "empty_response"
    return "ok"


def _ms(t0: float, t: float | None):
    return "" if t is None else round((t - t0) * 1000, 1)


async def one_request(client: httpx.AsyncClient, run_id: str, seq: int, prompt: str,
                      max_tokens: int, timeout_s: float, *, api_key: str | None = None,
                      base_url: str | None = None, model: str | None = None) -> dict:
    """Send exactly one request (no retries) and return one CSV row."""
    api_key = api_key if api_key is not None else settings.require_key()
    base_url = base_url or settings.base_url
    model = model or settings.model

    row = {k: "" for k in FIELDS}
    row.update(schema_version=SCHEMA_VERSION, run_id=run_id, seq=seq, model=model,
               prompt_chars=len(prompt), max_tokens=max_tokens,
               start_time_utc=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    st = StreamState()
    t0 = time.monotonic()
    try:
        # httpx's timeout is per phase (each read); asyncio.timeout is the
        # total deadline, so a slow trickle cannot hang the baseline forever.
        async with asyncio.timeout(timeout_s):
            async with client.stream(
                "POST", f"{base_url}/chat/completions", json=body,
                headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout_s,
            ) as resp:
                row["status"] = resp.status_code
                row.update(_rate_headers(resp.headers))
                if resp.status_code != 200:
                    row["error_body"] = (await resp.aread()).decode(errors="replace")[:500]
                    row["outcome"] = "rate_limited" if resp.status_code == 429 else "error"
                else:
                    await _consume(resp, st)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        row["outcome"] = "timeout"
    except StreamParseError as e:
        row["outcome"] = "error"
        row["error_body"] = str(e)[:500]
    except httpx.HTTPError as e:
        # Broke after a 200 => partial stream; before a response => plain error.
        row["outcome"] = "incomplete_stream" if row["status"] == 200 else "error"
        row["error_body"] = f"{type(e).__name__}: {e}"[:500]
    t1 = time.monotonic()

    if not row["outcome"]:
        row["outcome"] = _classify(st)
    row.update(
        total_ms=round((t1 - t0) * 1000, 1),
        first_token_ms=_ms(t0, st.first_content),
        first_reasoning_ms=_ms(t0, st.first_reasoning),
        tokens_in="" if st.tokens_in is None else st.tokens_in,
        tokens_out="" if st.tokens_out is None else st.tokens_out,
        content_chars=st.content_chars, reasoning_chars=st.reasoning_chars,
        finish_reason=st.finish_reason, done_received=st.done, usage_received=st.usage,
    )
    return row


def open_results(path: Path):
    """Open `path` for appending rows. Refuses a file whose header differs
    from FIELDS so incompatible schemas are never mixed. Returns (file, writer)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    has_data = path.exists() and path.stat().st_size > 0
    if has_data:
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:
            raise ValueError(
                f"{path} has a different header ({len(header)} columns, expected "
                f"{len(FIELDS)}). Not appending; choose a new --out file.")
    f = path.open("a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if not has_data:
        w.writeheader()
    return f, w


async def main(n: int, gap: float, max_tokens: int, timeout_s: float, out: Path) -> None:
    try:
        f, w = open_results(out)
    except ValueError as e:
        raise SystemExit(str(e))
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S")
    with f:
        async with httpx.AsyncClient() as client:
            for i in range(n):
                row = await one_request(client, run_id, i, PROMPTS[i % len(PROMPTS)],
                                        max_tokens, timeout_s)
                w.writerow(row)
                f.flush()
                print(f"[{i+1}/{n}] status={row['status']} outcome={row['outcome']} "
                      f"ttft_content={row['first_token_ms']}ms total={row['total_ms']}ms "
                      f"in/out={row['tokens_in']}/{row['tokens_out']} "
                      f"finish={row['finish_reason']} rem_req={row['rl_remaining_requests']} "
                      f"rem_tok={row['rl_remaining_tokens']}")
                if row["outcome"] != "ok":
                    print(f"  !! {row['outcome']}: {row['error_body'] or 'no error body'}")
                if row["outcome"] == "rate_limited":
                    print("  429 hit. retry-after:", row["retry_after"], "| stopping (no auto-retry).")
                    break
                if i < n - 1:
                    await asyncio.sleep(gap)
    print("raw rows appended to", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between sequential requests")
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    asyncio.run(main(a.n, a.gap, a.max_tokens, a.timeout, a.out))
