"""Retrieval + question understanding + verifier + pipeline, on the SYNTHETIC fixture library.

These tests prove the LOGIC (routing, refusal, citation validation, failure handling). They are
not retrieval-quality or answer-quality results for the real papers.
"""
import asyncio

import numpy as np
import pytest

from app import verifier as V
from app.chunking import RetrievalSetting
from app.embeddings import EmbeddingsUnavailable
from app.gateway import GatewayResult
from app.limiter import Limiter
from app.llm import LLMResult
from app.pipeline import Pipeline, PipelineConfig
from app.queue_manager import QueueManager
from app.question import InvalidQuestion, classify, normalize
from app.retrieval import Library, Retriever, SufficiencyConfig, fit_to_budget
from tests.fixtures import synthetic as S


from tests.helpers_pacing import PacingGateway


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------ chunks / retrieval
def test_fixture_chunks_keep_metadata_and_never_cross_sections():
    for c in S.chunks():
        assert c["paper"] in S.PAPERS and c["pages"] and c["page_start"] <= c["page_end"]
    secs = {(c["paper"], c["section"]) for c in S.chunks()}
    assert len(secs) == 9


def test_bm25_finds_the_right_chunk_with_page_and_section():
    r = S.retriever()
    h = r.search("how many folding heads does the zorblax layer use", 3)
    top = h[0].chunk
    assert top["paper"] == "attention" and top["section"] == "1 Zorblax Layers" and top["page_start"] == 1
    assert [x.rank for x in h] == [1, 2, 3][:len(h)]


def test_per_paper_guarantees_every_paper_appears():
    h = S.retriever().search("results score synthetic", 3, per_paper=1)
    assert {x.chunk["paper"] for x in h} == set(S.PAPERS)


def test_exclude_skips_already_seen_chunks():
    r = S.retriever()
    first = r.search("zorblax", 3)
    again = r.search("zorblax", 3, exclude=frozenset(h.chunk_id for h in first))
    assert not ({h.chunk_id for h in first} & {h.chunk_id for h in again})


def test_dedupe_removes_near_duplicate_chunks():
    base = S.chunks()
    dup = dict(base[0]); dup["chunk_id"] = "dup-1"
    lib = Library.from_chunks(base + [dup])
    ids = [h.chunk_id for h in Retriever(lib).search("zorblax folding heads", 5)]
    assert not ({base[0]["chunk_id"], "dup-1"} <= set(ids))


def test_rerank_is_deterministic_subset_and_prefers_adjacent_terms():
    mk = lambda i, t: {"chunk_id": f"x{i}", "paper": "attention", "title": "T", "arxiv_id": "0", "version": 1,
                       "section": "S", "page_start": 1, "page_end": 1, "pages": [1], "text": t,
                       "n_tokens": 10, "text_sha1": str(i)}
    chunks = [mk(0, "gimbal gimbal gimbal gimbal gimbal folding apart from other things entirely unrelated filler words here"),
              mk(1, "the gimbal folding term is normalised"), mk(2, "unrelated words about plumbus")]
    r = Retriever(Library.from_chunks(chunks))
    a = r.search("gimbal folding", 2, rerank=True)
    b = r.search("gimbal folding", 2, rerank=True)
    assert [h.chunk_id for h in a] == [h.chunk_id for h in b]
    assert a[0].chunk_id == "x1" and a[0].method == "bm25+rerank"


class BagEmbedder:
    """TEST DOUBLE: bag-of-words hashing. Used only to exercise fusion plumbing; never a result."""
    name = "test-double"
    def encode(self, texts):
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().split():
                out[i, hash(w) % 64] += 1
            n = np.linalg.norm(out[i]) or 1
            out[i] /= n
        return out


def test_hybrid_requires_real_embedder_and_vectors_no_silent_fallback():
    with pytest.raises(EmbeddingsUnavailable):
        S.retriever().search("zorblax", 3, mode="hybrid")
    lib = Library.from_chunks(S.chunks())
    emb = BagEmbedder()
    lib.vectors = emb.encode([c["text"] for c in lib.chunks]); lib.embed_model = "test-double"
    hits = Retriever(lib, emb).search("zorblax folding", 3, mode="hybrid")
    assert hits and hits[0].method == "hybrid"
    lib.embed_model = "other"
    with pytest.raises(EmbeddingsUnavailable):
        Retriever(lib, emb).search("zorblax", 3, mode="hybrid")


def test_fit_to_budget_trims_from_bottom_but_keeps_minimum():
    hits = S.retriever().search("zorblax synthetic results", 5)
    kept, trimmed = fit_to_budget(hits, max_context_tokens=1)
    assert trimmed and len(kept) == 2 and kept == hits[:2]
    kept, trimmed = fit_to_budget(hits, max_context_tokens=10**6)
    assert not trimmed and kept == hits


def test_sufficiency_supported_vs_unsupported():
    r = S.retriever()
    ok = r.assess("what latent width does the zorblax layer use", r.search("what latent width does the zorblax layer use", 3))
    bad_q = "what is the pricing of bitcoin mining hardware"
    bad = r.assess(bad_q, r.search(bad_q, 3))
    assert ok.sufficient and not bad.sufficient and bad.reason in ("low_term_coverage", "low_top_score", "no_hits")


# ------------------------------------------------------------ question understanding
@pytest.mark.parametrize("q,kind", [
    ("What latent width does the zorblax layer use?", "direct"),
    ("Compare the zorblax layer with wibble routing", "comparison"),
    ("How does blorpt quantisation work and why is the frame rate low?", "multi"),
    ("What about it?", "ambiguous"),
    ("Explain", "ambiguous"),
])
def test_classification(q, kind):
    assert classify(q).kind == kind


def test_titles_named_trigger_comparison_and_input_is_validated():
    assert classify("In Moshi and in Dynamic Chunking, what is the training setup?").kind == "comparison"
    for bad in ("", "   ", "x" * 1001, None):
        with pytest.raises(InvalidQuestion):
            normalize(bad)
    assert normalize("a\x00b\n c") == "a b c"


# ------------------------------------------------------------ verifier layer 1
def passages():
    hits = S.retriever().search("zorblax folding heads latent width", 3)
    return V.passages_from_hits(hits), hits


def test_analyze_accepts_supported_cited_answer():
    ps, hits = passages()
    n = next(p.n for p in ps if "folding heads" in p.chunk["text"])
    rep = V.analyze(f"The zorblax layer uses 7 folding heads [{n}].", ps)
    assert rep.ok and rep.cited == {n}


@pytest.mark.parametrize("answer,kind", [
    ("The zorblax layer uses folding heads.", "uncited_sentence"),
    ("The zorblax layer uses 7 folding heads [9].", "unknown_marker"),
    ("The zorblax layer uses 12 folding heads [1].", "unsupported_number"),
    ("Bananas are yellow fruits grown in tropical climates [1].", "weak_support"),
    ("", "empty"),
])
def test_analyze_flags_problems(answer, kind):
    ps, _ = passages()
    # make marker [1] point at the folding chunk so only the intended defect is present
    ps = sorted(ps, key=lambda p: "folding heads" not in p.chunk["text"])
    ps = [V.Passage(i + 1, p.chunk) for i, p in enumerate(ps)]
    kinds = {p.kind for p in V.analyze(answer, ps).problems}
    assert kind in kinds


def test_analyze_checks_page_exists_in_pdf():
    ps, _ = passages()
    n = ps[0].n
    rep = V.analyze(f"The zorblax layer uses folding heads [{n}].", ps, page_counts={ps[0].chunk["paper"]: 0})
    assert "bad_page" in {p.kind for p in rep.problems}


def test_refusal_marker_and_verdict_parsing():
    assert V.is_refusal("INSUFFICIENT_EVIDENCE") and V.is_refusal("insufficient evidence")
    assert not V.is_refusal("The layer uses 7 heads [1].")
    assert V.parse_verdict('```json\n{"verdict":"supported","unsupported_sentences":[]}\n```').supported
    v = V.parse_verdict('{"verdict":"unsupported","unsupported_sentences":["x"]}')
    assert v.parsed and not v.supported and v.unsupported == ["x"]
    assert not V.parse_verdict("looks fine to me").parsed
    assert not V.parse_verdict('{"verdict":"maybe"}').parsed
    assert not V.parse_verdict('{"verdict":"supported","unsupported_sentences":["still bad"]}').supported


# ------------------------------------------------------------ pipeline (scripted model double)
class ScriptedGateway:
    """Returns scripted model outputs. TEST DOUBLE: says nothing about real model behaviour."""
    def __init__(self, script, tpm=8000):
        self.script, self.calls, self.limiter = list(script), [], Limiter(1000, tpm, 10**6, 10**9, safety=1.0)
        self.queue = QueueManager(4, 10)

    async def submit(self, request_id, messages, max_tokens, priority=0, deadline_s=30.0):
        self.calls.append((request_id, messages))
        item = self.script.pop(0)
        if isinstance(item, GatewayResult):
            return item
        llm = LLMResult("ok", "ok", 200, item, ttft_ms=50.0, total_ms=60.0, tokens_in=500, tokens_out=40)
        return GatewayResult(True, "ok", "ok", item, llm, attempts=1, first_token_ms=55.0, total_ms=65.0,
                             model_ttft_ms=50.0, model_total_ms=60.0, est_tokens_in=480)


SUP = '{"verdict":"supported","unsupported_sentences":[]}'
UNS = '{"verdict":"unsupported","unsupported_sentences":["The zorblax layer uses 7 folding heads."]}'
Q = "How many folding heads does the zorblax layer use?"


def mkpipe(script, **kw):
    cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 3, False), **kw)
    gw = ScriptedGateway(script)
    return Pipeline(S.retriever(), gw, cfg), gw


def cited_good(pipe_result_hits=None):
    # marker numbers depend on ranking; find the passage containing the fact
    hits = S.retriever().search(Q, 3)
    ps = V.passages_from_hits(hits)
    n = next(p.n for p in ps if "folding heads" in p.chunk["text"])
    return f"The zorblax layer uses 7 folding heads [{n}]."


def test_happy_path_cites_real_metadata_and_counts_tokens_and_calls():
    p, gw = mkpipe([cited_good(), SUP])
    r = run(p.ask(Q, "rq1"))
    assert r.answered and r.status == "ok" and r.verification == "passed" and r.model_calls == 2
    c = r.citations[0]
    assert (c["paper"], c["section"], c["page_start"]) == ("attention", "1 Zorblax Layers", 1)
    assert "Sources:" in r.message and "arXiv:0000.00001v1" in r.message
    assert r.tokens_in == 1000 and r.tokens_out == 80 and r.first_token_ms is not None
    assert [t.name for t in r.tool_calls] == ["search_papers"] and r.tool_calls[0].ok
    assert gw.calls[1][1][0]["content"] == V.VERIFY_SYSTEM            # separate verification prompt


def test_unsupported_question_is_refused_with_zero_model_calls():
    p, gw = mkpipe([])
    r = run(p.ask("what is the pricing of bitcoin mining hardware", "rq2"))
    assert r.refused and r.reason == "insufficient_evidence" and not r.answered and gw.calls == []
    assert "does not establish" in r.message and r.sufficiency["expanded"] is True
    assert sum(t.name == "search_papers" for t in r.tool_calls) == 2   # loop #1: one expansion


def test_model_declining_is_a_refusal_not_an_answer():
    p, _ = mkpipe(["INSUFFICIENT_EVIDENCE"])
    r = run(p.ask(Q, "rq3"))
    assert r.refused and r.reason == "model_declined" and r.answer == ""


def test_uncited_answer_triggers_one_correction_then_succeeds():
    p, gw = mkpipe(["The zorblax layer uses folding heads.", cited_good(), SUP])
    r = run(p.ask(Q, "rq4"))
    assert r.answered and r.model_calls == 3
    assert "problems" in gw.calls[1][1][1]["content"] and "uncited_sentence" in gw.calls[1][1][1]["content"]


def test_hallucinated_number_twice_ends_in_refusal_and_never_leaks_the_text():
    bad = "The zorblax layer uses 12 folding heads [1]."
    p, _ = mkpipe([bad, bad])
    r = run(p.ask(Q, "rq5"))
    assert r.refused and r.reason == "verification_failed" and r.answer == "" and "12" not in r.message


def test_verifier_rejects_then_correction_passes():
    g = cited_good()
    p, gw = mkpipe([g, UNS, g, SUP])
    r = run(p.ask(Q, "rq6"))
    assert r.answered and r.model_calls == 4


def test_unparseable_verifier_output_is_treated_as_unverified():
    g = cited_good()
    p, _ = mkpipe([g, "seems okay", g, "also fine"])
    r = run(p.ask(Q, "rq7"))
    assert r.refused and r.reason == "verification_failed" and r.model_calls == 4


def test_model_call_budget_is_respected():
    g = cited_good()
    p, gw = mkpipe([g], max_model_calls=1)
    r = run(p.ask(Q, "rq8"))
    assert r.refused and len(gw.calls) == 1 and r.reason == "verification_failed"


def test_failed_generation_never_produces_a_fabricated_answer():
    fail = GatewayResult(False, "rate_limited", "rate_limited", user_message="provider is rate limiting", attempts=3)
    p, _ = mkpipe([fail])
    r = run(p.ask(Q, "rq9"))
    assert r.status == "rate_limited" and not r.answered and not r.refused and r.answer == ""
    assert r.message == "provider is rate limiting"


def test_failed_verification_call_does_not_ship_unverified_text():
    fail = GatewayResult(False, "timeout", "timeout", user_message="timed out", attempts=1)
    p, _ = mkpipe([cited_good(), fail])
    r = run(p.ask(Q, "rq10"))
    assert r.status == "timeout" and not r.answered and r.answer == ""


def test_ambiguous_and_invalid_questions_cost_no_model_calls():
    p, gw = mkpipe([])
    a = run(p.ask("What about it?", "rq11"))
    assert a.refused and a.reason == "ambiguous" and gw.calls == []
    b = run(p.ask("", "rq12"))
    assert b.status == "error" and b.reason == "invalid_question" and gw.calls == []


def test_context_is_trimmed_when_token_budget_is_small():
    cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 5, False), max_context_tokens=30)
    gw = ScriptedGateway([cited_good(), SUP])
    r = run(Pipeline(S.retriever(), gw, cfg).ask(Q, "rq13"))
    assert r.context_trimmed and len(r.retrieved) == 2


def test_permanent_capacity_shortfall_is_refused_as_too_large_with_zero_calls():
    # Changed from the old test (which expected "budget_exhausted"): a per-minute CAPACITY too small for even
    # the minimal prompt is PERMANENT, so it gets its own reason; "budget_exhausted" no longer exists as a
    # pipeline refusal because temporary depletion is now paced, not refused (see tests below).
    cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 3, False))
    gw = ScriptedGateway([], tpm=600)                 # capacity too small for system prompt + answer
    r = run(Pipeline(S.retriever(), gw, cfg).ask(Q, "rq14"))
    assert r.refused and r.reason == "too_large" and gw.calls == []


# ---------------- temporary token depletion: pace, never refuse (regression for qa-A-1) ----------------
def _depleting_script(n):
    return [x for _ in range(n) for x in (cited_good(), SUP)]


class OldPolicyPipeline(Pipeline):
    """Frozen copy of the ORIGINAL policy that caused qa-A-1: size the context from the bucket's CURRENT level
    and refuse when less than 200 tokens are left. Kept only to demonstrate the root cause."""
    def _context_budget(self, question_chars, remaining_s=None):
        from app.pipeline import BudgetPlan, estimate_tokens, DEFAULT_EXPECTED_OUT
        lim, cfg = self.gw.limiter, self.cfg
        overhead = estimate_tokens(len(V.GEN_SYSTEM) + question_chars + 400)
        room = int(lim.tpm_capacity - DEFAULT_EXPECTED_OUT - overhead)
        cap = min(cfg.max_context_tokens, room)
        if lim.wait_needed(overhead + cap + DEFAULT_EXPECTED_OUT) > 5.0:       # old max_budget_wait_s
            lim.tpm.refill(lim.clock())
            room = int(lim.tpm.level - DEFAULT_EXPECTED_OUT - overhead)
            cap = min(cap, max(room, 0))
        return BudgetPlan(cap, room >= 200, 0.0, False)


def test_root_cause_old_policy_refused_answerable_questions_once_the_bucket_was_drained():
    gw = PacingGateway(_depleting_script(6))
    pipe = OldPolicyPipeline(S.retriever(), gw, PipelineConfig(RetrievalSetting("T", 60, 10, 3, False), deadline_s=120))
    results = [run(pipe.ask(Q, f"old{i}")) for i in range(6)]
    reached = [r.model_calls > 0 for r in results]
    assert reached[0] and not all(reached)                       # first question fine, later ones never reach the model
    assert any(r.refused and r.reason == "too_large" for r in results)   # "too_large" == the old budget_exhausted exit


def test_temporary_depletion_is_paced_by_the_gateway_not_refused_by_the_pipeline():
    gw = PacingGateway(_depleting_script(6))
    cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 3, False), deadline_s=120)
    pipe = Pipeline(S.retriever(), gw, cfg)
    t0 = gw.clock()
    results = [run(pipe.ask(Q, f"new{i}")) for i in range(6)]
    assert all(r.answered and r.model_calls == 2 for r in results), [(r.reason, r.status) for r in results]
    assert not any(r.budget["shrunk_for_wait"] for r in results)           # full context kept
    assert any(w > 1.0 for w in gw.waits)                                   # it really waited for the refill
    assert gw.min_level > -gw.limiter.tpm_capacity                          # debt stays bounded
    # sustained rate cannot exceed the refill rate: 6 questions x ~5,000 tokens at 120 tokens/s
    assert gw.clock() - t0 >= (6 * 5000 - gw.limiter.tpm_capacity) / gw.limiter.tpm.per_second * 0.9


def test_context_shrinks_only_when_the_wait_exceeds_the_deadline_fraction_and_never_below_the_floor():
    gw = PacingGateway([])
    cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 3, False), deadline_s=10, min_context_tokens=600)
    gw.limiter.reserve(7000)                                                # nearly empty bucket
    plan = Pipeline(S.retriever(), gw, cfg)._context_budget(len(Q), remaining_s=10)
    assert plan.fits_ever and plan.shrunk and cfg.min_context_tokens <= plan.cap < cfg.max_context_tokens
    gw2 = PacingGateway([])                                                  # full bucket: no shrink, no wait
    plan2 = Pipeline(S.retriever(), gw2, cfg)._context_budget(len(Q), remaining_s=10)
    assert not plan2.shrunk and plan2.wait_s == 0 and plan2.cap == cfg.max_context_tokens


def test_wait_longer_than_the_deadline_is_shed_by_the_gateway_as_rate_limited_not_refused():
    gw = PacingGateway([])
    cfg = PipelineConfig(RetrievalSetting("T", 60, 10, 3, False), deadline_s=2)
    gw.limiter.reserve(7100)
    r = run(Pipeline(S.retriever(), gw, cfg).ask(Q, "shed1"))
    assert r.status == "rate_limited" and r.reason == "admission_wait" and not r.refused and not r.answered
