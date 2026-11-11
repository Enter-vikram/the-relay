"""Load-test runner smoke tests (short, stand-in only) + CSV integrity of what it writes."""
import asyncio
import csv
import json
import random

import pytest

from app.metrics import DIAG_COLUMNS, GRADES, RESULT_COLUMNS, SOURCES, STATUSES
from experiments.load_test import SCENARIOS, Scenario, arrivals, run_scenario


def tiny(**kw):
    base = dict(name="tiny", description="smoke", duration_s=6, rate_per_min=30, tokens_in=300,
                standin={"ttfr_median_ms": 60.0, "ms_per_token": 0.1, "content_tail_ms_mean": 5.0,
                         "tokens_out_median": 50.0}, deadline_s=8.0)
    base.update(kw)
    return Scenario(**base)


def test_arrivals_are_seeded_and_burst_is_inside_its_window():
    sc = SCENARIOS["burst"]
    a, b = arrivals(sc, random.Random(5)), arrivals(sc, random.Random(5))
    assert a == b and a != arrivals(sc, random.Random(6))
    in_burst = [t for t in a if sc.burst_at_s <= t <= sc.burst_at_s + sc.burst_width_s]
    assert len(in_burst) >= sc.burst_n


def test_mandatory_scenarios_exist():
    for n in ("steady", "burst", "overload", "limit_halved", "daily_exhausted"):
        assert n in SCENARIOS
    assert {"retry_naive_immediate", "retry_hardened"} <= set(SCENARIOS)


def test_runner_writes_valid_labelled_raw_files_and_refuses_to_overwrite(tmp_path):
    meta = asyncio.run(run_scenario(tiny(), 3, "t1", tmp_path))
    res = tmp_path / "t1__tiny.results.csv"
    rows = list(csv.DictReader(res.open(encoding="utf-8")))
    assert len(rows) == meta["n_requests"] >= 2 and list(rows[0].keys()) == RESULT_COLUMNS
    assert {r["source"] for r in rows} == {"stand-in"} and meta["source"] == "stand-in"
    assert {r["status"] for r in rows} <= STATUSES and all(r["answer_grade"] == "" for r in rows)
    assert len({r["request_id"] for r in rows}) == len(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    assert ok and all(float(r["first_token_ms"]) <= float(r["total_ms"]) for r in ok)
    assert all(r["first_token_ms"] == "" for r in rows if r["status"] != "ok")
    diag = list(csv.DictReader((tmp_path / "t1__tiny.diagnostics.csv").open(encoding="utf-8")))
    assert list(diag[0].keys()) == DIAG_COLUMNS and len(diag) == len(rows)
    m = json.loads((tmp_path / "t1__tiny.meta.json").read_text())
    assert m["standin_config"]["seed"] == 3 and "provenance" in m["standin_config"]
    with pytest.raises(FileExistsError):
        asyncio.run(run_scenario(tiny(), 3, "t1", tmp_path))


def test_daily_exhaustion_scenario_logic_fails_fast_after_budget(tmp_path):
    sc = tiny(name="dx", duration_s=10, rate_per_min=60, standin={**tiny().standin, "rpd": 3},
              client_believes={"rpd": 1000})
    meta = asyncio.run(run_scenario(sc, 1, "t2", tmp_path))
    rows = list(csv.DictReader((tmp_path / "t2__dx.results.csv").open(encoding="utf-8")))
    diag = {r["request_id"]: r for r in csv.DictReader((tmp_path / "t2__dx.diagnostics.csv").open(encoding="utf-8"))}
    oks = [r for r in rows if r["status"] == "ok"]
    assert len(oks) == 3
    reasons = [diag[r["request_id"]]["final_reason"] for r in rows if r["status"] != "ok"]
    assert "daily_exhausted" in reasons and reasons.count("daily_exhausted") == 1   # one probe, then fail fast
    assert all(x in ("daily_exhausted", "daily_budget") for x in reasons)
    assert meta["standin_stats"]["requests"] == len(oks) + 1                       # provider hit only once after budget
