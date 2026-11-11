"""Fill the generated tables into report/final_report.md and (optionally) export a PDF.
    python -m scripts.build_report           (fill tables from report/tables.md; run generate_report first)
    python -m scripts.build_report --pdf     (also write report/final_report.pdf; needs pandoc + playwright chromium)
Tables are copied from generated output, never typed by hand."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

from app.chunking import SETTINGS

REPORT = Path("report/final_report.md")
TABLES = Path("report/tables.md")


def sections(md: str) -> dict:
    out = {}
    for part in re.split(r"^## ", md, flags=re.M)[1:]:
        title, _, body = part.partition("\n")
        out[title.strip()] = body.strip()
    return out


def drop_retry_rows(table: str) -> str:
    return "\n".join(l for l in table.splitlines() if not re.match(r"\|\s*retry_", l))


def t3_block(sec: dict) -> str:
    body = next((v for k, v in sec.items() if k.startswith("Table 3")), "")
    if "|" in body:
        return body
    rows = ["| Setting (piece size / overlap, pieces fetched, re-rank) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total p50 / p95 (ms) |",
            "|---|---|---|---|---|---|---|---|"]
    for s in SETTINGS.values():
        rows.append(f"| {s.name}: {s.chunk_tokens}/{s.overlap_tokens}, {s.top_k}, {'yes' if s.rerank else 'no'} "
                    "| not measured | not measured | not measured | not measured | not measured | not measured | not measured |")
    return "\n".join(rows)


def fill(text: str, blocks: dict) -> str:
    for key, body in blocks.items():
        pat = re.compile(rf"(<!-- BEGIN:{key} -->)(.*?)(<!-- END:{key} -->)", re.S)
        if not pat.search(text):
            raise SystemExit(f"marker {key} missing in {REPORT}")
        text = pat.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(3)}", text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", action="store_true")
    a = ap.parse_args()
    sec = sections(TABLES.read_text(encoding="utf-8"))
    get = lambda prefix: next(v for k, v in sec.items() if k.startswith(prefix))
    blocks = {"T1": get("Table 1"), "T2A": "**Table 2a**\n\n" + drop_retry_rows(get("Table 2a")),
              "T2B": "**Table 2b**\n\n" + get("Table 2b"), "T3": "**Table 3**\n\n" + t3_block(sec),
              "RETRY": "**Retry strategies** [SIM] (retry_* scenarios; rows are in results.csv)\n\n" + get("Retry strategies")}
    REPORT.write_text(fill(REPORT.read_text(encoding="utf-8"), blocks), encoding="utf-8", newline="\n")
    print("tables filled into", REPORT)
    if a.pdf:
        return to_pdf()
    return 0


CSS = """@page{size:A4;margin:9mm 10mm}body{font:7.6pt/1.22 Arial,Helvetica,sans-serif;margin:0}
h1{font-size:11pt;margin:0 0 2px}h2{font-size:8.6pt;margin:5px 0 1px}p{margin:1px 0 2px}
table{border-collapse:collapse;width:100%;margin:2px 0 3px;font-size:6.3pt}th,td{border:.4px solid #888;padding:.6px 2px;text-align:left;vertical-align:top}
th{background:#eee}code{font-size:6.8pt}"""


def to_pdf() -> int:
    html = REPORT.with_suffix(".html")
    css = REPORT.parent / "_report.css"
    css.write_text(CSS, encoding="utf-8")
    subprocess.check_call(["pandoc", str(REPORT), "-f", "gfm", "-t", "html5", "-s", "--metadata", "title=The Relay",
                           "-c", str(css), "--embed-resources", "-o", str(html)])
    html.write_text(html.read_text(encoding="utf-8").replace('<h1 class="title">The Relay</h1>', ""), encoding="utf-8")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(html.resolve().as_uri())
        pg.pdf(path=str(REPORT.with_suffix(".pdf")), prefer_css_page_size=True, print_background=True)
        b.close()
    css.unlink(missing_ok=True)
    html.unlink(missing_ok=True)
    print("wrote", REPORT.with_suffix(".pdf"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
