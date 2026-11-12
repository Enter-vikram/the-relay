"""The multi-step answer pipeline.

  1 validate+normalise  2 understand question  3 retrieve (TOOL CALL: search_papers)
  4 BRANCH: evidence sufficient?  no -> expand retrieval once (LOOP #1) -> still no -> refuse
  5 fit context to the token budget   6 generate (model call 1)
  7 citation validation (deterministic)   8 verification (model call 2, separate prompt)
  9 BRANCH/LOOP #2: failed -> one correction attempt (calls 3, 4) -> still failing -> refuse

Model-call budget: max_model_calls (default 4). A refusal decided before any
model call (insufficient evidence, ambiguous, invalid) costs zero tokens.
Answers are NOT streamed to the user before verification, so the user sees text
only at `total_ms`; `first_token_ms` reports the generation call's first content
token (measured from request start) for latency analysis.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from app import verifier as V
from app.chunking import RetrievalSetting
from app.gateway import Gateway
from app.question import InvalidQuestion, classify
from app.queue_manager import INTERACTIVE
from app.retrieval import Hit, Retriever, SufficiencyConfig, fit_to_budget
from app.tokens import DEFAULT_EXPECTED_OUT, estimate_tokens


@dataclass
class PipelineConfig:
    setting: RetrievalSetting
    mode: str = "bm25"                    # or "hybrid" (needs embeddings)
    llm_verify: bool = True
    max_model_calls: int = 4
    max_context_tokens: int = 3500        # est. tokens of passage text
    # Temporary token-bucket depletion is handled by PACING (the gateway waits), never by refusing. The pipeline
    # only shrinks context when the wait for a full-size request would eat more than this fraction of the
    # remaining deadline, and never below min_context_tokens (below that it waits, or the gateway sheds).
    budget_wait_fraction: float = 0.5
    max_budget_wait_s: float | None = None   # optional absolute override of the above (seconds)
    min_context_tokens: int = 600
    gen_max_tokens: int = 900
    verify_max_tokens: int = 500
    deadline_s: float = 30.0
    support_threshold: float = 0.5        # lexical support; uncalibrated placeholder
    sufficiency: SufficiencyConfig = field(default_factory=SufficiencyConfig)
    comparison_per_paper: int = 2


MIN_USEFUL_CONTEXT = 200


@dataclass
class BudgetPlan:
    cap: int            # context tokens to send
    fits_ever: bool     # False: cannot fit the per-minute CAPACITY even with minimal context (permanent)
    wait_s: float       # pacing wait the gateway is expected to impose for this request
    shrunk: bool        # context reduced because waiting for the full size would use too much of the deadline


@dataclass
class ToolCall:
    name: str
    args: dict
    ms: float
    ok: bool
    error: str = ""


@dataclass
class PipelineResult:
    request_id: str
    status: str                       # ok | rate_limited | timeout | error   (ok includes pipeline refusals)
    answered: bool = False
    refused: bool = False
    reason: str = ""                  # refusal / failure reason
    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    message: str = ""                 # what the person sees
    question_kind: str = ""
    retrieved: list[dict] = field(default_factory=list)   # chunk ids + scores shown to the model
    first_token_ms: float | None = None
    total_ms: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None
    model_calls: int = 0
    retries: int = 0
    stage_ms: dict = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    sufficiency: dict = field(default_factory=dict)
    verification: str = ""            # passed | failed | skipped
    context_trimmed: bool = False
    est_tokens_in: int = 0
    budget: dict = field(default_factory=dict)       # context budget decision (cap, expected pacing wait, shrunk)
    first_pass: list = field(default_factory=list)   # raw first-pass hits, before the sufficiency gate (for benchmarks)

    def public(self) -> dict:
        return {"status": self.status, "answered": self.answered, "refused": self.refused,
                "reason": self.reason, "answer": self.answer, "citations": self.citations,
                "message": self.message, "question_kind": self.question_kind,
                "budget": self.budget, "timings_ms": {"total": self.total_ms, "first_token": self.first_token_ms, **self.stage_ms}}


class ToolRegistry:
    """Named tools with timing and error capture. Only local, deterministic tools exist today."""

    def __init__(self, tools: dict, timeout_s: float = 10.0):
        self.tools, self.timeout_s, self.calls = tools, timeout_s, []

    async def call(self, name: str, **kw):
        t = time.monotonic()
        try:
            out = await asyncio.wait_for(asyncio.to_thread(self.tools[name], **kw), self.timeout_s)
            self.calls.append(ToolCall(name, {k: v for k, v in kw.items() if k != "exclude"},
                                       round((time.monotonic() - t) * 1000, 2), True))
            return out
        except Exception as e:
            self.calls.append(ToolCall(name, kw, round((time.monotonic() - t) * 1000, 2), False, repr(e)))
            raise


class Pipeline:
    def __init__(self, retriever: Retriever, gateway: Gateway, cfg: PipelineConfig,
                 page_counts: dict[str, int] | None = None):
        self.retriever, self.gw, self.cfg, self.page_counts = retriever, gateway, cfg, page_counts

    # -------------------------------------------------------------- helpers
    def _context_budget(self, question_chars: int, remaining_s: float | None = None) -> "BudgetPlan":
        """Decide how much passage text to send. Distinguishes three situations:
        1. PERMANENT: even the minimum context cannot fit the per-minute CAPACITY -> fits_ever=False.
        2. TEMPORARY depletion: the bucket is low now but refills -> keep the full context and let the gateway
           pace, as long as the wait fits inside budget_wait_fraction of the remaining deadline.
        3. Wait too long for a full-size request -> shrink context to what the bucket will hold after the
           allowed wait (not what it holds right now), never below min_context_tokens.
        Daily budget exhaustion is the gateway's job (admission 'daily_budget')."""
        lim, cfg = self.gw.limiter, self.cfg
        remaining = cfg.deadline_s if remaining_s is None else remaining_s
        overhead = estimate_tokens(len(V.GEN_SYSTEM) + question_chars + 400)
        capacity_room = int(lim.tpm_capacity) - DEFAULT_EXPECTED_OUT - overhead
        if capacity_room < MIN_USEFUL_CONTEXT:
            return BudgetPlan(0, False, 0.0, False)
        cap = min(cfg.max_context_tokens, capacity_room)
        allowed = cfg.max_budget_wait_s if cfg.max_budget_wait_s is not None else cfg.budget_wait_fraction * remaining
        need = overhead + cap + DEFAULT_EXPECTED_OUT
        wait = lim.wait_needed(need)
        shrunk = False
        if wait > allowed:
            lim.tpm.refill(lim.clock())
            avail = lim.tpm.level + lim.tpm.per_second * allowed
            floor = min(cfg.min_context_tokens, cap)
            shrunk_cap = max(min(cap, int(avail - DEFAULT_EXPECTED_OUT - overhead)), floor)
            shrunk, cap = shrunk_cap < cap, shrunk_cap
            wait = lim.wait_needed(overhead + cap + DEFAULT_EXPECTED_OUT)
        return BudgetPlan(cap, True, wait, shrunk)

    def _search(self, q, k, rerank, per_paper, exclude=frozenset()):
        return self.retriever.search(q, k, rerank=rerank, mode=self.cfg.mode,
                                     per_paper=per_paper, exclude=exclude)

    def _finish(self, r: PipelineResult, t0: float) -> PipelineResult:
        r.total_ms = round((time.monotonic() - t0) * 1000, 1)
        return r

    async def _retrieve(self, info, tools: "ToolRegistry", stage: dict, r: "PipelineResult",
                        remaining_s: float | None = None):
        """Steps 3-5. Returns the hits to show the model, or a PipelineResult stub describing a refusal
        (reason/message set, refused=True). Shared with the retrieval benchmark (no model calls here)."""
        cfg, s = self.cfg, self.cfg.setting
        per_paper = cfg.comparison_per_paper if info.kind == "comparison" else None
        t = time.monotonic()
        hits: list[Hit] = await tools.call("search_papers", q=info.text, k=s.top_k, rerank=s.rerank, per_paper=per_paper)
        stage["retrieval_ms"] += (time.monotonic() - t) * 1000
        r.first_pass = list(hits)
        # BRANCH: sufficiency; LOOP #1: one expansion that skips already-seen evidence
        suff = self.retriever.assess(info.text, hits, cfg.sufficiency)
        if not suff.sufficient:
            seen = frozenset(h.chunk_id for h in hits)
            t = time.monotonic()
            more = await tools.call("search_papers", q=info.text, k=s.top_k + 3, rerank=s.rerank,
                                    per_paper=per_paper or 1, exclude=seen)
            stage["retrieval_ms"] += (time.monotonic() - t) * 1000
            merged = hits + [h for h in more if h.chunk_id not in seen]
            suff = self.retriever.assess(info.text, merged, cfg.sufficiency)
            r.sufficiency = {**vars(suff), "expanded": True}
            if not suff.sufficient:
                return PipelineResult(r.request_id, "ok", refused=True, reason="insufficient_evidence",
                                      message=V.REFUSAL_TEXT)
            hits = merged
        else:
            r.sufficiency = {**vars(suff), "expanded": False}
        plan = self._context_budget(len(info.text), remaining_s)
        r.budget = {"context_cap": plan.cap, "expected_pacing_wait_s": round(plan.wait_s, 2), "shrunk_for_wait": plan.shrunk}
        if not plan.fits_ever:                          # PERMANENT: exceeds per-minute capacity, waiting cannot help
            return PipelineResult(r.request_id, "ok", refused=True, reason="too_large",
                                  message="This question cannot be answered within the model's per-minute token capacity.")
        hits, r.context_trimmed = fit_to_budget(hits, plan.cap)
        r.context_trimmed = r.context_trimmed or plan.shrunk
        return hits

    async def retrieve_only(self, question: str):
        """Retrieval stage only (for benchmarks). Returns (QuestionInfo, hits or None, PipelineResult-with-details)."""
        r = PipelineResult("bench", "ok")
        r.stage_ms = {"retrieval_ms": 0.0}
        info = classify(question)
        r.question_kind = info.kind
        if info.kind == "ambiguous":
            r.refused, r.reason = True, "ambiguous"
            return info, None, r
        tools = ToolRegistry({"search_papers": self._search})
        out = await self._retrieve(info, tools, r.stage_ms, r)
        r.tool_calls = tools.calls
        if isinstance(out, PipelineResult):
            r.refused, r.reason = True, out.reason
            return info, None, r
        return info, out, r

    # -------------------------------------------------------------- main
    async def ask(self, question: str, request_id: str | None = None, priority: int = INTERACTIVE) -> PipelineResult:
        cfg, t0 = self.cfg, time.monotonic()
        rid = request_id or uuid.uuid4().hex[:12]
        r = PipelineResult(rid, "ok")
        tools = ToolRegistry({"search_papers": self._search})
        stage = {"retrieval_ms": 0.0, "generation_ms": 0.0, "verification_ms": 0.0,
                 "queue_ms": 0.0, "limiter_ms": 0.0, "retry_delay_ms": 0.0}
        r.stage_ms = stage

        def refuse(reason: str, msg: str = V.REFUSAL_TEXT) -> PipelineResult:
            r.refused, r.reason, r.message, r.answer = True, reason, msg, ""
            r.tool_calls = tools.calls
            return self._finish(r, t0)

        def fail(gres, why: str) -> PipelineResult:
            r.status, r.reason, r.message = gres.status, gres.final_reason, gres.user_message
            r.verification = r.verification or "skipped"
            r.tool_calls = tools.calls
            return self._finish(r, t0)

        # 1-2 validate + understand
        try:
            info = classify(question)
        except InvalidQuestion as e:
            r.status, r.reason, r.message = "error", "invalid_question", str(e)
            return self._finish(r, t0)
        r.question_kind = info.kind
        if info.kind == "ambiguous":
            return refuse("ambiguous", "Your question is too short or vague to search the papers. "
                                       "Please name the topic or paper you mean.")

        # 3-5 retrieve (tool call) -> sufficiency branch (+ expansion loop) -> context budget
        out = await self._retrieve(info, tools, stage, r, remaining_s=cfg.deadline_s - (time.monotonic() - t0))
        if isinstance(out, PipelineResult):
            return refuse(out.reason, out.message) if out.refused else out
        hits = out
        passages = V.passages_from_hits(hits)
        r.retrieved = [{"chunk_id": h.chunk_id, "score": h.score, "method": h.method, "paper": h.chunk["paper"],
                        "pages": h.chunk["pages"], "section": h.chunk["section"]} for h in hits]

        tin = tout = 0
        gen_first_ms: float | None = None
        attempt_text, feedback = "", ""

        async def call(kind: str, messages, max_tokens):
            nonlocal tin, tout, gen_first_ms
            r.model_calls += 1
            offset = (time.monotonic() - t0) * 1000
            t_c = time.monotonic()
            g = await self.gw.submit(f"{rid}:{kind}{r.model_calls}", messages, max_tokens, priority,
                                     deadline_s=max(cfg.deadline_s - (time.monotonic() - t0), 0.1))
            key = "generation_ms" if kind == "gen" else "verification_ms"
            stage[key] += (time.monotonic() - t_c) * 1000
            stage["queue_ms"] += g.queue_wait_ms
            stage["limiter_ms"] += g.limiter_wait_ms
            stage["retry_delay_ms"] += g.retry_delay_ms
            r.retries += max(g.attempts - 1, 0)
            r.est_tokens_in += g.est_tokens_in
            if g.ok:
                tin += g.llm.tokens_in or 0
                tout += g.llm.tokens_out or 0
                if kind == "gen" and gen_first_ms is None and g.first_token_ms is not None:
                    gen_first_ms = round(offset + g.first_token_ms, 1)
            return g

        for round_no in (1, 2):                                    # LOOP #2: at most one correction
            if r.model_calls >= cfg.max_model_calls:
                break
            g = await call("gen", V.gen_messages(info.text, passages, feedback, attempt_text), cfg.gen_max_tokens)
            r.tokens_in, r.tokens_out, r.first_token_ms = tin or None, tout or None, gen_first_ms
            if not g.ok:
                return fail(g, "generation failed")
            text = g.text.strip()
            if V.is_refusal(text):
                return refuse("model_declined")
            rep = V.analyze(text, passages, support_threshold=cfg.support_threshold, page_counts=self.page_counts)
            problems = rep.feedback()
            verified = None
            if rep.ok and cfg.llm_verify and r.model_calls < cfg.max_model_calls:
                v = await call("verify", V.verify_messages(text, passages, rep.cited), cfg.verify_max_tokens)
                r.tokens_in, r.tokens_out = tin or None, tout or None
                if not v.ok:
                    return fail(v, "verification call failed")        # never ship unverified text
                verdict = V.parse_verdict(v.text)
                verified = verdict.parsed and verdict.supported
                if not verified:
                    problems = ("verifier could not confirm support" if not verdict.parsed else
                                "unsupported sentences: " + " | ".join(verdict.unsupported))
            elif rep.ok and cfg.llm_verify:
                verified = False                                    # out of call budget: unverified => not shipped
                problems = "no model-call budget left to verify"
            if rep.ok and (verified or not cfg.llm_verify):
                r.verification = "passed" if cfg.llm_verify else "lexical_only"
                cites = V.citations_for(passages, rep.cited)
                r.answered, r.answer, r.citations = True, text, cites
                r.message = f"{text}\n\n{V.render_sources(cites)}"
                r.tool_calls = tools.calls
                return self._finish(r, t0)
            r.verification = "failed"
            attempt_text, feedback = text, problems
        return refuse("verification_failed")
