"""Stand-in: calibration provenance, limit semantics, faults, streaming, determinism."""
import asyncio
import json
import re

import httpx
import pytest

from standin.config import StandinConfig
from standin.simulator import Simulator, fmt_duration
from tests.helpers_server import fast_cfg, running_standin


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


# ---------- calibration ----------
def test_defaults_equal_values_derived_from_baseline_csv():
    d = StandinConfig.derive_observed()
    c = StandinConfig()
    assert d.pop("_n") == 5
    for k, v in d.items():
        assert getattr(c, k) == pytest.approx(v), f"default {k} drifted from baseline_v2.csv"


def test_every_parameter_has_provenance():
    c = StandinConfig()
    for k in c.__dataclass_fields__:
        if k in ("provenance", "seed", "hang_s", "tail_multiplier"):
            continue
        assert k in c.provenance, f"{k} has no provenance label"
    assert set(c.provenance.values()) >= {"assumed", "documented"}


def test_fmt_duration_matches_groq_doc_examples():
    assert fmt_duration(7.66) == "7.66s" and fmt_duration(179.56) == "2m59.56s"
    assert fmt_duration(432) == "7m12s" and fmt_duration(86.4) == "1m26.4s"


def test_headers_reproduce_baseline_arithmetic():
    """Same inference the real data supports: reset-requests = used*86.4 s; token refill 133.3/s."""
    from experiments.calibrate_from_baseline import parse_duration as pd_
    clk = Clock()
    sim = Simulator(StandinConfig(), clk)
    sim.admit(81, 300)
    sim.charge_completion(193)
    h = sim.rate_headers()
    assert h["x-ratelimit-limit-requests"] == "1000" and h["x-ratelimit-limit-tokens"] == "8000"
    assert h["x-ratelimit-remaining-requests"] == "999"
    assert pd_(h["x-ratelimit-reset-requests"]) == pytest.approx(86.4, abs=0.01)
    used = 8000 - int(h["x-ratelimit-remaining-tokens"])
    assert used == 81 + 193
    assert used / pd_(h["x-ratelimit-reset-tokens"]) == pytest.approx(8000 / 60, rel=0.01)


# ---------- limits ----------
def test_tpm_429_with_correct_retry_after():
    clk = Clock()
    sim = Simulator(StandinConfig(), clk)
    assert sim.admit(5000, 100).kind == "accept"
    d = sim.admit(5000, 100)                      # only 3000 left; needs 2000 more at 133.3/s = 15 s
    assert d.kind == "limit" and d.status == 429 and d.err_type == "tokens"
    assert d.retry_after_s == pytest.approx(15.0, rel=0.01)
    clk.t += 15.1
    assert sim.admit(5000, 100).kind == "accept"  # recovered exactly when told


def test_rpm_limit_and_recovery():
    clk = Clock()
    sim = Simulator(StandinConfig().with_(rpm=6), clk)
    assert all(sim.admit(10, 10).kind == "accept" for _ in range(6))
    d = sim.admit(10, 10)
    assert d.status == 429 and d.retry_after_s == pytest.approx(10.0, rel=0.01)
    clk.t += 10.1
    assert sim.admit(10, 10).kind == "accept"


def test_daily_budget_exhaustion_gives_long_retry_after():
    clk = Clock()
    sim = Simulator(StandinConfig().with_(rpd=3, rpm=1000), clk)
    for _ in range(3):
        assert sim.admit(10, 10).kind == "accept"
    d = sim.admit(10, 10)
    assert d.status == 429 and d.err_type == "requests" and d.retry_after_s > 3600


def test_prompt_larger_than_tpm_never_fits_even_after_waiting():
    sim = Simulator(StandinConfig(), Clock())
    d = sim.admit(9000, 10)
    assert d.status == 429  # needs more than the bucket capacity; retry_after is finite but futile
    assert d.retry_after_s > 0


def test_overload_then_recovery_window():
    clk = Clock()
    cfg = StandinConfig().with_(max_inflight=1, provider_queue_limit=1, recovery_s=5, rpm=1000, tpm=10**7)
    sim = Simulator(cfg, clk)
    sim.inflight, sim.provider_waiting = 1, 1
    d = sim.admit(10, 10)
    assert d.kind == "overload" and d.status == 503
    sim.inflight = sim.provider_waiting = 0
    assert sim.admit(10, 10).status == 503        # still inside the recovery window
    clk.t += 5.1
    assert sim.admit(10, 10).kind == "accept"


# ---------- model of latency ----------
def test_plan_is_deterministic_for_a_seed_and_differs_across_seeds():
    a = [Simulator(StandinConfig().with_(seed=3)).plan(900, 1) for _ in range(1)]
    b = [Simulator(StandinConfig().with_(seed=3)).plan(900, 1) for _ in range(1)]
    c = [Simulator(StandinConfig().with_(seed=4)).plan(900, 1) for _ in range(1)]
    assert a == b and a != c


def test_load_degrades_latency():
    cfg = StandinConfig().with_(seed=1, load_sensitivity=1.0, max_inflight=4)
    lo = sum(Simulator(cfg.with_(seed=s)).plan(900, 1).content_start_ms for s in range(50))
    hi = sum(Simulator(cfg.with_(seed=s)).plan(900, 4).content_start_ms for s in range(50))
    assert hi / lo == pytest.approx((1 + 1.0) / (1 + 0.25), rel=0.05)


def test_empty_response_when_reasoning_eats_max_tokens():
    sim = Simulator(StandinConfig().with_(seed=1))
    p = sim.plan(max_tokens=20, inflight_now=1)
    assert p.finish_reason == "length" and p.content_tokens == 0 and p.tokens_out == 20


def test_distribution_roughly_matches_calibration():
    sim = Simulator(StandinConfig().with_(seed=11))
    xs = sorted(sim.plan(10**6, 0).ttfr_ms for _ in range(4000))
    med = xs[len(xs) // 2]
    assert med == pytest.approx(1360, rel=0.05)


# ---------- HTTP server (real sockets) ----------
async def _post(base, stream=True, **body):
    payload = {"model": "x", "messages": [{"role": "user", "content": "hello"}], "stream": stream,
               "stream_options": {"include_usage": True}, "max_tokens": 500, **body}
    async with httpx.AsyncClient(timeout=10) as c:
        async with c.stream("POST", base + "/chat/completions", json=payload) as r:
            lines = [l async for l in r.aiter_lines()] if r.status_code == 200 else []
            err = (await r.aread()).decode() if r.status_code != 200 else ""
            return r.status_code, dict(r.headers), lines, err


def test_stream_shape_reasoning_then_content_then_done_with_usage():
    with running_standin(fast_cfg(seed=2)) as (base, _):
        st, h, lines, _ = asyncio.run(_post(base))
    assert st == 200 and h["x-standin"] == "1"
    data = [json.loads(l[5:]) for l in lines if l.startswith("data:") and "[DONE]" not in l]
    kinds = []
    for e in data:
        for ch in e.get("choices", []):
            d = ch["delta"]
            if d.get("reasoning"): kinds.append("r")
            if d.get("content"): kinds.append("c")
    assert kinds and kinds[0] == "r" and "c" in kinds and kinds.index("c") > kinds.index("r")
    assert lines[-2].strip() == "data: [DONE]" or "[DONE]" in "".join(lines)
    assert any(e.get("usage") for e in data) and any((e.get("x_groq") or {}).get("usage") for e in data)


def test_429_over_http_has_retry_after_and_ratelimit_headers():
    with running_standin(fast_cfg(rpm=1)) as (base, _):
        s1 = asyncio.run(_post(base, stream=False))
        s2 = asyncio.run(_post(base, stream=False))
    assert s1[0] == 200 and s2[0] == 429
    assert int(s2[1]["retry-after"]) >= 1 and "x-ratelimit-remaining-tokens" in s2[1]
    assert json.loads(s2[3])["error"]["code"] == "rate_limit_exceeded"


def test_injected_500_and_503():
    with running_standin(fast_cfg(p_500=1.0)) as (base, _):
        assert asyncio.run(_post(base, stream=False))[0] == 500
    with running_standin(fast_cfg(p_503=1.0)) as (base, _):
        r = asyncio.run(_post(base, stream=False))
    assert r[0] == 503 and r[1]["retry-after"] == "1"


def test_interrupted_stream_has_no_done():
    with running_standin(fast_cfg(p_interrupt=1.0, ttfr_median_ms=30, ms_per_token=0.5)) as (base, _):
        _, _, lines, _ = asyncio.run(_post(base))
    assert not any("[DONE]" in l for l in lines)


def test_hang_makes_client_time_out():
    with running_standin(fast_cfg(p_hang=1.0, hang_s=3.0)) as (base, _):
        async def go():
            async with httpx.AsyncClient(timeout=0.3) as c:
                await c.post(base + "/chat/completions", json={"messages": [], "max_tokens": 5})
        with pytest.raises(httpx.TimeoutException):
            asyncio.run(go())


def test_runtime_config_patch_resets_state_and_applies():
    with running_standin(fast_cfg()) as (base, root):
        async def go():
            async with httpx.AsyncClient() as c:
                r = await c.post(root + "/_standin/config", json={"rpm": 1})
                assert r.json()["rpm"] == 1
                a = await c.post(base + "/chat/completions", json={"messages": [], "max_tokens": 5})
                b = await c.post(base + "/chat/completions", json={"messages": [], "max_tokens": 5})
                s = (await c.get(root + "/_standin/stats")).json()
                return a.status_code, b.status_code, s
        a, b, s = asyncio.run(go())
    assert (a, b) == (200, 429) and s["limited_rpm"] == 1


def test_rejected_requests_can_extend_lockout_when_assumption_enabled():
    """Assumption flag (NOT a Groq fact): rejected attempts spend RPM budget, so hammering lengthens the wait."""
    def lockout(flag):
        clk = Clock()
        sim = Simulator(StandinConfig().with_(rpm=6, count_rejected_rpm=flag), clk)
        for _ in range(6):
            sim.admit(10, 10)
        for _ in range(30):                       # a retry storm during lockout
            d = sim.admit(10, 10)
        return d.retry_after_s
    assert lockout(True) > 3 * lockout(False)
