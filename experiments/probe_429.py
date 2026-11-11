"""Observe ONE real rate-limit response from Groq, cheaply and safely (RUN ON YOUR MACHINE).

    python -m experiments.probe_429 --yes-spend-tokens

What it does (3 requests, roughly 10 000 tokens, about 5% of the DOCUMENTED daily token budget):
  1. request A: a ~5 000-token prompt (fits the 8 000 tokens-per-minute bucket)
  2. request B: another ~5 000-token prompt immediately after: expected to exceed the per-minute token budget -> 429
  3. wait (retry-after + 1 s), then request C: a tiny prompt, to confirm recovery
and records every response (status, headers, body, retry-after, timing) to results/probe_429.csv using the baseline
v2 schema. It stops immediately on anything unexpected. It never retries. It never runs more than once per invocation.

What it can answer for Table 1: the real 429 body and headers, the real retry-after, whether recovery matched it,
and whether a REJECTED request still consumed daily-request budget (compare x-ratelimit-remaining-requests
across A, B, C). What it cannot answer: RPM, TPD, in-flight concurrency (deliberately not probed).
If B does not return 429 the probe reports that: your account's per-minute token budget is larger than 8 000
(check https://console.groq.com/settings/limits) and nothing else is attempted.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from experiments.baseline import open_results, one_request

OUT = settings.results_dir / "probe_429.csv"
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
         "quebec romeo sierra tango uniform victor whiskey xray yankee zulu").split()


def big_prompt(chars: int, salt: str) -> str:
    out, i = [f"Reference {salt}. Reply with the single word OK. Ignore the following filler text: "], 0
    n = len(out[0])
    while n < chars:
        w = WORDS[(i * 7 + len(salt)) % len(WORDS)] + (" " if i % 12 else ". ")
        out.append(w)
        n += len(w)
        i += 1
    return "".join(out)


def _h(row: dict, key: str) -> str:
    return str(row.get(key, ""))


async def run_probe(client: httpx.AsyncClient, writer, f, *, prompt_chars: int = 20_000, sleep=asyncio.sleep,
                    api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> dict:
    run_id = datetime.now(timezone.utc).strftime("probe429-%Y%m%dT%H%M%S")
    kw = dict(api_key=api_key, base_url=base_url, model=model)
    rows = []
    for seq, (label, prompt) in enumerate([("A", big_prompt(prompt_chars, "A")), ("B", big_prompt(prompt_chars, "B"))]):
        row = await one_request(client, run_id, seq, prompt, 32, 60.0, **kw)
        writer.writerow(row); f.flush(); rows.append(row)
        print(f"{label}: status={row['status']} outcome={row['outcome']} tokens_in={row['tokens_in']} "
              f"remaining_tokens={row['rl_remaining_tokens']} remaining_requests={row['rl_remaining_requests']} "
              f"retry_after={row['retry_after']}")
        if row["outcome"] not in ("ok", "empty_response", "rate_limited"):
            print("Unexpected outcome; stopping without further requests.")
            return {"result": "aborted", "rows": rows}
        if label == "A" and row["outcome"] == "rate_limited":
            print("Request A was already limited: the account may be at its limit right now. Stopping.")
            return {"result": "aborted_A_limited", "rows": rows}
    b = rows[1]
    if b["outcome"] != "rate_limited":
        print("B was NOT rate limited: the per-minute token budget on this account looks larger than 8 000 "
              "(or the prompt was smaller than intended). Check console.groq.com/settings/limits. Nothing more sent.")
        return {"result": "no_429", "rows": rows}
    wait = float(b["retry_after"] or 0) + 1.0
    print(f"429 observed. body: {b['error_body'][:200]!r}\nwaiting {wait:.0f} s (retry-after + 1) ...")
    await sleep(wait)
    c = await one_request(client, run_id, 2, "Reply with the single word OK.", 32, 60.0, **kw)
    writer.writerow(c); f.flush(); rows.append(c)
    print(f"C (after waiting): status={c['status']} outcome={c['outcome']} remaining_tokens={c['rl_remaining_tokens']} "
          f"remaining_requests={c['rl_remaining_requests']}")
    try:
        ra, rc = int(rows[0]["rl_remaining_requests"]), int(c["rl_remaining_requests"])
        print(f"daily-request counter: after A {ra}, after C {rc} (B rejected, C accepted): "
              + ("a rejected request did NOT consume daily budget" if ra - rc == 1
                 else f"difference {ra - rc}: B may have consumed daily budget (check by hand)"))
    except ValueError:
        pass
    return {"result": "recovered" if c["outcome"] in ("ok", "empty_response") else "not_recovered", "rows": rows}


async def main_async(a) -> int:
    if not a.yes_spend_tokens:
        print("This sends 3 real requests (~10 000 tokens). Re-run with --yes-spend-tokens to confirm.")
        return 3
    try:
        f, w = open_results(OUT)
    except ValueError as e:
        print("ERROR:", e)
        return 1
    with f:
        async with httpx.AsyncClient() as client:
            out = await run_probe(client, w, f)
    print("RESULT:", out["result"], "| rows appended to", OUT)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-spend-tokens", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
