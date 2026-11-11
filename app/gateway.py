"""Gateway: admission control + queue + limiter + retries around one LLMClient.

Request life cycle (each step records a timestamp for the latency breakdown):

  submit -> admission checks (can it ever fit? daily budget gone? queue full?)
         -> queue (priority, bounded, deadline = queue timeout)
         -> limiter wait (pace below TPM/RPM; shed if the wait cannot fit the deadline)
         -> one HTTP attempt
         -> on failure: classify -> retry policy (+ shared retry budget)
            backoff is slept WITHOUT holding a concurrency slot.

Status mapping used for results.csv (documented in docs/DECISIONS.md):
  ok            answer delivered
  rate_limited  failed because of a rate/capacity limit: provider 429, local limiter
                shed, queue full, daily budget exhausted, request too large
  timeout       gave up on time: queue timeout, deadline, provider timeout
  error         anything else (5xx exhausted, empty/incomplete stream, invalid request)
`final_reason` in diagnostics keeps the precise cause.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.llm import LLMClient, LLMResult
from app.limiter import Limiter
from app.metrics import Record
from app.queue_manager import INTERACTIVE, QueueFull, QueueManager, QueueTimeout
from app.retry import RetryBudget, RetryPolicy
from app.tokens import DEFAULT_EXPECTED_OUT, estimate_tokens, messages_chars

MESSAGES = {
    "ok": "",
    "queue_full": "The service is busy right now. Please try again in a minute.",
    "queue_timeout": "Your question waited too long in line and was dropped. Please try again.",
    "admission_wait": "The service is at capacity and cannot answer within your time limit. Please try again shortly.",
    "daily_budget": "Today's model capacity is used up, so no answer can be generated now. Please try again later.",
    "too_large": "This request is larger than the model's per-minute capacity and cannot be served.",
    "deadline": "The answer did not arrive in time. Please try again.",
    "rate_limited": "The model provider is rate-limiting requests. Please try again shortly.",
    "daily_exhausted": "Today's model capacity is used up, so no answer can be generated now. Please try again later.",
    "timeout": "The model provider timed out. Please try again.",
    "server": "The model provider returned an error. No answer was generated.",
    "incomplete_stream": "The answer was cut off before it finished, so it was not shown.",
    "empty_response": "The model returned no answer text.",
    "invalid": "The request was rejected by the provider as invalid.",
}
STATUS_OF = {"queue_full": "rate_limited", "admission_wait": "rate_limited", "daily_budget": "rate_limited",
             "too_large": "rate_limited", "rate_limited": "rate_limited", "daily_exhausted": "rate_limited",
             "queue_timeout": "timeout", "deadline": "timeout", "timeout": "timeout"}


@dataclass
class GatewayResult:
    ok: bool
    status: str
    final_reason: str
    text: str = ""
    llm: LLMResult | None = None
    attempts: int = 0
    first_token_ms: float | None = None      # from submit, includes queueing
    total_ms: float = 0.0
    queue_wait_ms: float = 0.0
    limiter_wait_ms: float = 0.0
    retry_delay_ms: float = 0.0
    model_ttft_ms: float | None = None
    model_total_ms: float | None = None
    est_tokens_in: int = 0
    queue_len_at_submit: int = 0
    user_message: str = ""
    stop_reason: str = ""                   # why no further retry (max_attempts, deadline, ...)


class Gateway:
    def __init__(self, client: LLMClient, limiter: Limiter, queue: QueueManager, policy: RetryPolicy,
                 budget: RetryBudget | None = None, request_timeout_s: float = 20.0,
                 admission: bool = True, local_pacing: bool = True):
        self.client, self.limiter, self.queue, self.policy = client, limiter, queue, policy
        self.budget = budget or RetryBudget()
        self.request_timeout_s = request_timeout_s
        self.admission, self.local_pacing = admission, local_pacing
        self.daily_blocked_until = 0.0
        self._inflight_ids: dict[str, asyncio.Future] = {}
        self.service_time_ema_s = 3.0          # starts at the baseline total p50 (~2.4 s) rounded up

    async def submit(self, request_id: str, messages: list[dict], max_tokens: int,
                     priority: int = INTERACTIVE, deadline_s: float = 30.0) -> GatewayResult:
        """Duplicate request ids in flight share one result (duplicate-request prevention)."""
        if request_id in self._inflight_ids:
            return await asyncio.shield(self._inflight_ids[request_id])
        fut = asyncio.get_running_loop().create_future()
        self._inflight_ids[request_id] = fut
        try:
            res = await self._run(messages, max_tokens, priority, deadline_s)
            fut.set_result(res)
            return res
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
                fut.exception()          # mark retrieved to avoid 'never retrieved' warnings
            raise
        finally:
            self._inflight_ids.pop(request_id, None)

    def _fail(self, r: GatewayResult, reason: str, t0: float) -> GatewayResult:
        r.ok, r.final_reason = False, reason
        r.status = STATUS_OF.get(reason, "error")
        r.user_message = MESSAGES.get(reason, MESSAGES["server"])
        r.total_ms = round((time.monotonic() - t0) * 1000, 1)
        return r

    async def _run(self, messages, max_tokens, priority, deadline_s) -> GatewayResult:
        clock = time.monotonic
        t0 = clock()
        deadline_at = t0 + deadline_s
        est_in = estimate_tokens(messages_chars(messages))
        est_total = est_in + min(max_tokens, DEFAULT_EXPECTED_OUT)
        r = GatewayResult(ok=False, status="error", final_reason="", est_tokens_in=est_in,
                          queue_len_at_submit=self.queue.queue_len)
        self.budget.note_first()

        if self.admission:
            if not self.limiter.can_ever_fit(est_total):
                return self._fail(r, "too_large", t0)
            if clock() < self.daily_blocked_until or self.limiter.daily_wait(est_total) > deadline_s:
                return self._fail(r, "daily_budget", t0)

        empty_done = 0
        last: LLMResult | None = None
        while True:
            remaining = deadline_at - clock()
            if remaining <= 0:
                return self._fail(r, "deadline", t0)
            # ---- wait for a concurrency slot ----
            try:
                async with self.queue.slot(priority, deadline_at) as waited:
                    r.queue_wait_ms += waited * 1000
                    # ---- pace below the limits ----
                    while self.local_pacing:
                        w = self.limiter.wait_needed(est_total)
                        if w <= 0:
                            break
                        remaining = deadline_at - clock()
                        if self.admission and w + self.service_time_ema_s > remaining:
                            return self._fail(r, "admission_wait", t0)
                        r.limiter_wait_ms += w * 1000
                        await asyncio.sleep(min(w, max(remaining, 0)))
                        if clock() >= deadline_at:
                            return self._fail(r, "deadline", t0)
                    if self.local_pacing:
                        self.limiter.reserve(est_total)
                    r.attempts += 1
                    t_attempt = clock()
                    timeout = min(self.request_timeout_s, max(deadline_at - t_attempt, 0.05))
                    last = await self.client.complete(messages, max_tokens, timeout)
            except QueueFull:
                return self._fail(r, "queue_full", t0)
            except QueueTimeout:
                return self._fail(r, "queue_timeout", t0)

            if self.local_pacing:
                refund_all = last.kind in ("rate_limited", "daily_exhausted", "server", "invalid")
                used = (last.tokens_in or 0) + (last.tokens_out or 0) if last.tokens_in is not None else None
                self.limiter.settle(est_total, 0 if refund_all and used is None else used)
            if last.total_ms and last.kind == "ok":
                self.service_time_ema_s = 0.8 * self.service_time_ema_s + 0.2 * last.total_ms / 1000

            if last.kind == "ok":
                r.ok, r.status, r.final_reason, r.text, r.llm = True, "ok", "ok", last.text, last
                r.model_ttft_ms, r.model_total_ms = last.ttft_ms, last.total_ms
                r.first_token_ms = round((t_attempt - t0) * 1000 + (last.ttft_ms or 0), 1)
                r.total_ms = round((clock() - t0) * 1000, 1)
                return r

            # ---- failure: shared cooldown, then retry decision ----
            r.llm = last
            if last.kind == "rate_limited" and last.retry_after_s and self.local_pacing:
                self.limiter.cool_down(last.retry_after_s)
            if last.kind == "daily_exhausted":
                self.daily_blocked_until = clock() + (last.retry_after_s or 3600)
            if last.kind == "empty_response":
                empty_done += 1
            remaining = deadline_at - clock()
            retry, wait, why = self.policy.decide(
                last.kind, r.attempts, remaining, retry_after_s=last.retry_after_s,
                expected_attempt_s=self.service_time_ema_s, empty_retries_done=empty_done - (1 if last.kind == "empty_response" else 0))
            if retry and not self.budget.try_spend():
                retry, why = False, "retry_budget"
            if not retry:
                r.stop_reason = why
                return self._fail(r, last.kind, t0)      # kind doubles as the precise final_reason
            r.retry_delay_ms += wait * 1000
            if wait > 0:
                await asyncio.sleep(wait)


def to_record(res: GatewayResult, *, request_id: str, scenario: str, source: str, start_time: str,
              priority: int | None = None, run_id: str = "", seed: int | None = None,
              retrieval_setting: str | None = None) -> Record:
    llm = res.llm if res.ok else None
    return Record(
        request_id=request_id, scenario=scenario, source=source, start_time=start_time, status=res.status,
        first_token_ms=res.first_token_ms if res.ok else None, total_ms=res.total_ms,
        retries=max(res.attempts - 1, 0), tokens_in=llm.tokens_in if llm else None,
        tokens_out=llm.tokens_out if llm else None, retrieval_setting=retrieval_setting,
        final_reason=res.final_reason, priority=priority, attempts=res.attempts,
        queue_wait_ms=res.queue_wait_ms, limiter_wait_ms=res.limiter_wait_ms,
        model_ttft_ms=res.model_ttft_ms, model_total_ms=res.model_total_ms,
        retry_delay_ms=res.retry_delay_ms, est_tokens_in=res.est_tokens_in,
        queue_len_at_submit=res.queue_len_at_submit, user_message=res.user_message,
        run_id=run_id, seed=seed)
