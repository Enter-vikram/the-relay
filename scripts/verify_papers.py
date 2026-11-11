"""Offline integrity check of research/papers/ against research/manifest.json.

    python -m scripts.verify_papers

Checks: PDF structure, exact arXiv id+version stamp on page 1, title, and that
the SHA-256 still equals the manifest. No network access. Exit 0 = all good.
"""
import sys

from app.papers import PAPERS, PAPERS_DIR, load_manifest, verify_pdf


def main() -> int:
    manifest = load_manifest().get("papers", {})
    bad = 0
    print(f"{'paper':<18}{'expected':<12}{'actual':<24}{'pages':>6}  verified")
    for p in PAPERS:
        v = verify_pdf(PAPERS_DIR / p.filename, p)
        entry = manifest.get(p.key)
        problems = [m for _, m in v.problems]
        if entry is None:
            problems.append("not in manifest.json")
        elif v.info.get("sha256") != entry.get("sha256"):
            problems.append("sha256 differs from manifest")
        ok = not problems
        bad += not ok
        print(f"{p.key:<18}{p.stamp[6:]:<12}{v.info.get('stamp', 'not found')[6:]:<24}"
              f"{v.info.get('pages', '-'):>6}  {'YES' if ok else 'NO'}")
        for m in problems:
            print(f"    problem: {m}")
    print("RESULT:", "all papers verified" if not bad else f"{bad} paper(s) FAILED verification")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
