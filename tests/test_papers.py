"""Download / verification / manifest tests. All HTTP is mocked and all PDFs are
synthetic fixtures: no network, and nothing here proves the real arXiv files
verify - that is what `python -m scripts.verify_papers` is for."""
import random

import httpx
import pytest

from app.papers import (PAPERS_BY_KEY, acquire, load_manifest, make_entry, save_manifest,
                        sha256_file, verify_pdf)
from tests.helpers import make_pdf

PAPER = PAPERS_BY_KEY["attention"]          # arXiv:1706.03762 v7
MIN = 1000                                  # fixtures are tiny; production default is 100 KB
BODY = "Self attention relates different positions of one sequence. " * 25


def pdf_bytes(tmp_path, name="f.pdf", stamp="arXiv:1706.03762v7 [cs.CL] 2 Aug 2023",
              title="Attention Is All You Need") -> bytes:
    p = make_pdf(tmp_path / name, [[("h", "Abstract"), ("p", BODY)], [("p", BODY)]],
                 stamp=stamp, title=title)
    return p.read_bytes()


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def run_acquire(tmp_path, handler, **kw):
    sleeps: list[float] = []
    calls: list[httpx.Request] = []

    def counting(req):
        calls.append(req)
        return handler(req, len(calls))

    with client(counting) as c:
        res = acquire(PAPER, c, tmp_path / "papers", min_bytes=MIN, sleep=sleeps.append,
                      rng=random.Random(0), **kw)
    return res, sleeps, calls


# ---------------- verify_pdf ----------------
def test_verify_accepts_exact_version(tmp_path):
    v = verify_pdf(make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2,
                            stamp="arXiv:1706.03762v7 [cs.CL]", title=PAPER.title), PAPER, MIN)
    assert v.ok, v.summary()
    assert v.info["stamp"] == "arXiv:1706.03762v7" and v.info["pages"] == 2


def test_verify_rejects_other_version_permanently(tmp_path):
    v = verify_pdf(make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2,
                            stamp="arXiv:1706.03762v6 [cs.CL]", title=PAPER.title), PAPER, MIN)
    assert not v.ok and "wrong_version" in v.summary() and not v.retryable


def test_verify_rejects_other_paper(tmp_path):
    v = verify_pdf(make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2,
                            stamp="arXiv:2410.00037v1 [eess.AS]", title=PAPER.title), PAPER, MIN)
    assert not v.ok and "wrong_version" in v.summary()


def test_verify_missing_stamp_is_reported_with_page_snippet(tmp_path):
    v = verify_pdf(make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2, title=PAPER.title),
                   PAPER, MIN)
    assert "stamp_missing" in v.summary() and "page starts" in v.summary()


def test_verify_rejects_wrong_title(tmp_path):
    v = verify_pdf(make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2,
                            stamp="arXiv:1706.03762v7", title="A Completely Different Paper"),
                   PAPER, MIN)
    assert "title_mismatch" in v.summary()


def test_verify_detects_truncation(tmp_path):
    full = pdf_bytes(tmp_path)
    p = tmp_path / "cut.pdf"
    p.write_bytes(full[: len(full) // 2])
    v = verify_pdf(p, PAPER, MIN)
    assert not v.ok and v.retryable


def test_verify_rejects_non_pdf_and_missing(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"<html>" + b"x" * 5000)
    assert not verify_pdf(p, PAPER, MIN).ok
    assert "missing" in verify_pdf(tmp_path / "nope.pdf", PAPER, MIN).summary()


def test_verify_enforces_min_size(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2, stamp="arXiv:1706.03762v7",
                 title=PAPER.title)
    assert "too_small" in verify_pdf(p, PAPER, min_bytes=10**9).summary()


# ---------------- acquire: happy path ----------------
def test_download_success_is_atomic_and_recorded(tmp_path):
    data = pdf_bytes(tmp_path)
    res, sleeps, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(200, content=data))
    dest = tmp_path / "papers" / PAPER.filename
    assert res.status == "downloaded" and res.attempts == 1 and not sleeps
    assert dest.read_bytes() == data
    assert not list((tmp_path / "papers").glob("*.part"))     # no temp left behind
    e = res.entry
    assert e["sha256"] == sha256_file(dest) and e["http_status"] == 200
    assert e["source_url"] == "https://arxiv.org/pdf/1706.03762v7"
    assert e["stamp_on_page1"] == "arXiv:1706.03762v7" and e["downloaded_utc"]
    assert str(calls[0].url) == "https://arxiv.org/pdf/1706.03762v7"


# ---------------- acquire: failures ----------------
def test_wrong_version_from_server_is_rejected_not_kept(tmp_path):
    v6 = pdf_bytes(tmp_path, stamp="arXiv:1706.03762v6 [cs.CL]")
    res, sleeps, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(200, content=v6))
    assert res.status == "failed" and "wrong_version" in res.detail
    assert len(calls) == 1 and not sleeps                      # deterministic: no retry
    assert not (tmp_path / "papers" / PAPER.filename).exists()
    assert not list((tmp_path / "papers").glob("*.part"))


def test_404_is_permanent(tmp_path):
    res, sleeps, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(404))
    assert res.status == "failed" and "404" in res.detail and len(calls) == 1 and not sleeps


def test_5xx_then_success_uses_backoff(tmp_path):
    data = pdf_bytes(tmp_path)

    def h(r, n):
        return httpx.Response(503) if n < 3 else httpx.Response(200, content=data)

    res, sleeps, calls = run_acquire(tmp_path, h, base_delay=2.0)
    assert res.status == "downloaded" and res.attempts == 3 and len(calls) == 3
    assert len(sleeps) == 2
    assert 1.0 <= sleeps[0] <= 2.0 and 2.0 <= sleeps[1] <= 4.0      # exponential, jittered


def test_persistent_failure_is_bounded(tmp_path):
    res, sleeps, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(503), max_attempts=3)
    assert res.status == "failed" and len(calls) == 3 and len(sleeps) == 2
    assert "gave up after 3" in res.detail
    assert not (tmp_path / "papers" / PAPER.filename).exists()


def test_retry_after_is_respected(tmp_path):
    data = pdf_bytes(tmp_path)

    def h(r, n):
        if n == 1:
            return httpx.Response(429, headers={"retry-after": "20"})
        return httpx.Response(200, content=data)

    res, sleeps, _ = run_acquire(tmp_path, h, base_delay=1.0, max_delay=5.0)
    assert res.status == "downloaded" and sleeps[0] >= 20


def test_timeout_is_retried(tmp_path):
    data = pdf_bytes(tmp_path)

    def h(r, n):
        if n == 1:
            raise httpx.ReadTimeout("slow", request=r)
        return httpx.Response(200, content=data)

    res, sleeps, calls = run_acquire(tmp_path, h)
    assert res.status == "downloaded" and len(calls) == 2 and len(sleeps) == 1


def test_truncated_body_detected_and_retried(tmp_path):
    data = pdf_bytes(tmp_path)

    def h(r, n):
        if n == 1:   # promise full length, deliver half
            return httpx.Response(200, content=data[: len(data) // 2],
                                  headers={"content-length": str(len(data))})
        return httpx.Response(200, content=data)

    res, sleeps, calls = run_acquire(tmp_path, h)
    assert res.status == "downloaded" and len(calls) == 2
    assert (tmp_path / "papers" / PAPER.filename).read_bytes() == data


def test_corrupt_download_without_length_header_is_rejected(tmp_path):
    data = pdf_bytes(tmp_path)
    res, _, _ = run_acquire(tmp_path, lambda r, n: httpx.Response(200, content=data[:300]),
                            max_attempts=2)
    assert res.status == "failed"
    assert not (tmp_path / "papers" / PAPER.filename).exists()


# ---------------- acquire: existing files ----------------
def test_existing_valid_file_makes_no_request(tmp_path):
    data = pdf_bytes(tmp_path)
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / PAPER.filename).write_bytes(data)
    res, sleeps, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(500))
    assert res.status == "skipped_valid" and not calls
    assert res.entry["sha256"] == sha256_file(tmp_path / "papers" / PAPER.filename)


def test_existing_corrupt_file_is_reacquired(tmp_path):
    data = pdf_bytes(tmp_path)
    (tmp_path / "papers").mkdir()
    dest = tmp_path / "papers" / PAPER.filename
    dest.write_bytes(data[:500])
    res, _, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(200, content=data))
    assert res.status == "downloaded" and len(calls) == 1 and dest.read_bytes() == data


def test_checksum_mismatch_with_manifest_triggers_reacquire(tmp_path):
    data = pdf_bytes(tmp_path)
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / PAPER.filename).write_bytes(data)
    prior = {"sha256": "0" * 64}
    res, _, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(200, content=data),
                                manifest_entry=prior)
    assert res.status == "downloaded" and len(calls) == 1


def test_force_redownloads_valid_file(tmp_path):
    data = pdf_bytes(tmp_path)
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / PAPER.filename).write_bytes(data)
    res, _, calls = run_acquire(tmp_path, lambda r, n: httpx.Response(200, content=data),
                                force=True)
    assert res.status == "downloaded" and len(calls) == 1


def test_failed_reacquire_keeps_no_bad_file_under_final_name(tmp_path):
    data = pdf_bytes(tmp_path)
    (tmp_path / "papers").mkdir()
    dest = tmp_path / "papers" / PAPER.filename
    dest.write_bytes(data[:500])                                # corrupt existing
    res, _, _ = run_acquire(tmp_path, lambda r, n: httpx.Response(503), max_attempts=2)
    assert res.status == "failed"
    assert not list((tmp_path / "papers").glob("*.part"))


# ---------------- manifest ----------------
def test_manifest_roundtrip_preserves_other_entries(tmp_path):
    p = tmp_path / "research" / "manifest.json"
    v = verify_pdf(make_pdf(tmp_path / "a.pdf", [[("p", BODY)]] * 2,
                            stamp="arXiv:1706.03762v7", title=PAPER.title), PAPER, MIN)
    m = {"papers": {"other": {"sha256": "abc"}}}
    m["papers"]["attention"] = make_entry(PAPER, v, final_url="u", http_status=200,
                                          downloaded_utc="t")
    save_manifest(m, p)
    back = load_manifest(p)
    assert back["papers"]["other"] == {"sha256": "abc"}
    assert back["papers"]["attention"]["sha256"] == v.info["sha256"]
    assert "updated_utc" in back and not list(p.parent.glob("*.part"))


def test_load_manifest_missing_file_is_empty(tmp_path):
    assert load_manifest(tmp_path / "nope.json") == {"papers": {}}


def test_registry_has_exactly_the_three_required_versions():
    got = {(p.arxiv_id, p.version) for p in PAPERS_BY_KEY.values()}
    assert got == {("1706.03762", 7), ("2410.00037", 1), ("2507.07955", 2)}
    assert {p.url for p in PAPERS_BY_KEY.values()} == {
        "https://arxiv.org/pdf/1706.03762v7", "https://arxiv.org/pdf/2410.00037v1",
        "https://arxiv.org/pdf/2507.07955v2"}
