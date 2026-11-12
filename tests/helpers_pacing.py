"""Deterministic test double that models the REAL gateway's pacing semantics on a virtual clock:
wait_needed -> (shed if the wait cannot fit the deadline) -> advance clock -> reserve -> service time -> settle.
It exercises the real Limiter and says nothing about provider behaviour."""
from app.gateway import GatewayResult
from app.limiter import Limiter
from app.llm import LLMResult
from app.queue_manager import QueueManager
from app.tokens import DEFAULT_EXPECTED_OUT, estimate_tokens


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += max(s, 0.0)


class PacingGateway:
    def __init__(self, script, *, tpm=8000, rpm=30, rpd=1000, tpd=200_000, service_s=3.0,
                 tokens_in=1600, tokens_out=900, safety=0.9):
        self.clock = FakeClock()
        self.limiter = Limiter(rpm, tpm, rpd, tpd, safety=safety, clock=self.clock)
        self.queue = QueueManager(4, 10)
        self.script, self.service_s, self.tin, self.tout = list(script), service_s, tokens_in, tokens_out
        self.calls, self.waits, self.min_level = [], [], float("inf")

    async def submit(self, request_id, messages, max_tokens, priority=0, deadline_s=30.0):
        self.calls.append((request_id, messages))
        lim = self.limiter
        est = estimate_tokens(sum(len(m["content"]) for m in messages)) + min(max_tokens, DEFAULT_EXPECTED_OUT)
        if not lim.can_ever_fit(est):
            return GatewayResult(False, "rate_limited", "too_large", "", None, user_message="too large")
        w = lim.wait_needed(est)
        if w > deadline_s:
            return GatewayResult(False, "rate_limited", "admission_wait", "", None, user_message="at capacity")
        self.waits.append(w)
        self.clock.advance(w)
        lim.reserve(est)
        self.clock.advance(self.service_s)
        lim.settle(est, self.tin + self.tout)
        self.min_level = min(self.min_level, lim.tpm.level)
        item = self.script.pop(0) if self.script else "scripted"
        llm = LLMResult("ok", "ok", 200, item, ttft_ms=50.0, total_ms=self.service_s * 1000,
                        tokens_in=self.tin, tokens_out=self.tout)
        return GatewayResult(True, "ok", "ok", item, llm, attempts=1, first_token_ms=55.0,
                             total_ms=self.service_s * 1000, model_ttft_ms=50.0,
                             model_total_ms=self.service_s * 1000, est_tokens_in=est)
