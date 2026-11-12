"""Register PDFs you already have (e.g. saved by hand) in research/manifest.json after full verification.
    python -m scripts.adopt_pdfs <folder-with-the-three-pdfs>
Files are copied to research/papers/<expected name> (an existing different file is never overwritten) and must pass
the same exact-version checks as downloads. The manifest records that they were supplied, not downloaded."""
import shutil
import sys
from pathlib import Path

from app.papers import PAPERS, PAPERS_DIR, load_manifest, make_entry, save_manifest, utcnow, verify_pdf


def main(argv=None) -> int:
    src = Path((argv or sys.argv[1:] or ["."])[0])
    manifest = load_manifest()
    manifest.setdefault("papers", {})
    ok = True
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    for p in PAPERS:
        cand = src / p.filename
        if not cand.is_file():
            cand = next(iter(sorted(src.glob(f"*{p.filename}"))), cand)   # tolerate an upload prefix on the name
        dest = PAPERS_DIR / p.filename
        if not cand.is_file():
            print(f"{p.key}: MISSING in {src}")
            ok = False
            continue
        if dest.exists() and dest.read_bytes() != cand.read_bytes():
            print(f"{p.key}: a DIFFERENT file already exists at {dest}; not overwriting")
            ok = False
            continue
        if not dest.exists():
            shutil.copyfile(cand, dest)
        v = verify_pdf(dest, p)
        print(f"{p.key}: {'verified' if v.ok else 'FAILED: ' + v.summary()} | {v.info}")
        if v.ok:
            manifest["papers"][p.key] = make_entry(p, v, final_url=None, http_status=None, downloaded_utc=None,
                                                    note=f"supplied locally, not downloaded by scripts (adopted {utcnow()})")
        else:
            ok = False
    save_manifest(manifest)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
