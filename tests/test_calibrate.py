"""Tests for experiments/calibrate_from_baseline.py using synthetic rows."""
import pandas as pd
import pytest

from experiments.baseline import FIELDS
from experiments.calibrate_from_baseline import analyze, parse_duration


@pytest.mark.parametrize("s,expected", [
    ("1m26.4s", 86.4), ("2.107s", 2.107), ("7m12s", 432.0), ("1h2m3.5s", 3723.5),
    ("120ms", 0.12), ("59.56s", 59.56), ("2m59.56s", 179.56), ("0s", 0.0)])
def test_parse_duration(s, expected):
    assert parse_duration(s) == pytest.approx(expected)


@pytest.mark.parametrize("s", ["", None, float("nan")])
def test_parse_duration_empty_is_none_not_zero(s):
    assert parse_duration(s) is None


@pytest.mark.parametrize("s", ["abc", "5x", "m"])
def test_parse_duration_garbage_raises(s):
    with pytest.raises(ValueError):
        parse_duration(s)


def row(**kw):
    base = {k: "" for k in FIELDS}
    base.update(outcome="ok", first_token_ms=2000, first_reasoning_ms=1000, total_ms=2100,
                tokens_in=80, tokens_out=300, finish_reason="stop", rl_limit_requests=1000,
                rl_limit_tokens=8000)
    base.update(kw)
    return base


def frame(rows):
    return pd.DataFrame(rows, columns=FIELDS)


def consistent_rows():
    rows = []
    for i in range(1, 6):
        used_tokens = 100 * i
        rows.append(row(rl_remaining_requests=1000 - i, rl_reset_requests=f"{86.4 * i:.1f}s",
                        rl_remaining_tokens=8000 - used_tokens,
                        rl_reset_tokens=f"{used_tokens / (8000 / 60):.3f}s"))
    return rows


def test_detects_daily_refill_and_per_minute_token_bucket():
    a = analyze(frame(consistent_rows()))
    assert a["requests_per_day"]["consistent_with_continuous_daily_refill"] is True
    assert a["requests_per_day"]["expected_if_limit_is_per_day"] == pytest.approx(86.4)
    assert a["tokens_per_minute"]["consistent_with_continuous_per_minute_refill"] is True
    assert a["requests_per_day"]["observed_limit_header"] == [1000]
    assert a["tokens_per_minute"]["observed_limit_header"] == [8000]


def test_inconsistent_headers_are_not_called_consistent():
    rows = consistent_rows()
    rows[2]["rl_reset_requests"] = "10s"          # breaks the 86.4 s/request pattern
    a = analyze(frame(rows))
    assert a["requests_per_day"]["consistent_with_continuous_daily_refill"] is False


def test_small_sample_warning_and_unobservables_listed():
    a = analyze(frame(consistent_rows()))
    assert "n<30" in a["warning"]
    assert any("RPM" in x for x in a["not_observable_from_headers"])
    assert any("TPD" in x for x in a["not_observable_from_headers"])


def test_non_ok_rows_excluded_from_latency_but_counted():
    rows = consistent_rows() + [row(outcome="rate_limited", first_token_ms="", total_ms=3,
                                    rl_remaining_requests="", rl_reset_requests="")]
    a = analyze(frame(rows))
    assert a["outcomes"] == {"ok": 5, "rate_limited": 1}
    assert a["latency_ms_ok_rows"]["total"]["n"] == 5


def test_missing_header_columns_do_not_crash_or_invent_numbers():
    a = analyze(frame([row(outcome="timeout", first_token_ms="", total_ms=60000)]))
    assert a["latency_ms_ok_rows"]["total"] == {"n": 0}
    assert "seconds_per_request_refill" not in a["requests_per_day"]
    assert "implied_refill_tokens_per_s" not in a["tokens_per_minute"]


def test_tpm_capacity_estimate_uses_observed_token_sizes():
    a = analyze(frame(consistent_rows()))
    t = a["tokens_per_minute"]
    assert t["tokens_per_request_mean_ok_rows"] == 380.0       # 80 + 300
    assert t["max_requests_per_min_by_tpm_at_that_size"] == pytest.approx(8000 / 380, abs=0.1)
