"""Generates a static HTML side-by-side viewer: each paragraph snippet next
to EasyOCR/Tesseract/Surya text, with disagreements and low-confidence
snippets highlighted. No server or DB needed — open the file directly in a
browser."""

import html
from pathlib import Path

_ROW_TEMPLATE = """
<div class="row {row_class}">
  <img src="{image_path}" alt="snippet">
  <div class="col">
    <div class="meta">page {page} · side {side} · para #{seq}{agreement}</div>
    <div class="engine"><span class="label">EasyOCR</span> <span class="conf">{easy_conf:.0%}</span> {easy_text}</div>
    <div class="engine"><span class="label">Tesseract</span> <span class="conf">{tess_conf:.0%}</span> {tess_text}</div>
    <div class="engine"><span class="label">Surya</span> <span class="conf">{surya_conf:.0%}*</span> {surya_text}{surya_flag}</div>
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
  h1 {{ font-size: 1.1rem; color: #aaa; margin-bottom: 0.25rem; }}
  .note {{ font-size: 0.75rem; color: #777; margin-bottom: 1.25rem; }}
  .row {{ display: flex; gap: 1rem; align-items: flex-start; padding: 0.75rem; margin-bottom: 0.5rem; border-radius: 6px; background: #1a1a1a; }}
  .row.flagged {{ background: #2a1a1a; border-left: 3px solid #e05555; }}
  .row img {{ max-width: 320px; max-height: 200px; background: #fff; border-radius: 4px; }}
  .col {{ flex: 1; }}
  .meta {{ font-size: 0.75rem; color: #888; margin-bottom: 0.35rem; }}
  .agree {{ color: #4caf7d; }}
  .engine {{ font-size: 1.05rem; margin: 0.2rem 0; }}
  .label {{ display: inline-block; width: 90px; color: #7ab8ff; font-size: 0.8rem; }}
  .conf {{ color: #888; font-size: 0.8rem; }}
  .suspect {{ color: #e0a955; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>{count} paragraph snippets · {flagged_count} flagged · {agree_count} with 2+ engines in exact agreement</h1>
<div class="note">* Surya reports one mean-token-probability per snippet, not per-word confidence like the other two engines.</div>
{rows}
</body>
</html>
"""


def generate_report(records: list[dict], run_dir: Path, output_path: Path) -> None:
    rows = []
    flagged_count = 0
    agree_count = 0
    for r in records:
        is_flagged = r["flags"]["disagreement"] or r["flags"]["low_confidence"] or r["flags"]["surya_suspect"]
        if is_flagged:
            flagged_count += 1
        if r["flags"]["majority_agreement"]:
            agree_count += 1
        rows.append(
            _ROW_TEMPLATE.format(
                row_class="flagged" if is_flagged else "",
                image_path=r["image_patch_path"],
                page=r["page_number"],
                side=html.escape(r["side"]),
                seq=r["paragraph_sequence"],
                agreement=" · <span class='agree'>2+ engines agree</span>" if r["flags"]["majority_agreement"] else "",
                easy_conf=r["easyocr"]["avg_confidence"],
                tess_conf=r["tesseract"]["avg_confidence"],
                surya_conf=r["surya"]["avg_confidence"],
                easy_text=html.escape(r["easyocr"]["text"]) or "<i>(empty)</i>",
                tess_text=html.escape(r["tesseract"]["text"]) or "<i>(empty)</i>",
                surya_text=html.escape(r["surya"]["text"]) or "<i>(empty)</i>",
                surya_flag=" <span class='suspect'>(suspect output)</span>" if r["flags"]["surya_suspect"] else "",
            )
        )

    html_out = _PAGE_TEMPLATE.format(
        count=len(records),
        flagged_count=flagged_count,
        agree_count=agree_count,
        rows="\n".join(rows),
    )
    output_path.write_text(html_out, encoding="utf-8")
