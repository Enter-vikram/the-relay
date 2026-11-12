"""Controlled load tests against the STAND-IN (never against Groq).

    python -m experiments.load_test --scenario steady --seed 1
    python -m experiments.load_test --all --seed 1          # all scenarios, in parallel processes

What a "request" is here: ONE gateway call (limiter + queue + retry + client) carrying a prompt sized
like a retrieval-augmented call (default ~1300 prompt tokens, ASSUMPTION derived from retrieval setting A:
3 x 300-token chunks + system prompt). It is not the full pipeline: retrieval and the verification model call
are not exercised, so a real answer (generation + verification = 2 calls) would use roughly twice the
per-answer capacity measured here. results.csv rows carry source=stand-in and an empty retrieval_setting.

Raw output (immutable once written, one file pair per run):
    results/raw/<run_id>__<scenario>.results.csv        the 12 required columns
    results/raw/<run_id>__<scenario>.diagnostics.csv    extra per-request fields
    results/raw/<run_id>__<scenario>.meta.json          scenario, seed, stand-in config, stand-in stats
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.gateway import Gateway, to_record
from app.limiter import Limiter
from app.llm import LLMClient
from app.metrics import append_records
from app.queue_manager import BATCH, INTERACTIVE, QueueManager
from app.retry import RetryBudget, RetryPolicy
from standin.config import StandinConfig

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "results" / "raw"

TOKENS_IN_TARGET = 1300            # ASSUMPTION: retrieval setting A prompt (see module docstring)
MAX_TOKENS = 900                   # request max_tokens (pipeline default)
CHARS_PER_TOKEN_STANDIN = 4.0      # stand-in assumption: converts target tokens to prompt characters
OVERHEAD = 70


@dataclass
class Scenario:
    name: str
    description: str
    duration_s: float                       # arrival window
    rate_per_min: float                     # base offered rate
    burst_at_s: float | None = None
    burst_n: int = 0
    burst_width_s: float = 3.0
    standin: dict = field(default_factory=dict)          # overrides on the baseline-calibrated config
    client_believes: dict = field(default_factory=dict)  # limits the gateway paces to (default = documented/observed)
    strategy: str = "jitter"                # retry strategy
    hardened: bool = True                   # pacing + admission + Retry-After + retry budget
    max_attempts: int = 3
    deadline_s: float = 30.0
    tokens_in: int = TOKENS_IN_TARGET
    interactive_share: float = 0.8
    concurrency: int = 4
    max_queue: int = 50
    notes: str = ""


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("steady", "steady arrivals below the token-bound capacity", 480, 3.5,
             notes="capacity at 8000 TPM and ~1570 tokens/call is ~5.1 calls/min (token-bound)"),
    Scenario("burst", "low background rate plus a sudden burst of 20 requests in 3 s", 360, 2.0,
             burst_at_s=120, burst_n=20),
    Scenario("overload", "sustained arrivals at ~2.6x the token-bound capacity", 300, 12.0),
    Scenario("limit_halved", "provider enforces half the per-minute limits (TPM 4000, RPM 15) while the "
             "client still believes the originals", 360, 3.5,
             standin={"tpm": 4000, "rpm": 15}),
    Scenario("daily_exhausted", "provider daily request budget scaled down to 12 so exhaustion happens in "
             "minutes (stand-in setting, not a Groq value)", 300, 6.0,
             standin={"rpd": 12}, client_believes={"rpd": 1000}),
    # ---- provider failure (all fault rates are ASSUMED stand-in settings, not Groq measurements) ----
    Scenario("fault_5xx", "provider returns injected HTTP 500 (12%) and 503 (8%) at a steady below-capacity rate",
             360, 3.5, standin={"p_500": 0.12, "p_503": 0.08}),
    Scenario("fault_timeout", "10% of requests hang until the client times out", 360, 3.5,
             standin={"p_hang": 0.10, "hang_s": 120.0}, deadline_s=30.0),
    Scenario("fault_incomplete", "15% of streams are cut before [DONE] (incomplete response)", 360, 3.5,
             standin={"p_interrupt": 0.15}),
    Scenario("fault_mixed", "5xx 8%, hang 5%, interrupted stream 8% together", 360, 3.5,
             standin={"p_500": 0.05, "p_503": 0.03, "p_hang": 0.05, "p_interrupt": 0.08}),
    # ---- retry comparison: small prompts so RPM (20, stand-in setting) is the binding limit ----
    *[Scenario(f"retry_{name}", f"retry strategy {name}: {desc}", 180, 40.0, tokens_in=300,
               standin={"rpm": 20, "count_rejected_rpm": True}, client_believes={"rpm": 1000},
               strategy=strat, hardened=hard, max_attempts=6 if not hard else 3,
               notes="ASSUMPTION: the provider counts rejected attempts against RPM (not verified for Groq)")
      for name, strat, hard, desc in [
          ("naive_immediate", "immediate", False, "no pacing, immediate retry, ignores Retry-After"),
          ("fixed", "fixed", False, "no pacing, fixed 1 s delay, ignores Retry-After"),
          ("exponential", "exponential", False, "no pacing, exponential backoff, ignores Retry-After"),
          ("jitter", "jitter", False, "no pacing, jittered backoff, ignores Retry-After"),
          ("hardened", "jitter", True, "pacing + Retry-After + retry budget + admission control")]],
    # CONTROL: same naive client, but rejected attempts cost no RPM budget. Separates "retries waste work"
    # (true under any provider) from "retries lengthen the lockout" (true only if rejections are counted).
    Scenario("retry_naive_immediate_nocount", "CONTROL: naive immediate retry, rejected attempts cost no RPM",
             180, 40.0, tokens_in=300, standin={"rpm": 20, "count_rejected_rpm": False},
             client_believes={"rpm": 1000}, strategy="immediate", hardened=False, max_attempts=6,
             notes="control for the count_rejected_rpm assumption"),
]}


def arrivals(sc: Scenario, rng: random.Random) -> list[float]:
    gap = 60.0 / sc.rate_per_min
    n = int(sc.duration_s / gap)
    ts = [max(0.0, i * gap + rng.uniform(-0.25, 0.25) * gap) for i in range(n)]
    if sc.burst_at_s is not None:
        ts += [sc.burst_at_s + rng.uniform(0, sc.burst_width_s) for _ in range(sc.burst_n)]
    return sorted(ts)


def prompt_for(tokens: int) -> str:
    chars = int((tokens - OVERHEAD) * CHARS_PER_TOKEN_STANDIN)
    base = "synthetic load-test passage about nothing in particular. "
    return (base * (chars // len(base) + 1))[:chars]


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_standin(cfg: StandinConfig, tmpdir: Path) -> tuple[subprocess.Popen, int]:
    port = free_port()
    cfgp = tmpdir / f"standin_{port}.json"
    cfgp.write_text(cfg.to_json(), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "standin.server", "--port", str(port), "--config", str(cfgp)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/_standin/config", timeout=0.5).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("stand-in did not start: " + (proc.stderr.read().decode()[:500] if proc.stderr else ""))


async def run_scenario(sc: Scenario, seed: int, run_id: str, out_dir: Path = RAW) -> dict:
    cfg = StandinConfig().with_(seed=seed, **sc.standin)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ".tmp"
    tmp.mkdir(exist_ok=True)
    proc, port = start_standin(cfg, tmp)
    rng = random.Random(seed)
    ts = arrivals(sc, rng)
    prios = [INTERACTIVE if rng.random() < sc.interactive_share else BATCH for _ in ts]
    believed = {"rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200_000, **sc.client_believes}
    client = LLMClient(f"http://127.0.0.1:{port}/openai/v1", "standin", "stand-in", "stand-in")
    if sc.hardened:
        policy = RetryPolicy(sc.strategy, max_attempts=sc.max_attempts, seed=seed)
        budget = RetryBudget()
    else:
        policy = RetryPolicy(sc.strategy, max_attempts=sc.max_attempts, seed=seed, respect_retry_after=False)
        budget = RetryBudget(ratio=1000.0, burst=10**6)
    gw = Gateway(client, Limiter(believed["rpm"], believed["tpm"], believed["rpd"], believed["tpd"], safety=0.9),
                 QueueManager(sc.concurrency, sc.max_queue), policy, budget, request_timeout_s=20.0,
                 admission=sc.hardened, local_pacing=sc.hardened)
    prompt = [{"role": "user", "content": prompt_for(sc.tokens_in)}]
    records, t_origin = [], time.monotonic()
    wall0 = datetime.now(timezone.utc)

    async def fire(i: int, t: float, prio: int):
        await asyncio.sleep(max(t_origin + t - time.monotonic(), 0))
        start = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        res = await gw.submit(f"{run_id}-{sc.name}-{i:04d}", prompt, MAX_TOKENS, prio, sc.deadline_s)
        records.append(to_record(res, request_id=f"{run_id}-{sc.name}-{i:04d}", scenario=sc.name,
                                 source="stand-in", start_time=start, priority=prio, run_id=run_id, seed=seed))

    try:
        await asyncio.gather(*[fire(i, t, p) for i, (t, p) in enumerate(zip(ts, prios))])
        stats = (await client._http.get(f"http://127.0.0.1:{port}/_standin/stats")).json()
    finally:
        await client.aclose()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    elapsed = time.monotonic() - t_origin
    records.sort(key=lambda r: r.start_time)
    base = out_dir / f"{run_id}__{sc.name}"
    for suffix in (".results.csv", ".diagnostics.csv"):
        if Path(str(base) + suffix).exists():
            raise FileExistsError(f"{base}{suffix} exists: raw results are immutable; use a new run id")
    append_records(records, Path(str(base) + ".results.csv"), Path(str(base) + ".diagnostics.csv"))
    meta = {"run_id": run_id, "scenario": asdict(sc), "seed": seed, "started_utc": wall0.isoformat(),
            "arrival_window_s": sc.duration_s, "wall_elapsed_s": round(elapsed, 1), "n_requests": len(records),
            "offered_rate_per_min": round(len(records) / (max(ts) - min(ts) + 1e-9) * 60, 2) if len(ts) > 1 else None,
            "offered_rate_per_min_nominal": sc.rate_per_min,
            "peak_queue": gw.queue.peak_queue, "peak_in_flight": gw.queue.peak_in_flight,
            "client_believed_limits": believed, "standin_config": json.loads(cfg.to_json()),
            "standin_stats": stats, "retry_policy": {"strategy": sc.strategy, "hardened": sc.hardened,
                                                      "max_attempts": sc.max_attempts},
            "python": sys.version.split()[0], "source": "stand-in",
            "workload": {"tokens_in_target": sc.tokens_in, "max_tokens": MAX_TOKENS,
                         "note": "gateway-level call, no retrieval, no verification call"}}
    Path(str(base) + ".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--all", action="store_true", help="run every scenario in parallel subprocesses")
    ap.add_argument("--only", nargs="+", choices=sorted(SCENARIOS), help="with --all: run just these scenarios")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("lt%Y%m%dT%H%M%S"))
    ap.add_argument("--out", type=Path, default=RAW)
    a = ap.parse_args()
    if a.all:
        procs = [subprocess.Popen([sys.executable, "-m", "experiments.load_test", "--scenario", n,
                                   "--seed", str(a.seed), "--run-id", a.run_id, "--out", str(a.out)], cwd=ROOT)
                 for n in (a.only or SCENARIOS)]
        codes = [p.wait() for p in procs]
        print("run id:", a.run_id, "| exit codes:", dict(zip(a.only or SCENARIOS, codes)))
        sys.exit(max(codes))
    if not a.scenario:
        ap.error("--scenario or --all required")
    meta = asyncio.run(run_scenario(SCENARIOS[a.scenario], a.seed, a.run_id, a.out))
    print(a.scenario, "done:", meta["n_requests"], "requests,", meta["wall_elapsed_s"], "s wall")


if __name__ == "__main__":
    main()
