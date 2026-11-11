"""Download the three exact arXiv versions into research/papers/ and write
research/manifest.json. Safe to re-run: verified files are not re-downloaded.

    python -m scripts.download_papers            # fetch what is missing/invalid
    python -m scripts.download_papers --force    # re-download everything

Exit code 0 only if ALL three papers are present and verified.
"""
import argparse
import logging
import sys
import time

import httpx

from app.papers import MANIFEST_PATH, PAPERS, PAPERS_DIR, acquire, load_manifest, save_manifest

UA = "the-relay-coursework/0.1 (personal assignment; github.com/Enter-vikram/the-relay)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download even if valid")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    manifest = load_manifest()
    manifest.setdefault("papers", {})
    results = []
    timeout = httpx.Timeout(60.0, connect=15.0)
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as c:
        for i, paper in enumerate(PAPERS):
            res = acquire(paper, c, PAPERS_DIR, manifest_entry=manifest["papers"].get(paper.key),
                          force=a.force)
            results.append(res)
            if res.entry:                       # only record what was actually verified
                manifest["papers"][paper.key] = res.entry
                save_manifest(manifest)
            if res.status == "downloaded" and i < len(PAPERS) - 1:
                time.sleep(3)                   # be polite to arxiv.org

    print("\nDOWNLOAD AUDIT")
    print(f"{'paper':<18}{'expected':<12}{'status':<16}{'bytes':>10}  detail")
    ok = True
    total = 0
    for p, r in zip(PAPERS, results):
        e = r.entry or {}
        total += e.get("bytes") or 0
        ok &= r.status != "failed"
        print(f"{p.key:<18}{p.stamp[6:]:<12}{r.status:<16}{e.get('bytes') or '-':>10}  {r.detail}")
    print(f"total verified bytes: {total}")
    if ok:
        print(f"manifest: {MANIFEST_PATH}\nRESULT: all {len(PAPERS)} papers verified")
        return 0
    print("RESULT: FAILED - not every paper was acquired (see detail above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
