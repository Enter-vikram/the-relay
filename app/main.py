"""HTTP service.   python -m uvicorn app.main:app --port 8000

POST /ask    {"question": "...", "priority": "interactive" | "batch"}
GET  /health liveness + whether the library/provider are configured (never exposes keys)
GET  /stats  in-process counters

Provider: GROQ (default, needs GROQ_API_KEY) or the stand-in (RELAY_PROVIDER=stand-in,
RELAY_STANDIN_URL=http://127.0.0.1:8765/openai/v1). Whichever is used is reported in
every response as `source`, so a stand-in answer can never pass as a real one.

HTTP mapping: answered/refused -> 200; rate_limited -> 429 (+Retry-After); timeout -> 504;
other failures -> 502. The service keeps answering (honestly) when the provider is down.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.chunking import SETTINGS
from app.config import ROOT, gateway_settings as G, settings
from app.gateway import Gateway
from app.limiter import Limiter
from app.llm import LLMClient
from app.pipeline import Pipeline, PipelineConfig
from app.queue_manager import BATCH, INTERACTIVE, QueueManager
from app.retrieval import Library, Retriever
from app.retry import RetryBudget, RetryPolicy

log = logging.getLogger("relay.api")
INDEX_ROOT = ROOT / "research" / "index"


class Ask(BaseModel):
    question: str = Field(..., max_length=4000)
    priority: str = "interactive"


def build_gateway(source: str | None = None) -> tuple[Gateway, str]:
    source = source or os.getenv("RELAY_PROVIDER", "groq")
    if source == "stand-in":
        base, key, label, model = os.getenv("RELAY_STANDIN_URL", "http://127.0.0.1:8765/openai/v1"), "standin", "stand-in", "stand-in"
    else:
        base, key, label, model = settings.base_url, settings.require_key(), "real", settings.model
    client = LLMClient(base, key, model, label, reasoning_effort=G.reasoning_effort)
    gw = Gateway(client, Limiter(G.rpm, G.tpm, G.rpd, G.tpd, G.safety_fraction),
                 QueueManager(G.max_concurrency, G.max_queue), RetryPolicy("jitter", max_attempts=3),
                 RetryBudget(), request_timeout_s=G.request_timeout_s)
    return gw, label


def build_pipeline(setting: str = "C") -> tuple[Pipeline, str]:
    s = SETTINGS[setting]
    lib = Library.load(INDEX_ROOT, s.chunking_id)
    pages = None
    summ = ROOT / "research" / "extracted" / "summary.json"
    if summ.is_file():
        pages = {k: v["pages"] for k, v in json.loads(summ.read_text(encoding="utf-8")).items()}
    gw, label = build_gateway()
    cfg = PipelineConfig(s, gen_max_tokens=G.gen_max_tokens, deadline_s=G.deadline_s)
    return Pipeline(Retriever(lib), gw, cfg, pages), label


def create_app(factory=build_pipeline) -> FastAPI:
    app = FastAPI(title="The Relay")
    app.state.pipeline, app.state.source, app.state.error = None, "", ""
    app.state.counts = {"requests": 0, "answered": 0, "refused": 0, "rate_limited": 0, "timeout": 0, "error": 0}

    def get():
        if app.state.pipeline is None and not app.state.error:
            try:
                app.state.pipeline, app.state.source = factory()
            except (FileNotFoundError, SystemExit, ValueError) as e:
                app.state.error = str(e) or type(e).__name__
        return app.state.pipeline

    @app.get("/health")
    async def health():
        p = get()
        return {"ok": p is not None, "source": app.state.source, "error": app.state.error or None,
                "queue_len": p.gw.queue.queue_len if p else None}

    @app.get("/stats")
    async def stats():
        return app.state.counts

    @app.post("/ask")
    async def ask(body: Ask):
        c = app.state.counts
        c["requests"] += 1
        p = get()
        if p is None:
            c["error"] += 1
            return JSONResponse({"status": "error", "message": f"Service is not ready: {app.state.error}"}, 503)
        prio = BATCH if body.priority == "batch" else INTERACTIVE
        r = await p.ask(body.question, priority=prio)
        out = {**r.public(), "source": app.state.source, "request_id": r.request_id}
        if r.status == "ok":
            c["answered" if r.answered else "refused"] += 1
            return out
        c[r.status] = c.get(r.status, 0) + 1
        code = {"rate_limited": 429, "timeout": 504}.get(r.status, 502 if r.reason != "invalid_question" else 400)
        headers = {"Retry-After": "30"} if r.status == "rate_limited" else {}
        return JSONResponse(out, code, headers)

    return app


app = create_app()
