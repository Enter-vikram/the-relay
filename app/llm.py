"""Async client for an OpenAI-compatible chat endpoint (Groq, or the stand-in).

One call = exactly one HTTP request. No SDK, no hidden retries (retries are the
gateway's job, and are logged there). Supports streaming and non-streaming,
measures time to first user-visible content (reasoning excluded) and total time
on a monotonic clock, and classifies every failure into a retry `kind`.

Source guard: the client is constructed with source="real" or "stand-in". The
stand-in stamps `x-standin: 1` on every response; a real client that sees that
header, or a stand-in client that does not, raises SourceMismatch. This is what
stops simulated numbers from ever being labelled as Groq measurements.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from experiments.baseline import StreamParseError, _rate_headers


class SourceMismatch(RuntimeError):
    pass


@dataclass
class LLMResult:
    kind: str                      # ok | rate_limited | daily_exhausted | server | timeout |
    #                                incomplete_stream | empty_response | invalid
    outcome: str                   # baseline-compatible: ok/incomplete_stream/empty_response/timeout/rate_limited/error
    status: int | None = None
    text: str = ""
    ttft_ms: float | None = None            # request start -> first non-empty content
    first_reasoning_ms: float | None = None
    total_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    finish_reason: str = ""
    retry_after_s: float | None = None
    rate_headers: dict = field(default_factory=dict)
    error_body: str = ""
    source: str = ""


_TRY_AGAIN = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)s)?", re.I)
_DAILY = re.compile(r"per day|\(RPD\)|\(TPD\)", re.I)
_MINUTE = re.compile(r"per minute|\(RPM\)|\(TPM\)", re.I)


def _retry_after(headers: httpx.Headers, body: str) -> float | None:
    v = headers.get("retry-after")
    if v:
        try:
            return float(v)
        except ValueError:
            pass
    m = _TRY_AGAIN.search(body or "")
    if m and any(m.groups()):
        h, mi, s = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + s
    return None


def classify_http(status: int, body: str, retry_after: float | None, daily_threshold_s: float = 900.0):
    """Return (kind, outcome) for a non-200 response."""
    if status == 429:
        # The body is authoritative. A huge Retry-After alone is NOT proof of a daily limit: a per-minute limit
        # in deep "debt" (e.g. after a retry storm) also produces one. Only fall back on the length heuristic
        # when the body names no window at all.
        if _DAILY.search(body or ""):
            return "daily_exhausted", "rate_limited"
        if _MINUTE.search(body or ""):
            return "rate_limited", "rate_limited"
        if retry_after is not None and retry_after > daily_threshold_s:
            return "daily_exhausted", "rate_limited"
        return "rate_limited", "rate_limited"
    if status in (408, 504):
        return "timeout", "timeout"
    if status >= 500:
        return "server", "error"
    return "invalid", "error"      # 400/401/403/404/413/422...: retrying cannot help


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, source: str,
                 http: httpx.AsyncClient | None = None, reasoning_effort: str = ""):
        if source not in ("real", "stand-in"):
            raise ValueError("source must be 'real' or 'stand-in'")
        self.base_url, self.api_key, self.model, self.source = base_url.rstrip("/"), api_key, model, source
        self._http = http or httpx.AsyncClient()
        self._own = http is None
        self.reasoning_effort = reasoning_effort

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()

    def _check_source(self, headers: httpx.Headers) -> None:
        marked = headers.get("x-standin") == "1"
        if self.source == "real" and marked:
            raise SourceMismatch("client is labelled 'real' but the server identified as the stand-in")
        if self.source == "stand-in" and not marked:
            raise SourceMismatch("client is labelled 'stand-in' but the server did not send x-standin: 1")

    async def complete(self, messages: list[dict], max_tokens: int, timeout_s: float,
                       stream: bool = True, temperature: float = 0.2) -> LLMResult:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                "temperature": temperature, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        res = LLMResult(kind="error", outcome="error", source=self.source)
        t0 = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                async with self._http.stream(
                        "POST", f"{self.base_url}/chat/completions", json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"}, timeout=timeout_s) as r:
                    self._check_source(r.headers)
                    res.status = r.status_code
                    res.rate_headers = {k: v for k, v in _rate_headers(r.headers).items() if v}
                    if r.status_code != 200:
                        res.error_body = (await r.aread()).decode(errors="replace")[:500]
                        res.retry_after_s = _retry_after(r.headers, res.error_body)
                        res.kind, res.outcome = classify_http(r.status_code, res.error_body, res.retry_after_s)
                    elif stream:
                        await self._consume_stream(r, res, t0)
                    else:
                        await self._consume_json(r, res)
        except SourceMismatch:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            res.kind, res.outcome = "timeout", "timeout"
        except StreamParseError as e:
            res.kind, res.outcome, res.error_body = "server", "error", str(e)[:500]
        except httpx.HTTPError as e:
            res.error_body = f"{type(e).__name__}: {e}"[:500]
            if res.status == 200:
                res.kind, res.outcome = "incomplete_stream", "incomplete_stream"
            else:
                res.kind, res.outcome = "server", "error"
        res.total_ms = round((time.monotonic() - t0) * 1000, 1)
        if res.kind == "ok" and res.ttft_ms is None and not stream:
            res.ttft_ms = res.total_ms          # non-streaming: no separate first-token time
        return res

    async def _consume_json(self, r: httpx.Response, res: LLMResult) -> None:
        data = json.loads((await r.aread()).decode())
        ch = (data.get("choices") or [{}])[0]
        res.text = (ch.get("message") or {}).get("content") or ""
        res.finish_reason = ch.get("finish_reason") or ""
        u = data.get("usage") or {}
        res.tokens_in, res.tokens_out = u.get("prompt_tokens"), u.get("completion_tokens")
        res.kind, res.outcome = ("ok", "ok") if res.text else ("empty_response", "empty_response")

    async def _consume_stream(self, r: httpx.Response, res: LLMResult, t0: float) -> None:
        parts: list[str] = []
        done = False
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError as e:
                raise StreamParseError(f"bad SSE JSON ({e}); payload={payload[:100]!r}") from e
            if isinstance(evt, dict) and "error" in evt:
                raise StreamParseError(f"error event in stream: {json.dumps(evt['error'])[:300]}")
            u = evt.get("usage") or (evt.get("x_groq") or {}).get("usage")
            if u:
                res.tokens_in, res.tokens_out = u.get("prompt_tokens"), u.get("completion_tokens")
            for ch in evt.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("reasoning") and res.first_reasoning_ms is None:
                    res.first_reasoning_ms = round((time.monotonic() - t0) * 1000, 1)
                if d.get("content"):
                    if res.ttft_ms is None:
                        res.ttft_ms = round((time.monotonic() - t0) * 1000, 1)
                    parts.append(d["content"])
                if ch.get("finish_reason"):
                    res.finish_reason = ch["finish_reason"]
        res.text = "".join(parts)
        if not done:
            res.kind, res.outcome = "incomplete_stream", "incomplete_stream"
        elif not res.text:
            res.kind, res.outcome = "empty_response", "empty_response"
        else:
            res.kind, res.outcome = "ok", "ok"
