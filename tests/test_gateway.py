"""LLM client + gateway against a REAL local stand-in server (sockets), plus mocked-transport cases.
Everything here is simulated; nothing touches Groq."""
import asyncio
import json
import time

import httpx
import pytest

from app.gateway import Gateway, to_record
from app.limiter import Limiter
from app.llm import LLMClient, SourceMismatch, classify_http
from app.queue_manager import BATCH, INTERACTIVE, QueueManager
from app.retry import RetryBudget, RetryPolicy
from tests.helpers_server import fast_cfg, running_standin

MSG = [{"role": "user", "content": "hello there"}]


def mk_gateway(base, *, tpm=8000, rpm=1000, rpd=10**6, tpd=10**9, conc=4, max_queue=20, policy=None,
               pacing=True, admission=True, timeout=5.0, budget=None):
    client = LLMClient(base, "k", "stand-in", "stand-in")
    return Gateway(client, Limiter(rpm, tpm, rpd, tpd, safety=1.0), QueueManager(conc, max_queue),
                   policy or RetryPolicy("fixed", fixed_s=0.05, max_attempts=3), budget or RetryBudget(1.0, 50),
                   request_timeout_s=timeout, admission=admission, local_pacing=pacing)


def run(coro):
    return asyncio.run(coro)


# ---------------- LLM client over mocked transport ----------------
def client_with(handler, source="real"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMClient("http://t", "k", "m", source, http=http)


def test_client_classifies_http_errors():
    assert classify_http(429, '{"error":{"message":"Rate limit on tokens per minute (TPM)"}}', 2)[0] == "rate_limited"
    assert classify_http(429, "requests per day (RPD) reached", 5)[0] == "daily_exhausted"
    assert classify_http(429, "x", 3600)[0] == "daily_exhausted"        # huge Retry-After => daily
    # regression: a per-minute limit in deep debt (retry storm) has a huge Retry-After but is NOT a daily limit
    assert classify_http(429, "Rate limit reached on requests per minute (RPM): Limit 20", 5000)[0] == "rate_limited"
    assert classify_http(429, "tokens per minute (TPM)", 5000)[0] == "rate_limited"
    assert classify_http(503, "", None)[0] == "server" and classify_http(500, "", None)[0] == "server"
    assert classify_http(400, "", None)[0] == "invalid" and classify_http(413, "", None)[0] == "invalid"
    assert classify_http(504, "", None) == ("timeout", "timeout")


def test_client_429_keeps_body_headers_retry_after_and_never_fakes_text():
    def h(req):
        return httpx.Response(429, json={"error": {"message": "Rate limit reached. Please try again in 1.5s."}},
                              headers={"retry-after": "2", "x-ratelimit-remaining-tokens": "0"})
    r = run(client_with(h).complete(MSG, 10, 5))
    assert (r.kind, r.status, r.text, r.retry_after_s) == ("rate_limited", 429, "", 2.0)
    assert "Rate limit reached" in r.error_body and r.rate_headers["rl_remaining_tokens"] == "0"


def test_client_retry_after_falls_back_to_body_text():
    r = run(client_with(lambda q: httpx.Response(429, text="Please try again in 7.5s")).complete(MSG, 10, 5))
    assert r.retry_after_s == 7.5


def test_client_non_streaming_ok_and_empty():
    ok = lambda q: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    r = run(client_with(ok).complete(MSG, 10, 5, stream=False))
    assert (r.kind, r.text, r.tokens_in, r.tokens_out) == ("ok", "hi", 3, 2)
    empty = lambda q: httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    assert run(client_with(empty).complete(MSG, 10, 5, stream=False)).kind == "empty_response"


def test_source_guard_both_directions():
    plain = lambda q: httpx.Response(200, text="data: [DONE]\n\n")
    stamped = lambda q: httpx.Response(200, text="data: [DONE]\n\n", headers={"x-standin": "1"})
    with pytest.raises(SourceMismatch):
        run(client_with(stamped, "real").complete(MSG, 10, 5))
    with pytest.raises(SourceMismatch):
        run(client_with(plain, "stand-in").complete(MSG, 10, 5))
    with pytest.raises(ValueError):
        LLMClient("http://t", "k", "m", "simulated")


# ---------------- gateway over the stand-in ----------------
def test_success_records_timings_and_usage():
    with running_standin(fast_cfg(seed=1)) as (base, _):
        gw = mk_gateway(base)
        res = run(gw.submit("r1", MSG, 500))
    assert res.ok and res.status == "ok" and res.attempts == 1 and res.text.startswith("[stand-in]")
    assert res.first_token_ms is not None and res.first_token_ms <= res.total_ms
    assert res.llm.tokens_in and res.llm.tokens_out
    rec = to_record(res, request_id="r1", scenario="t", source="stand-in", start_time="x")
    rec.validate()
    assert rec.retries == 0 and rec.tokens_out == res.llm.tokens_out


def test_429_is_retried_only_after_retry_after_and_then_succeeds():
    calls = []
    sse = ('data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
           'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"x_groq":{"usage":{"prompt_tokens":5,"completion_tokens":3}}}\n\n'
           'data: [DONE]\n\n')

    def handler(req):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "1"})
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    client = LLMClient("http://t", "k", "m", "real", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gw = Gateway(client, Limiter(1000, 8000, 10**6, 10**9, safety=1.0), QueueManager(2, 5),
                 RetryPolicy("jitter", max_attempts=3), RetryBudget(1.0, 5), local_pacing=True)
    res = run(gw.submit("r", MSG, 50))
    assert res.ok and res.attempts == 2 and res.text == "answer"
    assert calls[1] - calls[0] >= 0.95                 # never retried before Retry-After
    assert res.retry_delay_ms >= 950 and res.queue_wait_ms < 100


def test_retry_after_beyond_deadline_gives_up_immediately_as_rate_limited():
    handler = lambda req: httpx.Response(429, text="slow", headers={"retry-after": "60"})
    client = LLMClient("http://t", "k", "m", "real", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gw = Gateway(client, Limiter(1000, 8000, 10**6, 10**9, safety=1.0), QueueManager(2, 5),
                 RetryPolicy("jitter", max_attempts=3), RetryBudget(1.0, 5))
    t = time.monotonic()
    res = run(gw.submit("r", MSG, 50, deadline_s=10))
    assert res.status == "rate_limited" and res.attempts == 1 and res.stop_reason == "deadline"
    assert time.monotonic() - t < 1.0                  # did not sleep 60 s


def test_local_pacing_avoids_429s_that_unpaced_client_hits():
    with running_standin(fast_cfg(rpm=600, tpm=1500, seed=2)) as (base, root):
        # each request ~ (70+ small) prompt tokens + ~60 completion; 12 requests slightly exceed a 1500-token bucket
        gw = mk_gateway(base, tpm=1500, rpm=600, conc=1)
        async def go():
            return [await gw.submit(f"r{i}", MSG, 100) for i in range(12)]
        res = run(go())
        stats = httpx.get(root + "/_standin/stats").json()
    assert all(r.ok for r in res)
    assert stats["limited_tpm"] == 0 and sum(r.limiter_wait_ms for r in res) > 0


def test_daily_exhaustion_is_not_retried_and_then_fails_fast_without_calling_provider():
    with running_standin(fast_cfg(rpd=1, seed=1)) as (base, root):
        gw = mk_gateway(base, pacing=False)
        async def go():
            a = await gw.submit("a", MSG, 100)
            b = await gw.submit("b", MSG, 100)
            n_after_b = httpx.get(root + "/_standin/stats").json()["requests"]
            c = await gw.submit("c", MSG, 100)
            n_after_c = httpx.get(root + "/_standin/stats").json()["requests"]
            return a, b, c, n_after_b, n_after_c
        a, b, c, nb, nc = run(go())
    assert a.ok
    assert b.status == "rate_limited" and b.final_reason == "daily_exhausted" and b.attempts == 1
    assert c.status == "rate_limited" and c.final_reason == "daily_budget" and c.attempts == 0
    assert nb == nc == 2                       # request c never reached the provider
    assert "capacity" in c.user_message and c.text == "" and not c.ok


def test_server_errors_retry_up_to_max_attempts_then_report_error_without_fake_answer():
    with running_standin(fast_cfg(p_500=1.0)) as (base, root):
        gw = mk_gateway(base, policy=RetryPolicy("fixed", fixed_s=0.01, max_attempts=3))
        res = run(gw.submit("x", MSG, 100))
        n = httpx.get(root + "/_standin/stats").json()["requests"]
    assert (res.ok, res.status, res.final_reason, res.attempts, n) == (False, "error", "server", 3, 3)
    assert res.text == "" and res.first_token_ms is None


def test_timeout_status_and_deadline_prevents_pointless_retry():
    with running_standin(fast_cfg(p_hang=1.0, hang_s=3.0)) as (base, root):
        gw = mk_gateway(base, timeout=0.3, policy=RetryPolicy("fixed", fixed_s=0.0, max_attempts=3))
        res = run(gw.submit("x", MSG, 100, deadline_s=0.5))   # no room for a second 0.3 s attempt + EMA
    assert res.status == "timeout" and res.attempts == 1


def test_incomplete_stream_is_not_success():
    with running_standin(fast_cfg(p_interrupt=1.0, ms_per_token=0.5)) as (base, _):
        res = run(mk_gateway(base, policy=RetryPolicy("fixed", fixed_s=0.0, max_attempts=2)).submit("x", MSG, 500))
    assert not res.ok and res.final_reason == "incomplete_stream" and res.status == "error"


def test_queue_full_rejects_immediately_with_honest_message():
    with running_standin(fast_cfg(ttfr_median_ms=400)) as (base, _):
        gw = mk_gateway(base, conc=1, max_queue=1)
        async def go():
            return await asyncio.gather(*[gw.submit(f"r{i}", MSG, 100) for i in range(4)])
        res = run(go())
    reasons = sorted(r.final_reason for r in res)
    assert reasons.count("queue_full") == 2 and reasons.count("ok") == 2
    full = next(r for r in res if r.final_reason == "queue_full")
    assert full.status == "rate_limited" and full.total_ms < 100 and "busy" in full.user_message


def test_queue_timeout_status():
    with running_standin(fast_cfg(ttfr_median_ms=600)) as (base, _):
        gw = mk_gateway(base, conc=1, max_queue=5)
        async def go():
            return await asyncio.gather(gw.submit("a", MSG, 100, deadline_s=5),
                                        gw.submit("b", MSG, 100, deadline_s=0.2))
        a, b = run(go())
    assert a.ok and b.final_reason == "queue_timeout" and b.status == "timeout"


def test_request_too_large_for_tpm_is_refused_locally():
    with running_standin(fast_cfg()) as (base, root):
        gw = mk_gateway(base, tpm=1000)
        res = run(gw.submit("big", [{"role": "user", "content": "x" * 10_000}], 100))
        n = httpx.get(root + "/_standin/stats").json()["requests"]
    assert res.final_reason == "too_large" and res.attempts == 0 and n == 0


def test_duplicate_request_id_shares_one_provider_call():
    with running_standin(fast_cfg()) as (base, root):
        gw = mk_gateway(base)
        async def go():
            return await asyncio.gather(gw.submit("same", MSG, 100), gw.submit("same", MSG, 100))
        a, b = run(go())
        n = httpx.get(root + "/_standin/stats").json()["requests"]
    assert a is b and n == 1


def test_priority_interactive_served_before_queued_batch():
    with running_standin(fast_cfg(ttfr_median_ms=150)) as (base, _):
        gw = mk_gateway(base, conc=1, max_queue=10)
        order = []
        async def job(name, pr):
            r = await gw.submit(name, MSG, 100, priority=pr)
            order.append(name)
        async def go():
            t = [asyncio.create_task(job("first", BATCH))]
            await asyncio.sleep(0.03)
            t.append(asyncio.create_task(job("batch2", BATCH))); await asyncio.sleep(0.01)
            t.append(asyncio.create_task(job("inter", INTERACTIVE)))
            await asyncio.gather(*t)
        run(go())
    assert order == ["first", "inter", "batch2"]


def test_retry_budget_stops_retry_storm():
    with running_standin(fast_cfg(p_503=1.0)) as (base, root):
        gw = mk_gateway(base, budget=RetryBudget(ratio=0.0, burst=2),
                        policy=RetryPolicy("immediate", max_attempts=10))
        async def go():
            return [await gw.submit(f"r{i}", MSG, 50) for i in range(6)]
        res = run(go())
        n = httpx.get(root + "/_standin/stats").json()["requests"]
    assert sum(r.attempts - 1 for r in res) == 2 and n == 6 + 2     # only the burst allowance was retried


def test_source_never_mislabelled_end_to_end():
    with running_standin(fast_cfg()) as (base, _):
        bad = LLMClient(base, "k", "m", "real")
        with pytest.raises(SourceMismatch):
            run(bad.complete(MSG, 10, 5))
