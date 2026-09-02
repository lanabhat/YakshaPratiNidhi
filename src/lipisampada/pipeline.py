"""CLI: page(s) -> preprocess -> slice into paragraph snippets -> run
EasyOCR + Tesseract on each -> write result.json (+ snippet PNGs)."""

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

from lipisampada import layout, preprocessing
from lipisampada.ensemble import compare
from lipisampada.ocr_engines import run_easyocr, run_tesseract


def process_page(image_path: Path, snippets_dir: Path, book_id: str) -> list[dict]:
    meta = layout.parse_filename(image_path.name, book_id=book_id)
    gray, binary = preprocessing.preprocess_page(str(image_path))
    snippets = layout.slice_page(binary)

    records = []
    for snippet in snippets:
        crop = layout.crop_snippet(gray, snippet)
        if crop.size == 0:
            continue

        patch_name = f"{image_path.stem}_p{snippet.paragraph_sequence}.png"
        patch_path = snippets_dir / patch_name
        cv2.imwrite(str(patch_path), crop)

        easy = run_easyocr(crop)
        tess = run_tesseract(crop)
        flags = compare(easy, tess)

        records.append(
            {
                "book_id": meta.book_id,
                "page_number": meta.page_number,
                "side": meta.side,
                "paragraph_sequence": snippet.paragraph_sequence,
                "column_index": snippet.column_index,
                "bbox": list(snippet.bbox),
                "image_patch_path": f"snippets/{patch_name}",
                "easyocr": {"text": easy.text, "avg_confidence": round(easy.avg_confidence, 4)},
                "tesseract": {"text": tess.text, "avg_confidence": round(tess.avg_confidence, 4)},
                "flags": flags,
            }
        )
    return records


def main():
    parser = argparse.ArgumentParser(description="Lipi-Sampada Phase 0 OCR pipeline")
    parser.add_argument("--input", required=True, help="Directory of scanned .tif pages")
    parser.add_argument("--pages", type=int, default=None, help="Limit to first N pages (sorted by filename)")
    parser.add_argument("--book-id", default="Prasanga")
    parser.add_argument("--output", default=None, help="Output run directory (default: output/<timestamp>)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    image_paths = sorted(input_dir.glob("*.tif"))
    if args.pages:
        image_paths = image_paths[: args.pages]
    if not image_paths:
        print(f"No .tif files found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    run_id = args.output or f"output/{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = Path(run_id)
    snippets_dir = run_dir / "snippets"
    snippets_dir.mkdir(parents=True, exist_ok=True)

    all_records = []
    for i, image_path in enumerate(image_paths, 1):
        print(f"[{i}/{len(image_paths)}] Processing {image_path.name} ...")
        records = process_page(image_path, snippets_dir, args.book_id)
        all_records.extend(records)
        print(f"  -> {len(records)} paragraph snippets")

    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {len(all_records)} records to {result_path}")

    from lipisampada.report import generate_report

    report_path = run_dir / "report.html"
    generate_report(all_records, run_dir, report_path)
    print(f"Wrote visual QA report to {report_path}")


if __name__ == "__main__":
    main()
