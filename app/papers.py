"""The three-paper library: exact-version download, verification, manifest.

Design rules
------------
* Exact versions only. The arXiv version stamp printed on page 1 of each PDF
  ("arXiv:1706.03762v7 ...") must match the requested version, or the file is
  rejected. A different version is NEVER accepted silently.
* Downloads go to "<name>.part", are verified, then atomically renamed, so a
  truncated or wrong file never exists under its final name.
* Retries are bounded: exponential backoff with jitter, honouring Retry-After
  for 429/503. Permanent failures (404, wrong version) are not retried.
* Re-running is safe: a file that verifies and matches the manifest checksum is
  left alone and no network request is made.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
import pymupdf

from app.config import ROOT

log = logging.getLogger("relay.papers")

RESEARCH_DIR = ROOT / "research"
PAPERS_DIR = RESEARCH_DIR / "papers"
MANIFEST_PATH = RESEARCH_DIR / "manifest.json"
MIN_PDF_BYTES = 100_000          # all three real papers are far larger
STAMP_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})\s*v(\d+)", re.IGNORECASE)

# Problem codes. Structural problems may be transient (retry); identity
# problems (wrong version/paper) are deterministic and never retried.
RETRYABLE = {"too_small", "no_pdf_header", "no_eof", "cannot_open", "no_pages",
             "no_text", "corrupt_repaired", "truncated"}
PERMANENT = {"missing", "wrong_version", "stamp_missing", "title_mismatch"}


@dataclass(frozen=True)
class Paper:
    key: str
    title: str
    arxiv_id: str
    version: int
    filename: str

    @property
    def url(self) -> str:
        return f"https://arxiv.org/pdf/{self.arxiv_id}v{self.version}"

    @property
    def stamp(self) -> str:
        return f"arXiv:{self.arxiv_id}v{self.version}"


PAPERS: tuple[Paper, ...] = (
    Paper("attention", "Attention Is All You Need", "1706.03762", 7,
          "attention-is-all-you-need-v7.pdf"),
    Paper("moshi", "Moshi: a speech-text foundation model for real-time dialogue",
          "2410.00037", 1, "moshi-v1.pdf"),
    Paper("dynamic-chunking",
          "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling",
          "2507.07955", 2, "dynamic-chunking-v2.pdf"),
)
PAPERS_BY_KEY = {p.key: p for p in PAPERS}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
@dataclass
class Verification:
    problems: list[tuple[str, str]] = field(default_factory=list)   # (code, message)
    info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def retryable(self) -> bool:
        codes = {c for c, _ in self.problems}
        return bool(codes) and not (codes & PERMANENT)

    def summary(self) -> str:
        return "; ".join(f"{c}: {m}" for c, m in self.problems) or "ok"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def verify_pdf(path: Path, paper: Paper, min_bytes: int = MIN_PDF_BYTES) -> Verification:
    """Check that `path` is an intact PDF of exactly `paper` at its version."""
    v = Verification()
    path = Path(path)
    if not path.is_file():
        v.problems.append(("missing", f"{path} does not exist"))
        return v
    size = path.stat().st_size
    v.info.update(bytes=size, sha256=sha256_file(path))
    with path.open("rb") as f:
        head = f.read(8)
        f.seek(max(0, size - 2048))
        tail = f.read()
    if size < min_bytes:
        v.problems.append(("too_small", f"{size} bytes < {min_bytes}"))
    if not head.startswith(b"%PDF-"):
        v.problems.append(("no_pdf_header", f"starts with {head[:8]!r}"))
    if b"%%EOF" not in tail:
        v.problems.append(("no_eof", "no %%EOF marker near end (truncated?)"))
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001 - any parser failure means unusable
        v.problems.append(("cannot_open", f"{type(e).__name__}: {e}"))
        return v
    with doc:
        v.info["pages"] = doc.page_count
        if doc.page_count < 1:
            v.problems.append(("no_pages", "0 pages"))
            return v
        if getattr(doc, "is_repaired", False):
            v.problems.append(("corrupt_repaired", "parser had to repair the file"))
        try:
            texts = [p.get_text("text") for p in doc]
        except Exception as e:  # noqa: BLE001
            v.problems.append(("cannot_open", f"text extraction failed: {e}"))
            return v
    total_chars = sum(len(t) for t in texts)
    v.info["text_chars"] = total_chars
    if total_chars < 1000:
        v.problems.append(("no_text", f"only {total_chars} extractable characters"))
    first = texts[0]

    m = [(a, int(b)) for a, b in STAMP_RE.findall(first)]
    if not m:
        snippet = re.sub(r"\s+", " ", first)[:150]
        v.problems.append(("stamp_missing",
                           f"no 'arXiv:<id>v<n>' stamp on page 1; page starts: {snippet!r}"))
    else:
        v.info["stamp"] = f"arXiv:{m[0][0]}v{m[0][1]}"
        if (paper.arxiv_id, paper.version) not in m:
            v.problems.append(("wrong_version",
                               f"expected {paper.stamp}, found {v.info['stamp']}"))

    title_words = _words(paper.title)
    page_words = set(_words(first))
    coverage = sum(w in page_words for w in title_words) / len(title_words)
    v.info["title_word_coverage"] = round(coverage, 3)
    if coverage < 0.9:
        v.problems.append(("title_mismatch",
                           f"only {coverage:.0%} of title words found on page 1"))
    return v


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"papers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(manifest: dict, path: Path = MANIFEST_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_utc"] = utcnow()
    manifest["pymupdf_version"] = pymupdf.VersionBind
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def make_entry(paper: Paper, v: Verification, *, final_url: str | None,
               http_status: int | None, downloaded_utc: str | None, note: str = "") -> dict:
    return {
        "key": paper.key, "title": paper.title, "arxiv_id": paper.arxiv_id,
        "version": paper.version, "filename": paper.filename,
        "source_url": paper.url, "final_url": final_url, "http_status": http_status,
        "downloaded_utc": downloaded_utc, "verified_utc": utcnow(),
        "bytes": v.info.get("bytes"), "sha256": v.info.get("sha256"),
        "pages": v.info.get("pages"), "stamp_on_page1": v.info.get("stamp"),
        "title_word_coverage": v.info.get("title_word_coverage"), "note": note,
    }


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class _Permanent(Exception):
    pass


@dataclass
class AcquireResult:
    key: str
    status: str            # downloaded | skipped_valid | failed
    detail: str
    attempts: int = 0
    entry: dict | None = None


def _fetch_to(client: httpx.Client, url: str, tmp: Path) -> tuple[int, str, int]:
    """Stream `url` into `tmp`. Returns (http_status, final_url, bytes)."""
    try:
        with client.stream("GET", url) as r:
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("retry-after")
                raise _Retryable(f"HTTP {r.status_code}",
                                 float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
            if r.status_code != 200:
                raise _Permanent(f"HTTP {r.status_code} for {url}")
            n = 0
            with tmp.open("wb") as f:
                for block in r.iter_bytes():
                    f.write(block)
                    n += len(block)
            expected = r.headers.get("content-length")
            encoded = r.headers.get("content-encoding", "identity").lower() != "identity"
            # Content-Length counts wire bytes; only comparable when not compressed.
            if expected and expected.isdigit() and not encoded and n != int(expected):
                raise _Retryable(f"truncated body: got {n} of {expected} bytes")
            return r.status_code, str(r.url), n
    except httpx.HTTPError as e:
        raise _Retryable(f"{type(e).__name__}: {e}") from e


def acquire(paper: Paper, client: httpx.Client, papers_dir: Path = PAPERS_DIR, *,
            manifest_entry: dict | None = None, force: bool = False,
            max_attempts: int = 4, base_delay: float = 2.0, max_delay: float = 30.0,
            min_bytes: int = MIN_PDF_BYTES,
            sleep: Callable[[float], None] = time.sleep,
            rng: random.Random | None = None) -> AcquireResult:
    """Make sure `papers_dir/paper.filename` is a verified exact-version PDF."""
    rng = rng or random.Random()
    papers_dir = Path(papers_dir)
    papers_dir.mkdir(parents=True, exist_ok=True)
    dest = papers_dir / paper.filename
    tmp = dest.with_name(dest.name + ".part")

    if dest.exists() and not force:
        v = verify_pdf(dest, paper, min_bytes)
        prior_sha = (manifest_entry or {}).get("sha256")
        if v.ok and (prior_sha is None or prior_sha == v.info["sha256"]):
            entry = dict(manifest_entry or {})
            entry = make_entry(paper, v, final_url=entry.get("final_url"),
                               http_status=entry.get("http_status"),
                               downloaded_utc=entry.get("downloaded_utc"),
                               note=entry.get("note") or
                               ("adopted existing verified file" if not manifest_entry else ""))
            log.info("%s: existing file verified, skipping download", paper.key)
            return AcquireResult(paper.key, "skipped_valid", "existing file verified", 0, entry)
        why = v.summary() if not v.ok else "sha256 differs from manifest"
        log.warning("%s: existing file rejected (%s); reacquiring", paper.key, why)

    last = "no attempt made"
    for attempt in range(1, max_attempts + 1):
        retry_after = None
        try:
            status, final_url, _ = _fetch_to(client, paper.url, tmp)
            v = verify_pdf(tmp, paper, min_bytes)
            if v.ok:
                os.replace(tmp, dest)            # atomic: never a partial final file
                entry = make_entry(paper, v, final_url=final_url, http_status=status,
                                   downloaded_utc=utcnow())
                return AcquireResult(paper.key, "downloaded", f"ok after {attempt} attempt(s)",
                                     attempt, entry)
            last = f"verification failed: {v.summary()}"
            if not v.retryable:
                tmp.unlink(missing_ok=True)
                return AcquireResult(paper.key, "failed", last, attempt)
        except _Retryable as e:
            last, retry_after = str(e), e.retry_after
        except _Permanent as e:
            tmp.unlink(missing_ok=True)
            return AcquireResult(paper.key, "failed", str(e), attempt)
        tmp.unlink(missing_ok=True)
        if attempt < max_attempts:
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * rng.uniform(0.5, 1.0)
            if retry_after is not None:
                delay = max(delay, min(retry_after, 60.0))
            log.warning("%s: attempt %d/%d failed (%s); retrying in %.1fs",
                        paper.key, attempt, max_attempts, last, delay)
            sleep(delay)
    return AcquireResult(paper.key, "failed", f"gave up after {max_attempts} attempts: {last}",
                         max_attempts)
