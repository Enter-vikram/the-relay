"""Stand-in model provider (FastAPI). Groq-shaped, NOT Groq.

    python -m standin.server --port 8765 [--config path.json] [--seed 1]

Endpoint: POST /openai/v1/chat/completions (stream and non-stream).
Control:  GET /_standin/stats, GET/POST /_standin/config, POST /_standin/reset.
Every response carries header `x-standin: 1` so a stand-in answer can never be
mistaken for a real one in logs or captures.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from standin.config import StandinConfig
from standin.simulator import Plan, Simulator


def create_app(cfg: StandinConfig | None = None) -> FastAPI:
    cfg = cfg or StandinConfig()
    app = FastAPI(title="stand-in model service")
    app.state.sim = Simulator(cfg)
    app.state.sem = asyncio.Semaphore(cfg.max_inflight)

    def _reset(new_cfg: StandinConfig) -> None:
        app.state.sim = Simulator(new_cfg)
        app.state.sem = asyncio.Semaphore(new_cfg.max_inflight)

    def _chunk(delta: dict, finish=None, extra=None) -> bytes:
        evt = {"id": "standin", "object": "chat.completion.chunk", "model": "stand-in",
               "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if extra:
            evt.update(extra)
        return f"data: {json.dumps(evt)}\n\n".encode()

    async def _sleep_until(t0: float, t_ms: float) -> None:
        d = t0 + t_ms / 1000.0 - time.monotonic()
        if d > 0:
            await asyncio.sleep(d)

    async def _stream(plan: Plan, prompt_tokens: int, include_usage: bool):
        t0 = time.monotonic()
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": plan.tokens_out,
                 "total_tokens": prompt_tokens + plan.tokens_out}
        # reasoning chunks: first at ttfr, rest spread until content starts
        n_r = min(6, plan.reasoning_tokens)
        for i in range(n_r):
            t = plan.ttfr_ms + (plan.content_start_ms - plan.ttfr_ms) * i / max(n_r, 1)
            if plan.interrupt_at_ms is not None and t > plan.interrupt_at_ms:
                return
            await _sleep_until(t0, t)
            yield _chunk({"reasoning": "thinking " * max(plan.reasoning_tokens // n_r, 1)})
        n_c = min(5, plan.content_tokens)
        for i in range(n_c):
            t = plan.content_start_ms + (plan.content_end_ms - plan.content_start_ms) * i / max(n_c - 1, 1)
            if plan.interrupt_at_ms is not None and t > plan.interrupt_at_ms:
                return
            await _sleep_until(t0, t)
            yield _chunk({"content": "[stand-in] " + "word " * max(plan.content_tokens // n_c, 1)})
        await _sleep_until(t0, plan.content_end_ms)
        yield _chunk({}, finish=plan.finish_reason, extra={"x_groq": {"usage": usage}})
        if include_usage:
            yield f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    @app.post("/openai/v1/chat/completions")
    async def chat(request: Request):
        sim: Simulator = app.state.sim
        c = sim.cfg
        body = await request.json()
        msgs = body.get("messages") or []
        chars = sum(len(str(m.get("content", ""))) for m in msgs)
        ptoks = sim.prompt_tokens(chars)
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 4096)
        stream = bool(body.get("stream"))
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        base_headers = {"x-standin": "1"}

        d = sim.admit(ptoks, max_tokens)
        hdrs = {**base_headers, **sim.rate_headers()}
        if d.kind == "hang":
            await asyncio.sleep(c.hang_s)
            return JSONResponse({"error": {"message": "stand-in hang elapsed"}}, 504, hdrs)
        if d.kind != "accept":
            if d.retry_after_s is not None:
                hdrs["retry-after"] = str(max(1, math.ceil(d.retry_after_s))) if d.status == 429 \
                    else str(max(1, math.ceil(d.retry_after_s)))
            return JSONResponse({"error": {"message": d.message, "type": d.err_type,
                                           "code": "rate_limit_exceeded" if d.status == 429 else d.err_type}},
                                d.status, hdrs)

        sim.provider_waiting += 1
        sim.stats.peak_provider_waiting = max(sim.stats.peak_provider_waiting, sim.provider_waiting)
        await app.state.sem.acquire()
        sim.provider_waiting -= 1
        sim.inflight += 1
        sim.stats.peak_inflight = max(sim.stats.peak_inflight, sim.inflight)
        plan = sim.plan(max_tokens, sim.inflight)

        def finish():
            sim.charge_completion(plan.tokens_out)
            sim.inflight -= 1
            app.state.sem.release()

        if stream:
            async def gen():
                try:
                    async for part in _stream(plan, ptoks, include_usage):
                        yield part
                finally:
                    finish()
            return StreamingResponse(gen(), media_type="text/event-stream", headers=hdrs)
        try:
            await asyncio.sleep(plan.content_end_ms / 1000.0)
        finally:
            finish()
        usage = {"prompt_tokens": ptoks, "completion_tokens": plan.tokens_out,
                 "total_tokens": ptoks + plan.tokens_out}
        return JSONResponse({"object": "chat.completion", "model": "stand-in",
                             "choices": [{"index": 0, "finish_reason": plan.finish_reason,
                                          "message": {"role": "assistant",
                                                      "content": "[stand-in] " + "word " * plan.content_tokens}}],
                             "usage": usage}, 200, hdrs)

    @app.get("/_standin/stats")
    async def stats():
        from dataclasses import asdict
        return asdict(app.state.sim.stats)

    @app.get("/_standin/config")
    async def get_config():
        return json.loads(app.state.sim.cfg.to_json())

    @app.post("/_standin/config")
    async def set_config(request: Request):
        patch = await request.json()
        _reset(app.state.sim.cfg.with_(**patch))      # also resets buckets and stats
        return json.loads(app.state.sim.cfg.to_json())

    @app.post("/_standin/reset")
    async def reset():
        _reset(app.state.sim.cfg)
        return {"ok": True}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", type=Path, help="JSON StandinConfig; default = baseline-calibrated")
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()
    cfg = StandinConfig.from_json(a.config.read_text(encoding="utf-8")) if a.config else StandinConfig()
    if a.seed is not None:
        cfg = cfg.with_(seed=a.seed)
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
