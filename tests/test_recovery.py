"""Recovery + reservation reconciliation (simulated transport; nothing touches Groq)."""
import asyncio

import httpx

from app.gateway import Gateway
from app.limiter import Limiter
from app.llm import LLMClient
from app.queue_manager import QueueManager
from app.retry import RetryBudget, RetryPolicy

MSG = [{"role": "user", "content": "hello there"}]
OK = ('data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
      'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"x_groq":{"usage":{"prompt_tokens":20,"completion_tokens":30}}}\n\n'
      "data: [DONE]\n\n")


def gw_with(handler, lim):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient("http://t", "k", "m", "real", http=http)
    return Gateway(client, lim, QueueManager(2, 20), RetryPolicy("fixed", fixed_s=0.01, max_attempts=1),
                   RetryBudget(1.0, 50), request_timeout_s=2.0, admission=True, local_pacing=True)


def test_outage_then_recovery_and_balances_reconcile():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] <= 3:                       # provider outage for the first 3 requests
            return httpx.Response(503, text="down")
        return httpx.Response(200, text=OK, headers={"content-type": "text/event-stream"})

    lim = Limiter(1000, 8000, 10**6, 10**9, safety=1.0)
    gw = gw_with(handler, lim)

    async def go():
        bad = [await gw.submit(f"b{i}", MSG, 100) for i in range(3)]
        lim.tpm.refill(lim.clock())
        after_fail = lim.tpm.level                # failed calls reported no usage: reservations fully refunded
        good = await gw.submit("g", MSG, 100)
        lim.tpm.refill(lim.clock())
        return bad, after_fail, good, lim.tpm.level

    bad, after_fail, good, after_ok = asyncio.run(go())
    assert [r.status for r in bad] == ["error"] * 3 and all(not r.ok for r in bad)
    assert after_fail > 7990                       # only the tiny refill drift, nothing leaked
    assert good.ok and good.text == "answer"       # next valid request succeeds after the outage
    # settled to provider-reported usage (20 + 30 = 50 tokens), not the larger reservation
    assert 8000 - after_ok < 80
    assert lim.tpd.level >= lim.tpd.capacity - 80 and lim.rpd.level >= lim.rpd.capacity - 4


def test_temporary_tpm_exhaustion_recovers_after_refill_without_provider_calls_being_lost():
    t = {"now": 1000.0}
    lim = Limiter(1000, 6000, 10**6, 10**9, safety=1.0, clock=lambda: t["now"])
    lim.reserve(6000)                              # drain the minute bucket
    assert not lim.can_ever_fit(6001) and lim.can_ever_fit(3000)
    w = lim.wait_needed(3000)
    assert 29 < w < 31                             # 3000 tokens at 100/s
    t["now"] += w + 0.01
    assert lim.wait_needed(3000) == 0.0            # recovered; exhaustion was temporary, not permanent
