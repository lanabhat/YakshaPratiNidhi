"""Generates a static HTML side-by-side viewer: per page, Surya's full-page
transcription, followed by each paragraph snippet next to EasyOCR/Tesseract
text, with disagreements/low-confidence highlighted. No server or DB
needed — open the file directly in a browser."""

import html
from pathlib import Path

_PAGE_HEADER_TEMPLATE = """
<div class="page-block">
  <div class="page-title">Page {page} · side {side}</div>
  <div class="surya-row">
    <img src="{image_path}" alt="page">
    <div class="col">
      <div class="meta">Surya (full page, one call)</div>
      <div class="engine">{surya_text}</div>
    </div>
  </div>
"""

_ROW_TEMPLATE = """
<div class="row {row_class}">
  <img src="{image_path}" alt="snippet">
  <div class="col">
    <div class="meta">para #{seq}</div>
    <div class="engine"><span class="label">EasyOCR</span> ({easy_conf:.0%}) {easy_text}</div>
    <div class="engine"><span class="label">Tesseract</span> ({tess_conf:.0%}) {tess_text}</div>
  </div>
</div>
"""

_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Lipi-Sampada OCR QA Report</title>
<style>
  body {{ font-family: sans-serif; background: #111; color: #eee; margin: 0; padding: 1.5rem; }}
  h1 {{ font-size: 1.1rem; color: #aaa; }}
  .page-block {{ margin-bottom: 2rem; }}
  .page-title {{ font-size: 0.9rem; color: #999; margin: 1.5rem 0 0.5rem; border-bottom: 1px solid #333; padding-bottom: 0.25rem; }}
  .surya-row {{ display: flex; gap: 1rem; align-items: flex-start; padding: 0.75rem; margin-bottom: 0.75rem; border-radius: 6px; background: #16211a; border-left: 3px solid #4caf7d; }}
  .surya-row img {{ max-width: 220px; max-height: 320px; background: #fff; border-radius: 4px; }}
  .row {{ display: flex; gap: 1rem; align-items: flex-start; padding: 0.75rem; margin-bottom: 0.5rem; border-radius: 6px; background: #1a1a1a; }}
  .row.flagged {{ background: #2a1a1a; border-left: 3px solid #e05555; }}
  .row img {{ max-width: 320px; max-height: 140px; background: #fff; border-radius: 4px; }}
  .col {{ flex: 1; }}
  .meta {{ font-size: 0.75rem; color: #888; margin-bottom: 0.25rem; }}
  .engine {{ font-size: 1.05rem; margin: 0.2rem 0; }}
  .label {{ display: inline-block; width: 90px; color: #7ab8ff; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>{count} paragraph snippets across {page_count} pages · {flagged_count} flagged for review</h1>
{blocks}
</body>
</html>
"""


def generate_report(records: list[dict], surya_pages: list[dict], run_dir: Path, output_path: Path) -> None:
    flagged_count = sum(1 for r in records if r["flags"]["disagreement"] or r["flags"]["low_confidence"])

    records_by_page = {}
    for r in records:
        key = (r["page_number"], r["side"])
        records_by_page.setdefault(key, []).append(r)

    blocks = []
    for surya_rec in surya_pages:
        key = (surya_rec["page_number"], surya_rec["side"])
        blocks.append(
            _PAGE_HEADER_TEMPLATE.format(
                page=surya_rec["page_number"],
                side=html.escape(surya_rec["side"]),
                image_path=surya_rec["image_patch_path"],
                surya_text=html.escape(surya_rec["surya"]["text"]) or "<i>(empty)</i>",
            )
        )
        for r in records_by_page.get(key, []):
            is_flagged = r["flags"]["disagreement"] or r["flags"]["low_confidence"]
            blocks.append(
                _ROW_TEMPLATE.format(
                    row_class="flagged" if is_flagged else "",
                    image_path=r["image_patch_path"],
                    seq=r["paragraph_sequence"],
                    easy_conf=r["easyocr"]["avg_confidence"],
                    tess_conf=r["tesseract"]["avg_confidence"],
                    easy_text=html.escape(r["easyocr"]["text"]) or "<i>(empty)</i>",
                    tess_text=html.escape(r["tesseract"]["text"]) or "<i>(empty)</i>",
                )
            )
        blocks.append("</div>")

    html_out = _PAGE_TEMPLATE.format(
        count=len(records),
        page_count=len(surya_pages),
        flagged_count=flagged_count,
        blocks="\n".join(blocks),
    )
    output_path.write_text(html_out, encoding="utf-8")
