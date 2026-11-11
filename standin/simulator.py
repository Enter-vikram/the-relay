"""Pure stand-in logic: admission (limits, faults, overload) and response planning.

No web framework here, so everything is unit-testable with an injected clock and
a seeded RNG. Deliberately independent of app/limiter.py: the thing being
tested (the client-side limiter) must not share code with the thing it is
tested against (the provider model).

NOT Groq. What it does represent: continuous-refill token buckets for RPD and
TPM that reproduce the baseline header arithmetic, the reasoning-then-content
timing shape, and the 429/Retry-After contract from the docs. What it does NOT
represent: Groq's real in-flight limit, real 429 body text, queueing inside
Groq, network jitter, other tenants' load, or tail behaviour (n=5).
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from standin.config import StandinConfig


@dataclass
class _Bucket:
    cap: float
    rate: float            # units per second
    level: float = 0.0
    t: float = 0.0

    def __post_init__(self):
        self.level = self.cap

    def refill(self, now: float):
        if self.t:
            self.level = min(self.cap, self.level + (now - self.t) * self.rate)
        self.t = now

    def wait_for(self, amount: float, now: float) -> float:
        self.refill(now)
        return 0.0 if self.level >= amount else (amount - self.level) / self.rate


@dataclass
class Decision:
    kind: str                      # accept | limit | fault | hang | overload
    status: int = 200
    retry_after_s: float | None = None
    err_type: str = ""
    message: str = ""


@dataclass
class Plan:
    ttfr_ms: float                 # first reasoning chunk
    content_start_ms: float        # first content chunk
    content_end_ms: float
    tokens_out: int
    reasoning_tokens: int
    content_tokens: int
    finish_reason: str
    interrupt_at_ms: float | None = None


def fmt_duration(s: float) -> str:
    """Groq-style: '7.66s', '1m26.4s', '2m59.56s', '7m12s'."""
    s = max(s, 0.0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    sec_txt = f"{sec:.3f}".rstrip("0").rstrip(".")
    out = ""
    if h:
        out += f"{int(h)}h"
    if h or m:
        out += f"{int(m)}m"
    if sec_txt != "0" or not out:
        out += f"{sec_txt}s"
    return out


@dataclass
class Stats:
    requests: int = 0
    accepted: int = 0
    limited_tpm: int = 0
    limited_rpm: int = 0
    limited_daily: int = 0
    faults_500: int = 0
    faults_503: int = 0
    overload_503: int = 0
    hangs: int = 0
    interrupted: int = 0
    peak_inflight: int = 0
    peak_provider_waiting: int = 0


class Simulator:
    def __init__(self, cfg: StandinConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg, self.clock = cfg, clock
        self.rng = random.Random(cfg.seed)
        self.rpm = _Bucket(cfg.rpm, cfg.rpm / 60)
        self.tpm = _Bucket(cfg.tpm, cfg.tpm / 60)
        self.rpd = _Bucket(cfg.rpd, cfg.rpd / 86400)
        self.tpd = _Bucket(cfg.tpd, cfg.tpd / 86400)
        self.inflight = 0
        self.provider_waiting = 0
        self.overload_until = 0.0
        self.stats = Stats()

    # ---- token accounting ----
    def prompt_tokens(self, chars: int) -> int:
        c = self.cfg
        return int(round(c.prompt_overhead_tokens + chars / c.chars_per_token))

    def charge_completion(self, tokens: int) -> None:
        now = self.clock()
        for b in (self.tpm, self.tpd):
            b.refill(now)
            b.level -= tokens          # may go negative (debt), as in a bucket with post-paid usage

    # ---- admission ----
    def admit(self, prompt_tokens: int, max_tokens: int) -> Decision:
        c, st, now = self.cfg, self.stats, self.clock()
        st.requests += 1
        if now < self.overload_until:
            st.overload_503 += 1
            return Decision("overload", 503, self.overload_until - now, "overloaded",
                            "Service temporarily overloaded; recovering.")
        r = self.rng.random()
        if r < c.p_hang:
            st.hangs += 1
            return Decision("hang")
        r -= c.p_hang
        if r < c.p_500:
            st.faults_500 += 1
            return Decision("fault", 500, None, "server_error", "Internal server error (injected).")
        r -= c.p_500
        if r < c.p_503:
            st.faults_503 += 1
            return Decision("fault", 503, 1.0, "unavailable", "Service unavailable (injected).")

        need_tok = prompt_tokens + (max_tokens if c.tpm_admit_includes_max_tokens else 0)
        w = self.rpd.wait_for(1, now)
        if w > 0:
            st.limited_daily += 1
            return Decision("limit", 429, w, "requests",
                            f"Rate limit reached on requests per day (RPD): Limit {c.rpd}. "
                            f"Please try again in {fmt_duration(w)}.")
        w = self.tpd.wait_for(prompt_tokens, now)
        if w > 0:
            st.limited_daily += 1
            return Decision("limit", 429, w, "tokens",
                            f"Rate limit reached on tokens per day (TPD): Limit {c.tpd}. "
                            f"Please try again in {fmt_duration(w)}.")
        w = self.rpm.wait_for(1, now)
        if w > 0:
            if c.count_rejected_rpm:
                self.rpm.level -= 1          # assumption: rejected attempts still spend RPM budget
            st.limited_rpm += 1
            return Decision("limit", 429, w, "requests",
                            f"Rate limit reached on requests per minute (RPM): Limit {c.rpm}. "
                            f"Please try again in {fmt_duration(w)}.")
        w = self.tpm.wait_for(need_tok, now)
        if w > 0:
            if c.count_rejected_rpm:
                self.rpm.level -= 1
            st.limited_tpm += 1
            return Decision("limit", 429, w, "tokens",
                            f"Rate limit reached on tokens per minute (TPM): Limit {c.tpm}, "
                            f"Requested {need_tok}. Please try again in {fmt_duration(w)}.")
        if self.inflight >= c.max_inflight and self.provider_waiting >= c.provider_queue_limit:
            self.overload_until = now + c.recovery_s
            st.overload_503 += 1
            return Decision("overload", 503, c.recovery_s, "overloaded",
                            "Service overloaded: too many concurrent requests.")
        # accept: reserve request + prompt tokens now, completion tokens later
        self.rpm.level -= 1
        self.rpd.level -= 1
        self.tpm.level -= prompt_tokens
        self.tpd.level -= prompt_tokens
        st.accepted += 1
        return Decision("accept")

    # ---- planning a response ----
    def plan(self, max_tokens: int, inflight_now: int) -> Plan:
        c, rng = self.cfg, self.rng
        mult = 1.0 + c.load_sensitivity * inflight_now / max(c.max_inflight, 1)
        ttfr = math.exp(rng.gauss(math.log(c.ttfr_median_ms), c.ttfr_sigma)) * mult
        if rng.random() < c.tail_prob:
            ttfr *= c.tail_multiplier
        raw = max(20, int(round(math.exp(rng.gauss(math.log(c.tokens_out_median), c.tokens_out_sigma)))))
        reasoning = int(round(raw * c.reasoning_fraction))
        finish = "stop"
        if raw > max_tokens:
            raw, finish = max_tokens, "length"
        reasoning = min(reasoning, raw)
        content = raw - reasoning
        if finish == "stop" and reasoning >= max_tokens:
            finish = "length"
        content_start = ttfr + c.ms_per_token * raw * mult
        tail = rng.expovariate(1.0 / c.content_tail_ms_mean) * mult if c.content_tail_ms_mean > 0 else 0.0
        if content == 0:
            content_start = content_end = ttfr + c.ms_per_token * raw * mult
        else:
            content_end = content_start + tail
        interrupt = None
        if rng.random() < c.p_interrupt:
            self.stats.interrupted += 1
            interrupt = ttfr + (content_end - ttfr) * rng.uniform(0.2, 0.9)
        return Plan(ttfr, content_start, content_end, raw, reasoning, content, finish, interrupt)

    # ---- headers (semantics per Groq docs: *-requests = RPD, *-tokens = TPM) ----
    def rate_headers(self) -> dict[str, str]:
        now, c = self.clock(), self.cfg
        self.rpd.refill(now)
        self.tpm.refill(now)
        rem_req = max(int(self.rpd.level), 0)
        rem_tok = max(int(self.tpm.level), 0)
        return {
            "x-ratelimit-limit-requests": str(c.rpd),
            "x-ratelimit-limit-tokens": str(c.tpm),
            "x-ratelimit-remaining-requests": str(rem_req),
            "x-ratelimit-remaining-tokens": str(rem_tok),
            "x-ratelimit-reset-requests": fmt_duration((c.rpd - self.rpd.level) / self.rpd.rate),
            "x-ratelimit-reset-tokens": fmt_duration(max(c.tpm - self.tpm.level, 0) / self.tpm.rate),
        }
