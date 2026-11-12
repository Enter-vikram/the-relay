"""Benchmark dataset validation/grading, 429 probe and analysis tables. SYNTHETIC data only for the first group."""
import asyncio
import csv
import json

import httpx
import pandas as pd
import pytest

from experiments import analyze
from experiments.benchmark_data import (DatasetError, Evidence, Question, grade, load_page_text, load_questions,
                                        norm, retrieval_metrics, validate)
from experiments.probe_429 import big_prompt, run_probe
from experiments.baseline import open_results
from tests.fixtures import synthetic as S

SSE = ('data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
       'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"x_groq":{"usage":{"prompt_tokens":5000,"completion_tokens":2}}}\n\n'
       'data: [DONE]\n\n')


# ---------------- dataset (SYNTHETIC fixture pages) ----------------
@pytest.fixture
def pages(tmp_path):
    for p in S.PAPERS:
        with (tmp_path / f"{p}.units.jsonl").open("w", encoding="utf-8") as f:
            for u in S.units(p):
                f.write(json.dumps(u) + "\n")
    return load_page_text(tmp_path)


def q(i, **kw):
    base = dict(id=f"q{i}", type="direct", question="what?", expect_refusal=False,
                evidence=[Evidence("attention", 1, "applies quasi-widget folding to every token")],
                required_facts=[["7 folding heads"]], min_cited=1)
    base.update(kw)
    return Question(**base)


def test_quote_matching_is_immune_to_hyphenation_spacing_ligatures_and_quote_style():
    from experiments.benchmark_data import loose
    assert loose("self-\nattention") == loose("self-attention") == loose("Self attention")
    assert loose("\ufb01nal \u201cquoted\u201d") == loose('final "quoted"')
    assert norm("a\n  b") == "a b"


def test_validator_accepts_grounded_quote_and_rejects_fabricated_or_misplaced_ones(pages):
    good = q(1)
    bad_text = q(2, evidence=[Evidence("attention", 1, "this sentence does not appear anywhere in the page")])
    bad_page = q(3, evidence=[Evidence("attention", 2, "applies quasi-widget folding to every token")])
    bad_paper = q(4, evidence=[Evidence("moshi", 1, "applies quasi-widget folding to every token")])
    short = q(5, evidence=[Evidence("attention", 1, "quasi-widget folding")])
    errs, _ = validate([good, bad_text, bad_page, bad_paper, short], pages)
    joined = "\n".join(errs)
    assert "q1:" not in joined
    for i in (2, 3, 4):
        assert f"q{i}: quote not found" in joined
    assert "q5: quote shorter" in joined


def test_validator_enforces_structure_and_minimum_size(pages):
    errs, warns = validate([q(1), q(1)], pages)
    assert any("duplicate id" in e for e in errs) and any("at least 15" in e for e in errs)
    refusal_with_evidence = q(2, expect_refusal=True, type="unsupported")
    assert any("must have no evidence" in e for e in validate([refusal_with_evidence], pages)[0])
    assert any("needs expect_refusal" in e.replace("must have expect_refusal=true", "needs expect_refusal")
               for e in validate([q(3, type="unsupported")], pages)[0])
    assert any("covers" in w or "cover" in w for w in warns)


def test_loader_rejects_template_rows_and_missing_file(tmp_path):
    with pytest.raises(DatasetError):
        load_questions(tmp_path / "none.jsonl")
    p = tmp_path / "q.jsonl"
    p.write_text('{"id":"TEMPLATE-001","type":"direct","question":"x","expect_refusal":false}\n')
    with pytest.raises(DatasetError, match="template"):
        load_questions(p)
    p.write_text('# comment\n{"id":"a","type":"direct","question":"x","expect_refusal":true}\n')
    assert load_questions(p)[0].id == "a"


def test_grading_rules():
    Q = q(1, required_facts=[["7 folding heads", "seven folding heads"], ["512"]])
    cite = [{"paper": "attention", "page_start": 1, "page_end": 1}]
    assert grade(Q, answered=True, refused=False, answer="It uses 7 folding heads and width 512.", citations=cite)[0] == "correct"
    g, review, _ = grade(Q, answered=True, refused=False, answer="It uses seven folding heads.", citations=cite)
    assert g == "wrong" and review                       # partial facts -> flagged for manual review
    g, review, _ = grade(Q, answered=True, refused=False, answer="7 folding heads, 512.", citations=[])
    assert g == "wrong" and review                       # right words, no evidence cited
    wrong_page = [{"paper": "attention", "page_start": 2, "page_end": 2}]
    assert grade(Q, answered=True, refused=False, answer="7 folding heads 512", citations=wrong_page)[0] == "wrong"
    assert grade(Q, answered=False, refused=True, answer="", citations=[])[0] == "refused_wrongly"
    R = q(2, expect_refusal=True, type="unsupported", evidence=[], required_facts=[])
    assert grade(R, answered=False, refused=True, answer="", citations=[])[0] == "refused_correctly"
    assert grade(R, answered=True, refused=False, answer="whatever", citations=cite)[0] == "wrong"


def test_retrieval_metrics_use_page_and_quote_not_just_paper():
    hits = S.retriever().search("zorblax folding heads latent width", 3)
    chunks = [h.chunk for h in hits]
    m = retrieval_metrics(chunks, q(1))
    assert m["hit"] and m["rr"] > 0
    miss = retrieval_metrics(chunks, q(2, evidence=[Evidence("moshi", 3, "listeners rated the blorpt dialogue stack at 4.1")]))
    assert not miss["hit"] and miss["rr"] == 0.0


# ---------------- 429 probe (mocked transport, fake sleep) ----------------
def run_probe_with(handler, tmp_path):
    slept = []
    async def fake_sleep(s): slept.append(s)
    async def go():
        f, w = open_results(tmp_path / "probe.csv")
        with f:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await run_probe(c, w, f, sleep=fake_sleep, api_key="SECRETKEY123", base_url="http://t", model="m")
    return asyncio.run(go()), slept


def test_probe_flow_429_then_waits_retry_after_plus_one_then_recovers(tmp_path):
    seq = []
    def handler(req):
        seq.append(1)
        if len(seq) == 2:
            return httpx.Response(429, json={"error": {"message": "tokens per minute (TPM) limit"}},
                                  headers={"retry-after": "21", "x-ratelimit-remaining-requests": "990"})
        return httpx.Response(200, text=SSE, headers={"x-ratelimit-remaining-requests": str(998 - len(seq))})
    out, slept = run_probe_with(handler, tmp_path)
    assert out["result"] == "recovered" and slept == [22.0] and len(seq) == 3        # exactly 3 requests, no auto-retry
    rows = list(csv.DictReader((tmp_path / "probe.csv").open(encoding="utf-8")))
    assert [r["status"] for r in rows] == ["200", "429", "200"] and rows[1]["retry_after"] == "21"
    assert "SECRETKEY123" not in (tmp_path / "probe.csv").read_text(encoding="utf-8")


def test_probe_reports_no_429_and_stops(tmp_path):
    seq = []
    out, slept = run_probe_with(lambda r: (seq.append(1), httpx.Response(200, text=SSE))[1], tmp_path)
    assert out["result"] == "no_429" and len(seq) == 2 and slept == []


def test_probe_aborts_on_unexpected_error_without_more_requests(tmp_path):
    seq = []
    out, _ = run_probe_with(lambda r: (seq.append(1), httpx.Response(401, text="bad key"))[1], tmp_path)
    assert out["result"] == "aborted" and len(seq) == 1


def test_big_prompt_size_and_distinct_salt():
    a, b = big_prompt(20_000, "A"), big_prompt(20_000, "B")
    assert 20_000 <= len(a) < 20_100 and a != b


# ---------------- analysis (tables recomputed from results.csv) ----------------
def make_results(tmp_path, monkeypatch):
    res = tmp_path / "results"; (res / "raw").mkdir(parents=True)
    monkeypatch.setattr(analyze, "RES", res); monkeypatch.setattr(analyze, "RAW", res / "raw")
    from app.metrics import Record, append_records
    recs = []
    for i in range(10):
        ok = i < 7
        recs.append(Record(f"s-{i}", "demo", "stand-in", f"2026-01-01T00:00:{i:02d}.000+00:00",
                           "ok" if ok else ("rate_limited" if i < 9 else "timeout"),
                           first_token_ms=1000.0 + 100 * i if ok else None, total_ms=2000.0 + 100 * i, retries=i % 3,
                           tokens_in=1000 if ok else None, tokens_out=300 if ok else None,
                           queue_len_at_submit=i, attempts=1 + i % 3, queue_wait_ms=50.0, limiter_wait_ms=0.0,
                           retry_delay_ms=0.0, model_ttft_ms=900.0 + 100 * i if ok else None,
                           model_total_ms=1900.0 + 100 * i if ok else None))
    append_records(recs, res / "raw" / "x__demo.results.csv", res / "raw" / "x__demo.diagnostics.csv")
    return res


def test_build_and_tables_are_recomputable_from_results_csv(tmp_path, monkeypatch):
    res = make_results(tmp_path, monkeypatch)
    info = analyze.build_results_csv()
    assert info["rows"] == 10
    df, dg = analyze.load()
    (a,), small = analyze.table_2a(df)
    assert a["requests"] == 10 and a["n_ok"] == 7 and small and a["p99_unstable"]
    assert a["ttft"][0] == "1300"                       # p50 of 1000..1600 step 100
    (b,) = analyze.table_2b(df, dg)
    assert (b["ok"], b["rate_limited"], b["timeout"], b["error"]) == ("70.0%", "20.0%", "10.0%", "0.0%")
    assert b["peak_queue"] == "9" and b["tokens_per_answer"] == "1300"
    # 1000 in x $0.075/1M + 300 out x $0.30/1M = $0.000165 per answer -> $0.17 per 1000 (simulated)
    assert b["cost_per_1000"].startswith("$0.17") and "simulated" in b["cost_per_1000"]
    bd = analyze.breakdown(df, dg, "demo")
    assert bd["total_ms"] == 2600.0 and bd["components_ms"]["queue wait"] == 50.0
    manifest = json.loads((res / "raw_manifest.json").read_text())
    assert len(manifest) == 2                           # results + diagnostics raw files hashed


def test_not_measured_instead_of_zero_when_no_quality_rows(tmp_path, monkeypatch):
    make_results(tmp_path, monkeypatch)
    analyze.build_results_csv()
    df, dg = analyze.load()
    assert analyze.table_3(df) == []
    assert analyze.ms(None) == "not measured" and analyze.pct(0, 0) == "not measured"


def test_baseline_normalisation_maps_outcomes_and_keeps_real_label():
    recs = analyze.normalize_baseline(analyze.RES / "baseline_v2.csv")
    assert len(recs) == 5 and {r.source for r in recs} == {"real"} and {r.status for r in recs} == {"ok"}
    assert all(r.scenario == "real-baseline-v2" and r.first_token_ms and r.tokens_in for r in recs)


def test_report_checker_requires_the_twelve_headings(tmp_path):
    from scripts.check_report import HEADINGS, check, main
    good = "\n".join(f"## {i}. {h}\ntext [TODO]" for i, h in enumerate(HEADINGS, 1))
    assert check(good)["headings_ok"] and check(good)["placeholders"] == 12
    swapped = good.replace("## 4. Where the time goes", "## 4. Something else")
    assert not check(swapped)["headings_ok"]
    p = tmp_path / "r.md"
    p.write_text(swapped, encoding="utf-8")
    assert main(["--file", str(p)]) == 1
    p.write_text(good, encoding="utf-8")
    assert main(["--file", str(p)]) == 0


def test_build_report_fills_markers_and_marks_table3_not_measured():
    from scripts.build_report import drop_retry_rows, fill, sections, t3_block
    assert "not measured" in t3_block({"Table 3: x": "not measured: needs questions"})
    assert t3_block({"Table 3: x": "| a |\n|---|\n| 1 |"}).startswith("| a |")
    t = "| s | x |\n| retry_fixed | 1 |\n| steady | 2 |"
    assert "retry_fixed" not in drop_retry_rows(t) and "steady" in drop_retry_rows(t)
    out = fill("a <!-- BEGIN:X --><!-- END:X --> b", {"X": "T"})
    assert "<!-- BEGIN:X -->\nT\n<!-- END:X -->" in out
    assert fill(out, {"X": "T"}) == out            # idempotent
    assert sections("## A\nbody\n## B\nmore") == {"A": "body", "B": "more"}


def test_retrieval_metrics_are_measured_before_the_gate_not_after_it():
    """Regression: a question refused by the sufficiency gate used to be counted as a retrieval MISS."""
    from app.chunking import SETTINGS
    from app.retrieval import SufficiencyConfig
    from experiments.retrieval_benchmark import evaluate, summarize
    quest = q(1, question="applies quasi-widget folding to every token")
    strict = SufficiencyConfig(min_coverage=0.999, min_top_score=1e9)       # forces the gate to refuse
    rows = asyncio.run(evaluate(SETTINGS["A"], "bm25", [quest], S.retriever().lib, None, strict))
    assert rows[0]["gated_refusal"] is True
    assert rows[0]["hit"] is True and rows[0]["shown_hit"] is False        # found, then withheld by the gate
    s = summarize(rows)
    assert s["hit_at_k_pct"] == 100.0 and s["shown_hit_after_gate_pct"] == 0.0
    assert s["gate_blocks_answerable_pct"] == 100.0


def test_retrieval_table_uses_newest_unsuperseded_run_and_shows_denominators(tmp_path):
    from scripts.build_report import retrieval_table
    cols = "setting,mode,id,type,expect_refusal,gated_refusal,hit,recall,rr,shown_hit,context_tokens_est,retrieval_ms".split(",")
    def rows(hit):
        return [["S", "bm25", "a", "direct", False, False, hit, 1.0 if hit else 0.0, 1.0 if hit else 0.0, hit, 100, 1.0],
                ["S", "bm25", "b", "direct", False, True, hit, 1.0, 0.5, False, 100, 1.0],
                ["S", "bm25", "u", "unsupported", True, True, "", "", "", "", 0, 1.0]]
    for name, hit in (("rb-1", False), ("rb-2", True)):
        pd.DataFrame(rows(hit), columns=cols).to_csv(tmp_path / f"{name}.rows.csv", index=False)
    assert "2/2 (100.0%)" in retrieval_table(tmp_path)                  # rb-2 wins (newest)
    (tmp_path / "SUPERSEDED.json").write_text('{"rb-2.rows": "x"}')
    assert "0/2 (0.0%)" in retrieval_table(tmp_path)                    # superseded run skipped -> rb-1
    assert "1/1 (100.0%)" in retrieval_table(tmp_path)                  # gate blocks 1/1 unsupported
    assert retrieval_table(tmp_path / "nothing") == "retrieval-only benchmark: not measured"
