"""Phase 2 verification API: serves paragraph snippets (image + AI draft)
to the review frontend and applies the two-reviewer consensus logic.

Run against one pipeline output directory:
    uvicorn lipisampada.review_app:app --reload
    (point it at a run via the LIPISAMPADA_RUN_DIR env var, or it defaults
    to the newest folder under output/)
"""

import json
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from lipisampada import refiner, review_db

# FastAPI runs sync endpoints in a threadpool, so two requests (e.g. two
# reviewers submitting at once) could otherwise race on the same
# read-modify-write in submit_review. One lock is plenty at this app's scale.
_db_lock = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _default_run_dir() -> Path:
    output_dir = PROJECT_ROOT / "output"
    runs = [p for p in output_dir.iterdir() if p.is_dir() and (p / "result.json").exists()]
    if not runs:
        raise RuntimeError(f"No pipeline run with result.json found under {output_dir}")
    return max(runs, key=lambda p: p.stat().st_mtime)


RUN_DIR = Path(os.environ["LIPISAMPADA_RUN_DIR"]) if "LIPISAMPADA_RUN_DIR" in os.environ else _default_run_dir()

app = FastAPI(title="Yaksha - PratiNidhi (ಯಕ್ಷ-ಪ್ರತಿ-ನಿಧಿ) Review")
app.mount("/images", StaticFiles(directory=RUN_DIR), name="images")

_conn = review_db.open_review_db(RUN_DIR / "review.db")
review_db.seed_from_result_json(_conn, RUN_DIR / "result.json")

# Same corrections.db the pipeline's LLM refiner writes to (created fresh
# here if the run was done with --no-refine) — finalized human decisions
# get logged into it below, so it stays the one self-correction dictionary
# for a run regardless of which phase produced each entry.
_corrections_conn = refiner.open_corrections_db(RUN_DIR / "corrections.db")


class ReviewSubmission(BaseModel):
    snippet_id: str
    reviewer: str
    text: str
    is_expert: bool = False


def _snippet_payload(row) -> dict:
    d = dict(row)
    d["image_url"] = f"/images/{d['image_path']}"
    d["page_image_url"] = f"/images/{d['page_image_path']}" if d.get("page_image_path") else None
    d["bbox"] = json.loads(d["bbox"]) if d.get("bbox") else None
    d["position"], d["total_count"] = review_db.position_info(_conn, d["id"])
    if d["status"] in review_db.TERMINAL_STATUSES or d["status"] == review_db.STATUS_NEEDS_EXPERT:
        d["reviews"] = [dict(r) for r in review_db.reviews_for(_conn, d["id"])]
    return d


@app.get("/api/stats")
def get_stats():
    with _db_lock:
        return review_db.stats(_conn)


@app.get("/api/next")
def get_next(reviewer: str):
    if not reviewer.strip():
        raise HTTPException(400, "reviewer name is required")
    with _db_lock:
        row = review_db.next_snippet(_conn, reviewer.strip())
        if row is None:
            return {"snippet": None}
        payload = _snippet_payload(row)
    return {"snippet": payload}


@app.get("/api/snippet/{snippet_id}/adjacent")
def get_adjacent(snippet_id: str, direction: str):
    if direction not in ("next", "prev"):
        raise HTTPException(400, "direction must be 'next' or 'prev'")
    with _db_lock:
        try:
            row = review_db.adjacent_snippet(_conn, snippet_id, direction)
        except ValueError as e:
            raise HTTPException(404, str(e))
        if row is None:
            return {"snippet": None}
        payload = _snippet_payload(row)
    return {"snippet": payload}


@app.post("/api/review")
def post_review(submission: ReviewSubmission):
    if not submission.reviewer.strip():
        raise HTTPException(400, "reviewer name is required")
    if not submission.text.strip():
        raise HTTPException(400, "submitted text cannot be empty")
    try:
        with _db_lock:
            new_status = review_db.submit_review(
                _conn, submission.snippet_id, submission.reviewer.strip(), submission.text, submission.is_expert
            )
            if new_status in review_db.TERMINAL_STATUSES:
                row = _conn.execute("SELECT * FROM snippets WHERE id = ?", (submission.snippet_id,)).fetchone()
                reviewers = sorted({r["reviewer_name"] for r in review_db.reviews_for(_conn, submission.snippet_id)})
                refiner.log_human_correction(
                    _corrections_conn,
                    book_id=row["book_id"],
                    page_number=row["page_number"],
                    side=row["side"],
                    paragraph_sequence=row["paragraph_sequence"],
                    easyocr_text=row["easyocr_text"],
                    tesseract_text=row["tesseract_text"],
                    surya_text=row["surya_text"],
                    final_text=row["final_text"],
                    reviewers=reviewers,
                    image_patch_path=row["image_path"],
                )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"status": new_status}


static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
