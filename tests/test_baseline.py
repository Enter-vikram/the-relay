"""Tests for experiments/baseline.py. All HTTP is mocked: zero Groq quota used."""
import asyncio
import json

import httpx
import pytest

from experiments.baseline import FIELDS, open_results, one_request

SECRET = "gsk_TEST_SECRET_DO_NOT_LEAK"


# ---------- helpers ----------
def chunk(content=None, reasoning=None, finish=None) -> str:
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    ch = {"delta": delta}
    if finish:
        ch["finish_reason"] = finish
    return "data: " + json.dumps({"choices": [ch]}) + "\n\n"


def usage_chunk(i, o, x_groq=False) -> str:
    u = {"prompt_tokens": i, "completion_tokens": o}
    evt = {"choices": [], "x_groq": {"usage": u}} if x_groq else {"choices": [], "usage": u}
    return "data: " + json.dumps(evt) + "\n\n"


DONE = "data: [DONE]\n\n"


def sse(*parts: str) -> httpx.Response:
    return httpx.Response(200, content="".join(parts).encode(),
                          headers={"content-type": "text/event-stream"})


class SlowStream(httpx.AsyncByteStream):
    """Yields parts with real delays so timing assertions are meaningful.
    If `then_raise` is set, raises it after the last part (simulated cut)."""
    def __init__(self, parts, delay=0.05, then_raise=None):
        self.parts, self.delay, self.then_raise = parts, delay, then_raise

    async def __aiter__(self):
        for p in self.parts:
            yield p.encode()
            await asyncio.sleep(self.delay)
        if self.then_raise:
            raise self.then_raise


def run(handler, timeout_s=5.0):
    """Run one_request through a mocked transport; returns (row, call_count)."""
    calls = []

    def counting(request):
        calls.append(request)
        return handler(request)

    async def go():
        transport = httpx.MockTransport(counting)
        async with httpx.AsyncClient(transport=transport) as client:
            return await one_request(client, "t", 0, "hi", 100, timeout_s,
                                     api_key=SECRET, base_url="http://test", model="m")

    return asyncio.run(go()), len(calls)


# ---------- 1. success ----------
def test_success_stream_is_ok():
    row, calls = run(lambda r: sse(chunk("Hello"), chunk(" world", finish="stop"),
                                   usage_chunk(12, 7), DONE))
    assert row["outcome"] == "ok" and row["status"] == 200
    assert row["content_chars"] == len("Hello world")
    assert row["done_received"] is True and row["usage_received"] is True
    assert row["finish_reason"] == "stop"
    assert row["first_token_ms"] != "" and row["total_ms"] >= row["first_token_ms"]
    assert calls == 1


# ---------- 2. no [DONE] ----------
def test_stream_without_done_is_incomplete():
    row, _ = run(lambda r: sse(chunk("partial answer"), usage_chunk(5, 3)))
    assert row["outcome"] == "incomplete_stream"
    assert row["done_received"] is False
    assert row["content_chars"] > 0  # partial content kept as evidence, not success


def test_connection_cut_mid_stream_is_incomplete():
    stream = SlowStream([chunk("part")], delay=0.0,
                        then_raise=httpx.RemoteProtocolError("peer closed connection"))
    row, calls = run(lambda r: httpx.Response(200, stream=stream))
    assert row["outcome"] == "incomplete_stream"
    assert "RemoteProtocolError" in row["error_body"]
    assert row["first_token_ms"] != ""
    assert calls == 1


# ---------- 3. empty completed stream ----------
def test_done_without_content_is_empty_response():
    row, _ = run(lambda r: sse(chunk(finish="stop"), usage_chunk(5, 0), DONE))
    assert row["outcome"] == "empty_response"
    assert row["first_token_ms"] == ""


def test_reasoning_only_is_empty_response_and_never_a_ttft():
    # Reasoning model burned its whole budget thinking: finish_reason=length.
    row, _ = run(lambda r: sse(chunk(reasoning="thinking..."),
                               chunk(finish="length"), usage_chunk(5, 100), DONE))
    assert row["outcome"] == "empty_response"
    assert row["first_token_ms"] == ""          # reasoning is not user-visible
    assert row["first_reasoning_ms"] != ""
    assert row["finish_reason"] == "length"


# ---------- 4. HTTP 429 ----------
def test_429_recorded_with_body_and_headers_and_no_retry():
    body = {"error": {"message": "Rate limit reached", "type": "tokens"}}
    row, calls = run(lambda r: httpx.Response(
        429, json=body, headers={"retry-after": "7", "x-ratelimit-remaining-tokens": "0"}))
    assert row["outcome"] == "rate_limited" and row["status"] == 429
    assert row["retry_after"] == "7" and row["rl_remaining_tokens"] == "0"
    assert "Rate limit reached" in row["error_body"]
    assert calls == 1  # no hidden retry


# ---------- 5. other HTTP errors ----------
@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_other_http_errors_keep_body_and_do_not_retry(status):
    row, calls = run(lambda r: httpx.Response(status, text=f"boom-{status}"))
    assert row["outcome"] == "error" and row["status"] == status
    assert row["error_body"] == f"boom-{status}"
    assert row["tokens_out"] == "" and calls == 1


def test_malformed_sse_json_is_error_not_swallowed():
    row, _ = run(lambda r: sse(chunk("ok so far"), "data: {not json\n\n", DONE))
    assert row["outcome"] == "error"
    assert "bad SSE JSON" in row["error_body"]


def test_error_event_inside_stream_is_error():
    evt = "data: " + json.dumps({"error": {"message": "overloaded"}}) + "\n\n"
    row, _ = run(lambda r: sse(chunk("hi"), evt, DONE))
    assert row["outcome"] == "error" and "overloaded" in row["error_body"]


# ---------- 6. timeouts ----------
def test_httpx_read_timeout_recorded():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    row, calls = run(handler)
    assert row["outcome"] == "timeout" and row["status"] == ""
    assert calls == 1


def test_total_deadline_timeout_recorded():
    async def slow(request):
        await asyncio.sleep(1.0)
        return sse(chunk("late"), DONE)
    row, _ = run(slow, timeout_s=0.05)
    assert row["outcome"] == "timeout"
    assert row["total_ms"] < 500  # gave up at the deadline, did not wait 1s


# ---------- 7. content after reasoning ----------
def test_ttft_measures_content_not_reasoning():
    stream = SlowStream([chunk(reasoning="step 1"), chunk(reasoning=" step 2"),
                         chunk("Answer", finish="stop"), usage_chunk(9, 40), DONE],
                        delay=0.05)
    row, _ = run(lambda r: httpx.Response(200, stream=stream))
    assert row["outcome"] == "ok"
    assert row["content_chars"] == len("Answer")
    assert row["reasoning_chars"] == len("step 1") + len(" step 2")
    # Content starts ~2 delays (~100 ms) after reasoning; allow Windows timer slop.
    assert row["first_token_ms"] - row["first_reasoning_ms"] >= 60
    assert row["first_token_ms"] <= row["total_ms"]


# ---------- 8. token usage ----------
def test_usage_from_top_level_field():
    row, _ = run(lambda r: sse(chunk("x"), usage_chunk(123, 45), DONE))
    assert (row["tokens_in"], row["tokens_out"], row["usage_received"]) == (123, 45, True)


def test_usage_from_x_groq_field():
    row, _ = run(lambda r: sse(chunk("x"), usage_chunk(11, 22, x_groq=True), DONE))
    assert (row["tokens_in"], row["tokens_out"], row["usage_received"]) == (11, 22, True)


def test_missing_usage_is_blank_not_zero():
    row, _ = run(lambda r: sse(chunk("x"), DONE))
    assert row["outcome"] == "ok"  # usage is tracked separately from success
    assert row["tokens_in"] == "" and row["tokens_out"] == ""
    assert row["usage_received"] is False


# ---------- secrets ----------
def test_api_key_never_in_row_even_if_server_echoes_it_in_headers():
    row, _ = run(lambda r: sse(chunk("x"), DONE))
    assert all(SECRET not in str(v) for v in row.values())
    assert not any("auth" in k.lower() for k in row)


def test_request_sends_key_only_as_bearer_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return sse(chunk("x"), DONE)

    run(handler)
    assert seen["auth"] == f"Bearer {SECRET}"
    assert SECRET not in seen["body"]


# ---------- CSV schema / append-only guard ----------
def test_row_has_exactly_the_schema_columns():
    row, _ = run(lambda r: sse(chunk("x"), DONE))
    assert list(row.keys()) == FIELDS


def test_open_results_writes_header_once_and_appends(tmp_path):
    p = tmp_path / "b.csv"
    f, w = open_results(p)
    row, _ = run(lambda r: sse(chunk("x"), DONE))
    w.writerow(row)
    f.close()
    f, w = open_results(p)
    w.writerow(row)
    f.close()
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0].split(",") == FIELDS and len(lines) == 3


def test_open_results_refuses_old_schema_and_leaves_file_untouched(tmp_path):
    old = tmp_path / "baseline.csv"
    old_bytes = b"run_id,seq,first_token_ms\r\nrun-1,0,1562.0\r\n"
    old.write_bytes(old_bytes)
    with pytest.raises(ValueError, match="different header"):
        open_results(old)
    assert old.read_bytes() == old_bytes  # original evidence intact
