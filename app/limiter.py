"""Client-side pacing that mirrors the provider's limit *types*.

Why local limiting at all: a 429 costs a round trip, a wasted queue slot and (if
retried early) extra load. Pacing below the limit converts most would-be 429s
into short, predictable waits, and admission control rejects work that cannot
finish inside its deadline BEFORE it consumes capacity.

Models (all continuous refills, matching what baseline_v2.csv headers imply):
  RPM  token bucket, capacity rpm,  refill rpm/60 per s
  TPM  token bucket, capacity tpm,  refill tpm/60 per s (can go into debt)
  RPD  token bucket, capacity rpd,  refill rpd/86400 per s
  TPD  token bucket, capacity tpd,  refill tpd/86400 per s
A shared cooldown (set from a provider 429 Retry-After) pauses everyone, so
concurrent workers do not each rediscover the limit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

Clock = Callable[[], float]


@dataclass
class Bucket:
    capacity: float
    per_second: float
    level: float = field(init=False)
    t: float = 0.0

    def __post_init__(self) -> None:
        self.level = self.capacity

    def refill(self, now: float) -> None:
        if self.t:
            self.level = min(self.capacity, self.level + (now - self.t) * self.per_second)
        self.t = now

    def wait_for(self, amount: float, now: float) -> float:
        self.refill(now)
        if self.level >= amount:
            return 0.0
        return (amount - self.level) / self.per_second


class Limiter:
    def __init__(self, rpm: int, tpm: int, rpd: int, tpd: int, safety: float = 0.9,
                 clock: Clock = time.monotonic):
        self.clock = clock
        s = safety
        self.rpm = Bucket(rpm * s, rpm * s / 60)
        self.tpm = Bucket(tpm * s, tpm * s / 60)
        self.rpd = Bucket(rpd * s, rpd * s / 86400)
        self.tpd = Bucket(tpd * s, tpd * s / 86400)
        self.cooldown_until = 0.0
        self.tpm_capacity = tpm * s

    def can_ever_fit(self, tokens: int) -> bool:
        """A single request larger than the per-minute capacity can never be served."""
        return tokens <= self.tpm_capacity

    def wait_needed(self, est_tokens: int) -> float:
        """Seconds until a request of est_tokens may start. Does not reserve."""
        now = self.clock()
        w = max(self.cooldown_until - now, 0.0)
        for b, amt in ((self.rpm, 1), (self.tpm, est_tokens), (self.rpd, 1), (self.tpd, est_tokens)):
            w = max(w, b.wait_for(amt, now))
        return w

    def daily_wait(self, est_tokens: int) -> float:
        now = self.clock()
        return max(self.rpd.wait_for(1, now), self.tpd.wait_for(est_tokens, now))

    def reserve(self, est_tokens: int) -> None:
        now = self.clock()
        for b, amt in ((self.rpm, 1), (self.tpm, est_tokens), (self.rpd, 1), (self.tpd, est_tokens)):
            b.refill(now)
            b.level -= amt

    def settle(self, est_tokens: int, actual_tokens: int | None) -> None:
        """Replace the reservation by the provider-reported usage (refund/charge difference)."""
        if actual_tokens is None:
            return
        diff = est_tokens - actual_tokens
        for b in (self.tpm, self.tpd):
            b.level = min(b.capacity, b.level + diff)

    def cool_down(self, seconds: float) -> None:
        self.cooldown_until = max(self.cooldown_until, self.clock() + seconds)
