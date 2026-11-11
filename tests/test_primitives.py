"""Retry policy, limiter, queue, metrics. No network, no real sleeping except tiny asyncio waits."""
import asyncio
import csv

import pytest

from app.limiter import Limiter
from app.metrics import (DIAG_COLUMNS, RESULT_COLUMNS, Record, append_records, percentile, summarize)
from app.queue_manager import BATCH, INTERACTIVE, QueueFull, QueueManager, QueueTimeout
from app.retry import RetryBudget, RetryPolicy


# ---------------- retry ----------------
def test_backoff_shapes():
    assert RetryPolicy("immediate").backoff(3) == 0.0
    assert RetryPolicy("fixed", fixed_s=2).backoff(5) == 2
    p = RetryPolicy("exponential", base_s=0.5, cap_s=4)
    assert [p.backoff(i) for i in (1, 2, 3, 4, 5)] == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_jitter_is_bounded_and_seeded():
    a = [RetryPolicy("jitter", base_s=1, cap_s=8, seed=7).backoff(3) for _ in range(1)]
    b = [RetryPolicy("jitter", base_s=1, cap_s=8, seed=7).backoff(3) for _ in range(1)]
    assert a == b and 0 <= a[0] <= 4
    p = RetryPolicy("jitter", base_s=1, cap_s=8, seed=1)
    assert all(0 <= p.backoff(2) <= 2 for _ in range(200))


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        RetryPolicy("yolo")


def test_429_waits_exactly_retry_after_and_respects_deadline():
    p = RetryPolicy("jitter")
    ok, wait, _ = p.decide("rate_limited", 1, remaining_s=30, retry_after_s=4.0)
    assert ok and wait == 4.0
    ok, _, why = p.decide("rate_limited", 1, remaining_s=3, retry_after_s=4.0)
    assert not ok and why == "deadline"


@pytest.mark.parametrize("kind", ["invalid", "daily_exhausted"])
def test_never_retry_kinds(kind):
    assert not RetryPolicy().decide(kind, 1, 100)[0]


def test_max_attempts_and_empty_cap():
    p = RetryPolicy("fixed", max_attempts=2)
    assert p.decide("server", 1, 100)[0]
    assert p.decide("server", 2, 100) == (False, 0.0, "max_attempts")
    p3 = RetryPolicy("fixed", max_attempts=5, max_empty_retries=1)
    assert p3.decide("empty_response", 1, 100, empty_retries_done=1)[1:] == (0.0, "empty_retry_cap")


def test_timeout_retry_needs_time_for_another_attempt():
    p = RetryPolicy("fixed", fixed_s=1)
    assert not p.decide("timeout", 1, remaining_s=5, expected_attempt_s=10)[0]
    assert p.decide("timeout", 1, remaining_s=20, expected_attempt_s=10)[0]


def test_retry_budget_caps_storm():
    b = RetryBudget(ratio=0.2, burst=1)
    for _ in range(10):
        b.note_first()
    spent = sum(b.try_spend() for _ in range(50))
    assert spent == 3          # burst 1 + 0.2*10 = 3 (strictly below 3 -> allows 3 total)


# ---------------- limiter ----------------
class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def test_limiter_paces_tpm_and_refills():
    c = Clock()
    lim = Limiter(rpm=1000, tpm=6000, rpd=10**6, tpd=10**9, safety=1.0, clock=c)
    assert lim.wait_needed(4000) == 0
    lim.reserve(4000)
    w = lim.wait_needed(4000)                  # need 2000 more tokens at 100/s
    assert w == pytest.approx(20.0, rel=0.01)
    c.t += 20
    assert lim.wait_needed(4000) == pytest.approx(0, abs=1e-6)


def test_limiter_rpm_and_cooldown():
    c = Clock()
    lim = Limiter(rpm=60, tpm=10**9, rpd=10**6, tpd=10**12, safety=1.0, clock=c)
    for _ in range(60):
        lim.reserve(1)
    assert lim.wait_needed(1) == pytest.approx(1.0, rel=0.05)   # 1 req/s refill
    lim2 = Limiter(rpm=60, tpm=10**9, rpd=10**6, tpd=10**12, clock=c)
    lim2.cool_down(7)
    assert lim2.wait_needed(1) == pytest.approx(7, rel=0.01)


def test_limiter_settle_refunds_over_estimate_and_charges_under():
    c = Clock()
    lim = Limiter(1000, 6000, 10**6, 10**9, safety=1.0, clock=c)
    lim.reserve(3000)
    lim.settle(3000, 1000)                      # used less -> refund 2000
    assert lim.tpm.level == pytest.approx(5000)
    lim.settle(1000, 3000)                      # used more -> charge
    assert lim.tpm.level == pytest.approx(3000)


def test_limiter_daily_wait_is_huge_when_budget_gone():
    c = Clock()
    lim = Limiter(1000, 10**9, rpd=10, tpd=10**12, safety=1.0, clock=c)
    for _ in range(10):
        lim.reserve(1)
    assert lim.daily_wait(1) > 3600           # 86400/10 s per request refill
    assert not lim.can_ever_fit(10**10)


# ---------------- queue ----------------
def run(coro):
    return asyncio.run(coro)


def test_queue_caps_concurrency_and_tracks_peak():
    async def go():
        q = QueueManager(2, 10)
        live, maxlive = 0, 0

        async def job():
            nonlocal live, maxlive
            async with q.slot(INTERACTIVE, q.clock() + 5):
                live += 1; maxlive = max(maxlive, live)
                await asyncio.sleep(0.02)
                live -= 1
        await asyncio.gather(*[job() for _ in range(6)])
        return maxlive, q.peak_queue, q.in_flight
    maxlive, peak, inflight = run(go())
    assert maxlive == 2 and peak >= 3 and inflight == 0


def test_queue_full_rejects_immediately():
    async def go():
        q = QueueManager(1, 1)
        hold = asyncio.Event()

        async def holder():
            async with q.slot(0, q.clock() + 5):
                await hold.wait()
        t1 = asyncio.create_task(holder()); await asyncio.sleep(0.01)
        t2 = asyncio.create_task(holder()); await asyncio.sleep(0.01)   # fills the queue (1)
        with pytest.raises(QueueFull):
            await q.acquire(0, q.clock() + 5)
        hold.set(); await asyncio.gather(t1, t2)
    run(go())


def test_queue_timeout_when_deadline_passes_and_slot_not_leaked():
    async def go():
        q = QueueManager(1, 5)
        hold = asyncio.Event()

        async def holder():
            async with q.slot(0, q.clock() + 5):
                await hold.wait()
        t = asyncio.create_task(holder()); await asyncio.sleep(0.01)
        with pytest.raises(QueueTimeout):
            await q.acquire(0, q.clock() + 0.05)
        assert q.queue_len == 0
        hold.set(); await t
        assert q.in_flight == 0
        async with q.slot(0, q.clock() + 1):          # still usable afterwards
            pass
    run(go())


def test_priority_interactive_overtakes_batch():
    async def go():
        q = QueueManager(1, 10)
        order = []
        hold = asyncio.Event()

        async def holder():
            async with q.slot(0, q.clock() + 5):
                await hold.wait()

        async def waiter(name, pr):
            async with q.slot(pr, q.clock() + 5):
                order.append(name)
        t = asyncio.create_task(holder()); await asyncio.sleep(0.01)
        ws = [asyncio.create_task(waiter("batch1", BATCH))]; await asyncio.sleep(0.005)
        ws.append(asyncio.create_task(waiter("batch2", BATCH))); await asyncio.sleep(0.005)
        ws.append(asyncio.create_task(waiter("inter", INTERACTIVE))); await asyncio.sleep(0.005)
        hold.set(); await asyncio.gather(t, *ws)
        return order
    assert run(go()) == ["inter", "batch1", "batch2"]


# ---------------- metrics / CSV ----------------
def test_percentile_matches_numpy():
    import numpy as np
    xs = [5, 1, 9, 3, 7, 11, 2]
    for q in (0, 50, 95, 99, 100):
        assert percentile(xs, q) == pytest.approx(float(np.percentile(xs, q)))
    assert percentile([], 50) is None and summarize([]) == {"n": 0}


def test_results_csv_schema_and_empty_for_missing(tmp_path):
    r = Record("r1", "steady", "stand-in", "2026-01-01T00:00:00Z", "rate_limited")
    ok = Record("r2", "steady", "stand-in", "2026-01-01T00:00:01Z", "ok", first_token_ms=12.34,
                total_ms=20.0, tokens_in=10, tokens_out=5)
    res, diag = tmp_path / "results.csv", tmp_path / "diag.csv"
    append_records([r, ok], res, diag)
    rows = list(csv.DictReader(res.open(encoding="utf-8")))
    assert list(rows[0].keys()) == RESULT_COLUMNS
    assert rows[0]["first_token_ms"] == "" and rows[0]["tokens_in"] == ""   # not 0
    assert rows[1]["first_token_ms"] == "12.3"
    assert list(csv.DictReader(diag.open(encoding="utf-8")).fieldnames) == DIAG_COLUMNS


def test_append_only_and_header_guard(tmp_path):
    res, diag = tmp_path / "r.csv", tmp_path / "d.csv"
    append_records([Record("a", "s", "real", "t", "ok")], res, diag)
    append_records([Record("b", "s", "real", "t", "ok")], res, diag)
    assert len(res.read_text().splitlines()) == 3
    res.write_text("wrong,header\n")
    with pytest.raises(ValueError):
        append_records([Record("c", "s", "real", "t", "ok")], res, diag)


@pytest.mark.parametrize("kw", [{"source": "sim"}, {"status": "failed"}, {"answer_grade": "meh"}])
def test_record_validation(kw):
    base = dict(request_id="x", scenario="s", source="real", start_time="t", status="ok")
    base.update(kw)
    with pytest.raises(ValueError):
        Record(**base).validate()
