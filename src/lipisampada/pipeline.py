"""CLI: page(s) -> preprocess -> slice into paragraph snippets -> run
EasyOCR + Tesseract + Surya on each snippet -> AI-refine via local LLM ->
write result.json (+ snippet PNGs + a self-correction dictionary DB)."""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import cv2

from lipisampada import layout, preprocessing, refiner
from lipisampada.ensemble import compare
from lipisampada.ocr_engines import run_easyocr, run_surya, run_tesseract


def process_page(
    image_path: Path,
    snippets_dir: Path,
    pages_dir: Path,
    book_id: str,
    refine: bool,
    corrections_db: sqlite3.Connection | None,
    on_snippet=None,
) -> list[dict]:
    """on_snippet(index, total), if given, is called after each of this
    page's snippets completes - a page can have a dozen-plus snippets each
    taking real OCR+refine time, so without this, per-page progress alone
    can look stalled for minutes while a lot of real work is happening."""
    meta = layout.parse_filename(image_path.name, book_id=book_id)
    gray, binary = preprocessing.preprocess_page(str(image_path))
    snippets = layout.slice_page(binary)

    # One full-page reference image per page, so the review app can show a
    # reviewer the surrounding context (e.g. a continuing verse/song) that a
    # tightly-cropped snippet alone loses — see review_app.py/static/index.html.
    page_image_name = f"{image_path.stem}.png"
    cv2.imwrite(str(pages_dir / page_image_name), gray)
    page_image_path = f"pages/{page_image_name}"

    records = []
    for snippet_index, snippet in enumerate(snippets, 1):
        crop = layout.crop_snippet(gray, snippet)
        if crop.size == 0:
            if on_snippet is not None:
                on_snippet(snippet_index, len(snippets))
            continue

        patch_name = f"{image_path.stem}_p{snippet.paragraph_sequence}.png"
        patch_path = snippets_dir / patch_name
        cv2.imwrite(str(patch_path), crop)

        easy = run_easyocr(crop)
        tess = run_tesseract(crop)
        surya = run_surya(crop)
        flags = compare(easy, tess, surya)

        record = {
            "book_id": meta.book_id,
            "page_number": meta.page_number,
            "side": meta.side,
            "paragraph_sequence": snippet.paragraph_sequence,
            "column_index": snippet.column_index,
            "bbox": list(snippet.bbox),
            "image_patch_path": f"snippets/{patch_name}",
            "page_image_path": page_image_path,
            "easyocr": {"text": easy.text, "avg_confidence": round(easy.avg_confidence, 4)},
            "tesseract": {"text": tess.text, "avg_confidence": round(tess.avg_confidence, 4)},
            "surya": {"text": surya.text, "avg_confidence": round(surya.avg_confidence, 4)},
            "flags": flags,
        }

        if refine:
            try:
                refined_text = refiner.refine(easy.text, tess.text, surya.text, flags)
                record["refined"] = {"text": refined_text, "model": refiner.OLLAMA_MODEL}
            except Exception as e:
                # A single stuck/slow LLM call (e.g. a cold model load) must
                # not lose an entire book's remaining pages in an unattended
                # queue run - fall back to a non-LLM best guess and keep going;
                # flagged so reviewers/QA can tell this snippet skipped the LLM pass.
                refined_text = refiner.fallback_text(easy.text, tess.text, surya.text, flags)
                record["refined"] = {"text": refined_text, "model": "fallback-no-llm"}
                record["flags"]["refine_failed"] = str(e)
            if corrections_db is not None:
                refiner.log_correction(corrections_db, record, refined_text)

        records.append(record)
        if on_snippet is not None:
            on_snippet(snippet_index, len(snippets))
    return records


def run_book(
    image_paths: list[Path],
    book_id: str,
    run_dir: Path,
    refine: bool = True,
    progress=print,
    on_page=None,
    on_snippet=None,
    on_page_error=None,
    resume: bool = False,
) -> Path:
    """Runs the full OCR+refine pipeline over one book's already-prepared
    page images (already correctly oriented/cropped/named), writing
    result.json + snippets/ + pages/ + report.html under run_dir. Shared by
    the CLI (main(), below) and the intake queue app so both go through
    the exact same OCR/refine logic. Returns run_dir.

    result.json is (re)written after every page, not just at the end, so a
    caller (or a person watching the run dir) can see live progress rather
    than nothing until the whole book finishes. on_page(index, total,
    image_path), if given, is called after each page completes (whether it
    succeeded or was skipped); on_snippet(page_index, page_total, snippet_index,
    snippet_total) after each snippet within a page - a page can have a
    dozen-plus snippets each taking real OCR+refine time, so page-level
    progress alone can look stalled for minutes with real work happening;
    the intake queue app uses both for its UI. on_page_error(image_path,
    exception), if given, is called instead of (before) on_page whenever a
    page's own OCR raises - the loop still keeps going to the next page (one
    bad/corrupt page must not lose the rest of an unattended book), but the
    caller needs to know a page was silently skipped rather than assuming
    every page that ran actually produced a record.

    resume=True picks up an interrupted run: pages whose records are already
    in result.json (written after every page, so only whole pages are ever
    in it) are skipped, and their records kept. A page interrupted midway
    isn't in result.json yet, so it is simply redone from its first snippet."""
    snippets_dir = run_dir / "snippets"
    pages_dir = run_dir / "pages"
    snippets_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"

    corrections_db = refiner.open_corrections_db(run_dir / "corrections.db") if refine else None

    all_records = []
    done_pages = set()
    if resume and result_path.exists():
        try:
            all_records = json.loads(result_path.read_text(encoding="utf-8"))
            done_pages = {(r["page_number"], r["side"]) for r in all_records}
        except (json.JSONDecodeError, KeyError):
            all_records, done_pages = [], set()  # unreadable checkpoint: start over rather than trust it

    try:
        for i, image_path in enumerate(image_paths, 1):
            meta = layout.parse_filename(image_path.name, book_id=book_id)
            if (meta.page_number, meta.side) in done_pages:
                progress(f"[{i}/{len(image_paths)}] {image_path.name} already done, skipping")
                if on_page is not None:
                    on_page(i, len(image_paths), image_path)
                continue
            progress(f"[{i}/{len(image_paths)}] Processing {image_path.name} ...")
            try:
                page_on_snippet = (
                    (lambda si, st, _i=i, _n=len(image_paths): on_snippet(_i, _n, si, st))
                    if on_snippet is not None
                    else None
                )
                records = process_page(
                    image_path, snippets_dir, pages_dir, book_id, refine, corrections_db, page_on_snippet
                )
                all_records.extend(records)
                progress(f"  -> {len(records)} paragraph snippets")
            except Exception as e:
                # One unreadable/corrupt page must not lose every other
                # page's work in an unattended queue run - skip it and keep going.
                progress(f"  !! failed, skipping this page: {e}")
                if on_page_error is not None:
                    on_page_error(image_path, e)
            result_path.write_text(json.dumps(all_records, ensure_ascii=False, indent=2), encoding="utf-8")
            if on_page is not None:
                on_page(i, len(image_paths), image_path)
    finally:
        if corrections_db is not None:
            corrections_db.close()

    progress(f"\nWrote {len(all_records)} records to {result_path}")

    from lipisampada.report import generate_report

    report_path = run_dir / "report.html"
    generate_report(all_records, run_dir, report_path)
    progress(f"Wrote visual QA report to {report_path}")
    return run_dir


def main():
    parser = argparse.ArgumentParser(description="Lipi-Sampada OCR + AI-refiner pipeline")
    parser.add_argument("--input", required=True, help="Directory of scanned .tif pages")
    parser.add_argument("--pages", type=int, default=None, help="Limit to first N pages (sorted by filename)")
    parser.add_argument("--book-id", default="Prasanga")
    parser.add_argument("--output", default=None, help="Output run directory (default: output/<timestamp>)")
    parser.add_argument(
        "--no-refine", action="store_true", help="Skip the LLM refinement step (Phase 0 behavior; no Ollama required)"
    )
    args = parser.parse_args()

    input_dir = Path(args.input)
    image_paths = sorted(input_dir.glob("*.tif"))
    if args.pages:
        image_paths = image_paths[: args.pages]
    if not image_paths:
        print(f"No .tif files found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    run_id = args.output or f"output/{time.strftime('%Y%m%d_%H%M%S')}"
    run_book(image_paths, args.book_id, Path(run_id), refine=not args.no_refine)


if __name__ == "__main__":
    main()
