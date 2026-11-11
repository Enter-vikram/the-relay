"""Extraction, chunking and BM25 tests on SYNTHETIC PDFs (not the real papers)."""
import re

import pytest

from app.bm25 import BM25Index, chunks_digest, tokenize
from app.chunking import SETTINGS, SENT_RE, chunk_units, count_tokens
from app.extract import extract_units, outline
from tests.helpers import make_pdf

META = {"key": "t", "title": "T", "arxiv_id": "0000.00000", "version": 1}


def sent(n: int, tag: str) -> str:
    return " ".join(f"Sentence {tag}{i} talks about topic {tag} with several extra words here."
                    for i in range(n))


@pytest.fixture
def units(tmp_path):
    pages = [
        [("h", "Abstract"), ("p", sent(3, "A")), ("h", "1 Introduction"), ("p", sent(4, "I"))],
        [("p", sent(4, "J")), ("h", "2.1 Scaled Dot-Product Attention"), ("p", sent(5, "S")),
         ("c", "Figure 1: The attention diagram.")],
        [("p", sent(5, "T")), ("h", "References"), ("p", "[1] Someone. A paper. 2017.")],
    ]
    pdf = make_pdf(tmp_path / "t.pdf", pages, stamp="arXiv:0000.00000v1 [cs.CL] 1 Jan 2020",
                   title="Synthetic Title Of The Paper")
    u, meta = extract_units(pdf, "t")
    return u, meta


# ---------------- extraction ----------------
def test_headings_detected_and_front_matter_title_is_not_a_heading(units):
    u, meta = units
    heads = [t for _, t in outline(u)]
    assert heads == ["Abstract", "1 Introduction", "2.1 Scaled Dot-Product Attention",
                     "References"]
    assert u[0]["kind"] == "para" and u[0]["section"] == "Front matter"
    assert meta["pages"] == 3 and meta["headings"] == 4


def test_pages_are_physical_and_sections_follow_headings(units):
    u, _ = units
    by_sec = {}
    for x in u:
        by_sec.setdefault(x["section"], set()).add(x["page"])
    assert by_sec["1 Introduction"] == {1, 2}            # section continues onto page 2
    assert by_sec["2.1 Scaled Dot-Product Attention"] == {2, 3}
    assert by_sec["References"] == {3}
    assert all(1 <= x["page"] <= 3 for x in u)


def test_rotated_margin_stamp_is_not_body_text(units):
    u, _ = units
    assert not any("arXiv:0000" in x["text"] for x in u)


def test_caption_kind(units):
    u, _ = units
    caps = [x for x in u if x["kind"] == "caption"]
    assert len(caps) == 1 and caps[0]["text"].startswith("Figure 1")


def test_extraction_is_deterministic(tmp_path):
    pdf = make_pdf(tmp_path / "t.pdf", [[("h", "1 Introduction"), ("p", sent(3, "Q"))]])
    assert extract_units(pdf, "t") == extract_units(pdf, "t")


# ---------------- chunking ----------------
def chunks_for(units, size, overlap):
    return chunk_units(units, META, size, overlap, f"c{size}o{overlap}")


def test_chunks_respect_size_and_never_cross_sections(units):
    u, _ = units
    cs = chunks_for(u, 60, 15)
    assert cs and all(c["n_tokens"] <= 60 for c in cs)
    section_of_text = {}
    for x in u:
        if x["kind"] != "heading":
            section_of_text.setdefault(x["section"], []).append(x["text"])
    for c in cs:
        # every chunk's text must come from its own section's units only
        src = " ".join(section_of_text[c["section"]])
        assert all(s in src for s in SENT_RE.split(c["text"])[:1])
        for other, texts in section_of_text.items():
            if other != c["section"]:
                assert c["text"] not in " ".join(texts)


def test_every_sentence_is_covered_by_some_chunk(units):
    u, _ = units
    cs = chunks_for(u, 60, 15)
    blob = " ".join(c["text"] for c in cs)
    for x in u:
        if x["kind"] == "heading":
            continue
        for s in SENT_RE.split(x["text"]):
            assert s.strip() in blob


def test_chunk_text_is_only_source_text_no_invention(units):
    u, _ = units
    cs = chunks_for(u, 80, 20)
    src = re.sub(r"\s+", " ", " ".join(x["text"] for x in u))
    for c in cs:
        for s in SENT_RE.split(c["text"]):
            assert s.strip() in src


def test_page_metadata_is_correct_and_spans_pages(units):
    u, _ = units
    big = chunks_for(u, 600, 100)
    intro = next(c for c in big if c["section"] == "1 Introduction")
    assert intro["pages"] == [1, 2] and intro["page_start"] == 1 and intro["page_end"] == 2
    refs = next(c for c in big if c["section"] == "References")
    assert refs["pages"] == [3]
    assert all(c["pages"] == sorted(set(c["pages"])) for c in big)


def test_overlap_shares_sentences_and_zero_overlap_does_not(units):
    u, _ = units
    with_ov = [c for c in chunks_for(u, 50, 25) if c["section"] == "2.1 Scaled Dot-Product Attention"]
    no_ov = [c for c in chunks_for(u, 50, 0) if c["section"] == "2.1 Scaled Dot-Product Attention"]
    assert len(with_ov) >= 2 and len(no_ov) >= 2

    def sents(c):
        return {s.strip() for s in SENT_RE.split(c["text"])}

    assert any(sents(a) & sents(b) for a, b in zip(with_ov, with_ov[1:]))
    assert all(not (sents(a) & sents(b)) for a, b in zip(no_ov, no_ov[1:]))
    assert len(with_ov) >= len(no_ov)


def test_overlap_never_stalls_progress(units):
    u, _ = units
    cs = chunks_for(u, 40, 39)       # nearly-full overlap must still terminate
    assert len(cs) < 500


def test_single_huge_sentence_is_hard_split():
    big = " ".join(f"w{i}" for i in range(500)) + "."
    units = [{"paper": "t", "page": 1, "section": "S", "kind": "para", "text": big}]
    cs = chunk_units(units, META, 100, 0, "c100o0")
    assert len(cs) >= 5 and all(c["n_tokens"] <= 100 for c in cs)
    assert " ".join(c["text"] for c in cs).replace(" ", "") == big.replace(" ", "")


def test_chunk_ids_unique_stable_and_ordered(units):
    u, _ = units
    a, b = chunks_for(u, 60, 15), chunks_for(u, 60, 15)
    assert a == b
    ids = [c["chunk_id"] for c in a]
    assert len(set(ids)) == len(ids) and ids == sorted(ids)
    assert ids[0] == "t-c60o15-0000"


def test_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ValueError):
        chunk_units([], META, 100, 100, "x")


def test_settings_match_the_brief_and_share_chunking():
    assert (SETTINGS["A"].chunk_tokens, SETTINGS["A"].top_k, SETTINGS["A"].rerank) == (300, 3, False)
    assert (SETTINGS["B"].chunk_tokens, SETTINGS["B"].top_k, SETTINGS["B"].rerank) == (600, 5, False)
    assert (SETTINGS["C"].chunk_tokens, SETTINGS["C"].top_k, SETTINGS["C"].rerank) == (600, 5, True)
    assert SETTINGS["B"].chunking_id == SETTINGS["C"].chunking_id != SETTINGS["A"].chunking_id
    assert count_tokens("a b, c") == 4


# ---------------- BM25 ----------------
def mk(cid, section, text):
    return {"chunk_id": cid, "section": section, "text": text, "text_sha1": cid}


CORPUS = [
    mk("d0", "Methods", "multi head attention runs several attention functions in parallel"),
    mk("d1", "Audio", "the codec compresses speech into discrete audio tokens at low frame rate"),
    mk("d2", "Routing Module", "the chunk boundary is predicted from cosine similarity of neighbours"),
    mk("d3", "Misc", "unrelated filler words about nothing in particular at all"),
]


def test_bm25_ranks_relevant_document_first():
    idx = BM25Index.build(CORPUS)
    assert idx.search("speech audio codec", 3)[0][0] == "d1"
    assert idx.search("parallel attention heads", 3)[0][0] == "d0"


def test_bm25_uses_section_title_text():
    idx = BM25Index.build(CORPUS)
    assert idx.search("routing module", 2)[0][0] == "d2"      # term only in the section name


def test_bm25_no_match_and_stopword_only_queries_return_nothing():
    idx = BM25Index.build(CORPUS)
    assert idx.search("zebra quantum", 3) == []
    assert idx.search("the of and", 3) == []


def test_bm25_deterministic_ties_and_k_limit():
    docs = [mk(f"x{i}", "S", "same words here") for i in range(4)]
    idx = BM25Index.build(docs)
    assert [d for d, _ in idx.search("same words", 10)] == ["x0", "x1", "x2", "x3"]
    assert len(idx.search("same words", 2)) == 2


def test_bm25_save_load_roundtrip(tmp_path):
    idx = BM25Index.build(CORPUS)
    idx.save(tmp_path / "i.json")
    back = BM25Index.load(tmp_path / "i.json")
    assert back.search("speech codec", 3) == idx.search("speech codec", 3)
    assert back.chunks_sha256 == idx.chunks_sha256 == chunks_digest(CORPUS)


def test_chunk_digest_detects_stale_index():
    changed = [dict(c) for c in CORPUS]
    changed[1]["text_sha1"] = "different"
    assert chunks_digest(changed) != chunks_digest(CORPUS)


def test_tokenize_plural_strip_and_stopwords():
    assert tokenize("The attention heads of models") == ["attention", "head", "model"]
    assert tokenize("class bias") == ["class", "bia"]          # documented crude stemmer
