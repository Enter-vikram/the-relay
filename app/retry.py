"""Explicit retry policy (no SDK retries anywhere).

Strategies compared in the load experiments: immediate, fixed, exponential,
jitter (exponential with full jitter). Policy decisions are pure functions of
(failure kind, attempt, remaining deadline), so they are unit-testable without
sleeping.

Retry rules (why):
  rate_limited(429)   retry only after Retry-After (never before: an early retry is
                      guaranteed to fail and just burns a slot). If Retry-After
                      exceeds the remaining deadline, give up now.
  daily_exhausted     never retry (waiting hours cannot help a 30 s request).
  server (500/503)    retry with the configured backoff.
  timeout             retry only if the deadline leaves room for another attempt.
  incomplete_stream   retry (provider cut the stream), same budget rules.
  empty_response      retry once at most (usually reasoning ate max_tokens; a
                      retry rarely helps, so it is capped separately).
  invalid (4xx!=429)  never retry.
A shared RetryBudget caps retries as a fraction of first attempts so a bad
minute cannot turn into a retry storm.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

STRATEGIES = ("immediate", "fixed", "exponential", "jitter")
NEVER = {"invalid", "daily_exhausted", "ok"}


@dataclass
class RetryPolicy:
    strategy: str = "jitter"
    max_attempts: int = 3            # total attempts including the first
    base_s: float = 0.5
    cap_s: float = 8.0
    fixed_s: float = 1.0
    max_empty_retries: int = 1
    respect_retry_after: bool = True   # False = naive client (used to demonstrate retry storms)
    seed: int = 0
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}")
        self._rng = random.Random(self.seed)

    def backoff(self, attempt: int) -> float:
        """Delay before attempt number `attempt+1` (attempt is 1-based, the one that failed)."""
        if self.strategy == "immediate":
            return 0.0
        if self.strategy == "fixed":
            return self.fixed_s
        ceiling = min(self.cap_s, self.base_s * (2 ** (attempt - 1)))
        if self.strategy == "exponential":
            return ceiling
        return self._rng.uniform(0.0, ceiling)        # full jitter

    def decide(self, kind: str, attempt: int, remaining_s: float, *,
               retry_after_s: float | None = None, expected_attempt_s: float = 0.0,
               empty_retries_done: int = 0) -> tuple[bool, float, str]:
        """Return (retry?, wait_seconds, reason)."""
        if kind in NEVER:
            return False, 0.0, f"{kind}: not retryable"
        if attempt >= self.max_attempts:
            return False, 0.0, "max_attempts"
        if kind == "empty_response" and empty_retries_done >= self.max_empty_retries:
            return False, 0.0, "empty_retry_cap"
        if kind == "rate_limited":
            wait = retry_after_s if (retry_after_s is not None and self.respect_retry_after) \
                else self.backoff(attempt)
        elif kind in ("server", "timeout", "incomplete_stream", "empty_response"):
            wait = self.backoff(attempt)
            if retry_after_s is not None:
                wait = max(wait, retry_after_s)
        else:
            return False, 0.0, f"unknown kind {kind}"
        if wait + expected_attempt_s > remaining_s:
            return False, 0.0, "deadline"
        return True, wait, kind


@dataclass
class RetryBudget:
    """Allow at most `ratio` retries per first attempt (plus `burst` free ones)."""
    ratio: float = 0.3
    burst: int = 3
    firsts: int = 0
    retries: int = 0

    def note_first(self) -> None:
        self.firsts += 1

    def try_spend(self) -> bool:
        if self.retries < self.burst + self.ratio * self.firsts:
            self.retries += 1
            return True
        return False
