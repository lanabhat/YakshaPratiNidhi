"""Phase 3 active learning loop: aggregates human-verified corrections
across pipeline runs, checks the retraining trigger, and prepares data for
fine-tuning Tesseract's Kannada model via tesstrain.

Only `source='human'` rows count — an LLM draft (source='llm') is a
guess, not verified ground truth, and fine-tuning on unverified guesses
would teach the engine to repeat whatever mistakes the LLM didn't catch.

Deliberately doesn't include a "run tesstrain now" step: at the scale
this project has real corrections (a handful, from Phase 2 testing, not
real review work), an actual fine-tuning run wouldn't be meaningful —
see check_retraining_trigger() and the CLI's default behavior.
"""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from lipisampada import refiner

DEFAULT_THRESHOLD = 1000
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def find_corrections_dbs(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("*/corrections.db"))


def _human_rows(db_path: Path) -> list[sqlite3.Row]:
    # Routed through open_corrections_db (not a raw sqlite3.connect) so its
    # schema migration runs first — older runs' corrections.db files
    # predate the source/reviewers/image_patch_path columns.
    conn = refiner.open_corrections_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT book_id, page_number, side, paragraph_sequence,
                   tesseract_text, refined_text AS human_correction, image_patch_path
            FROM corrections
            WHERE source = 'human' AND image_patch_path IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()
    return rows


def collect_human_corrections(db_paths: list[Path]) -> list[dict]:
    """One entry per (db_path, row): resolves image_patch_path relative to
    that db's run directory, since paths are stored relative to the run
    they came from."""
    out = []
    for db_path in db_paths:
        run_dir = db_path.parent
        for row in _human_rows(db_path):
            out.append(
                {
                    "original_ocr": row["tesseract_text"] or "",
                    "human_correction": row["human_correction"] or "",
                    "image_patch_path": str(run_dir / row["image_patch_path"]),
                    "book_id": row["book_id"],
                    "page_number": row["page_number"],
                    "side": row["side"],
                    "paragraph_sequence": row["paragraph_sequence"],
                }
            )
    return out


def check_retraining_trigger(corrections: list[dict], threshold: int = DEFAULT_THRESHOLD) -> dict:
    return {"count": len(corrections), "threshold": threshold, "ready": len(corrections) >= threshold}


def export_jsonl(corrections: list[dict], output_path: Path) -> None:
    """Writes the exact {"original_ocr", "human_correction",
    "image_patch_path"} pairs the original brief's Active Learning Log
    describes, one JSON object per line."""
    with output_path.open("w", encoding="utf-8") as f:
        for c in corrections:
            f.write(
                json.dumps(
                    {
                        "original_ocr": c["original_ocr"],
                        "human_correction": c["human_correction"],
                        "image_patch_path": c["image_patch_path"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def prepare_tesstrain_data(corrections: list[dict], ground_truth_dir: Path, lang: str = "kan") -> int:
    """Stages image + .gt.txt pairs in the layout tesstrain
    (github.com/tesseract-ocr/tesstrain) expects under its data/<lang>-ground-truth/
    folder: <name>.png next to <name>.gt.txt holding the correct text.
    Skips corrections whose source image is missing rather than failing
    the whole batch. Returns how many pairs were staged.

    This only stages the data — it does not run tesstrain. Actually
    fine-tuning requires cloning tesstrain and having Tesseract's training
    tools (lstmtraining, combine_lang_model, etc.), which the standard
    tesseract-ocr.tesseract Windows package used elsewhere in this project
    does not include. See README for the run command once enough data
    exists to make training worthwhile.
    """
    ground_truth_dir.mkdir(parents=True, exist_ok=True)
    staged = 0
    for i, c in enumerate(corrections):
        src_image = Path(c["image_patch_path"])
        if not src_image.is_file():
            continue
        name = f"{lang}_{c['book_id']}_{c['page_number']}_{c['side']}_{c['paragraph_sequence']}_{i}"
        shutil.copy(src_image, ground_truth_dir / f"{name}{src_image.suffix}")
        (ground_truth_dir / f"{name}.gt.txt").write_text(c["human_correction"], encoding="utf-8")
        staged += 1
    return staged


def main():
    parser = argparse.ArgumentParser(description="Lipi-Sampada Phase 3 active learning tooling")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output"), help="Directory containing pipeline run folders")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="Corrections needed before retraining is 'ready'")
    parser.add_argument("--export-jsonl", metavar="PATH", help="Write the brief's {original_ocr, human_correction, image_patch_path} pairs to this file")
    parser.add_argument("--prepare-tesstrain", metavar="DIR", help="Stage image+.gt.txt pairs for tesstrain into this directory")
    parser.add_argument("--lang", default="kan", help="Language code prefix for staged tesstrain filenames (default: kan)")
    args = parser.parse_args()

    db_paths = find_corrections_dbs(Path(args.output_dir))
    corrections = collect_human_corrections(db_paths)
    trigger = check_retraining_trigger(corrections, args.threshold)

    print(f"Scanned {len(db_paths)} corrections.db file(s) under {args.output_dir}")
    print(f"Human-verified corrections: {trigger['count']} / {trigger['threshold']} needed to retrain")
    print("Retraining trigger:", "READY" if trigger["ready"] else "not yet")

    if args.export_jsonl:
        export_jsonl(corrections, Path(args.export_jsonl))
        print(f"Wrote {len(corrections)} pairs to {args.export_jsonl}")

    if args.prepare_tesstrain:
        staged = prepare_tesstrain_data(corrections, Path(args.prepare_tesstrain), args.lang)
        print(f"Staged {staged}/{len(corrections)} image+ground-truth pairs into {args.prepare_tesstrain}")
        if not trigger["ready"]:
            print(
                f"Note: only {trigger['count']} corrections available — well under the "
                f"{trigger['threshold']} the retraining trigger wants. Data is staged for "
                "inspection, but running tesstrain on this little ground truth wouldn't "
                "produce a meaningfully improved model."
            )


if __name__ == "__main__":
    main()
