"""Bounded priority queue in front of a fixed number of concurrency slots.

Policy (and why):
  * Fixed in-flight cap (max_concurrency): the provider's in-flight limit is
    unknown, and more concurrency than TPM can feed only lengthens waits.
  * Bounded queue (max_queue): a queue that can grow forever turns overload into
    unbounded latency; past the bound we reject immediately ("busy") so the user
    gets an honest answer in milliseconds instead of a timeout in 30 s.
  * Priority (lower number first, FIFO within a priority): interactive questions
    overtake batch/evaluation work when capacity is scarce.
  * Queue timeout = the request's own deadline: waiting past it is pointless.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from contextlib import asynccontextmanager
from typing import Callable

INTERACTIVE, BATCH = 0, 1


class QueueFull(Exception):
    """Rejected at admission: too many requests already waiting."""


class QueueTimeout(Exception):
    """The deadline passed while waiting for a slot."""


class QueueManager:
    def __init__(self, max_concurrency: int, max_queue: int, clock: Callable[[], float] = time.monotonic):
        self.max_concurrency, self.max_queue, self.clock = max_concurrency, max_queue, clock
        self.in_flight = 0
        self._heap: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()
        self._waiting = 0
        self.peak_queue = 0
        self.peak_in_flight = 0

    @property
    def queue_len(self) -> int:
        return self._waiting

    async def acquire(self, priority: int, deadline_at: float) -> float:
        """Wait for a slot. Returns seconds spent waiting."""
        t0 = self.clock()
        if self.in_flight < self.max_concurrency and self._waiting == 0:
            self._take()
            return 0.0
        if self._waiting >= self.max_queue:
            raise QueueFull(f"queue full ({self._waiting}/{self.max_queue})")
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._seq), fut))
        self._waiting += 1
        self.peak_queue = max(self.peak_queue, self._waiting)
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=max(deadline_at - self.clock(), 0.0))
        except asyncio.TimeoutError:
            if fut.done() and not fut.cancelled():
                # slot was granted at the last instant; give it back
                self._release_slot()
            else:
                fut.cancel()
            self._waiting -= 1
            raise QueueTimeout("deadline passed while queued") from None
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self._release_slot()
            else:
                fut.cancel()
            self._waiting -= 1
            raise
        self._waiting -= 1
        return self.clock() - t0

    def _take(self) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def _release_slot(self) -> None:
        self.in_flight -= 1
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                self._take()
                fut.set_result(True)
                return

    def release(self) -> None:
        self._release_slot()

    @asynccontextmanager
    async def slot(self, priority: int, deadline_at: float):
        waited = await self.acquire(priority, deadline_at)
        try:
            yield waited
        finally:
            self.release()
