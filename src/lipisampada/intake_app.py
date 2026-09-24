"""Phase A: PDF intake & queue app. Browse the Pratisangraha catalog
snapshot, preview a book's PDF, queue it for OCR. A background worker then
downloads -> rasterizes -> auto-preps each queued book's pages; a human
reviews/adjusts each page's crop/rotation/(optional) split in the browser;
once every page of a book is approved the worker hands the finished pages
to the existing pipeline.run_book() unchanged.

Run:
    uvicorn lipisampada.intake_app:app --reload
"""

import datetime
import hashlib
import json
import os
import threading
import time
import traceback
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from lipisampada import config
from lipisampada import page_ops
from lipisampada import intake_queue_db as qdb
from lipisampada import pdf_intake
from lipisampada import pipeline
from lipisampada import publisher
from lipisampada.reviewapi import storage as storagemod

config.load_env()  # API_BASE_URL / INGEST_API_KEY / storage settings from the git-ignored .env

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Overridable so tests can point at throwaway databases and never touch real data.
CATALOG_DB_PATH = Path(os.environ.get("INTAKE_CATALOG_DB", PROJECT_ROOT / "db" / "pratisangraha.sqlite3"))
QUEUE_DB_PATH = Path(os.environ.get("INTAKE_QUEUE_DB", PROJECT_ROOT / "db" / "intake_queue.sqlite3"))
WORK_ROOT = PROJECT_ROOT / "intake_work"
OUTPUT_ROOT = PROJECT_ROOT / "output"

if not CATALOG_DB_PATH.exists():
    raise RuntimeError(f"Catalog snapshot not found at {CATALOG_DB_PATH}")

app = FastAPI(title="Lipi-Sampada Intake")

_db_lock = threading.Lock()
_queue_conn = qdb.open_queue_db(QUEUE_DB_PATH)

# One OCR job at a time (single GPU / single Ollama): the book worker and single-page re-OCR share this.
_ocr_lock = threading.Lock()
_pause_requested: set[int] = set()
_reocr_state: dict[int, dict] = {}  # item_id -> {"page": n, "state": running|done|error, "message": str}


class OcrPaused(BaseException):
    """Raised from an OCR progress callback to stop a run cleanly. BaseException so the pipeline's
    per-page 'skip a broken page' handler does not swallow it."""


def _catalog_conn():
    import sqlite3

    conn = sqlite3.connect(f"file:{CATALOG_DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


CATALOG_QUERY = """
    SELECT e.id, e.entry_id, e.prati_link, e.kosha_link, e.publish_date_kannada, e.publish_date_english,
           e.view_count, k.name AS kavi_name, p.name AS prasanga_name, pub.name AS publisher_name
    FROM catalog_catalogentry e
    LEFT JOIN catalog_kavi k ON k.id = e.kavi_id
    LEFT JOIN catalog_prasanga p ON p.id = e.prasanga_id
    LEFT JOIN catalog_publisher pub ON pub.id = e.publisher_id
"""

# A book "has a kosha link" only if it holds a real URL. The catalog stores
# missing ones as NULL or as the Kannada placeholder text "(ನಿರೀಕ್ಷಿಸಿ)" (= "wait").
HAS_KOSHA_SQL = "e.kosha_link LIKE 'http%'"
NO_KOSHA_SQL = "(e.kosha_link IS NULL OR e.kosha_link NOT LIKE 'http%')"


def _queue_status_by_entry() -> dict:
    """catalog_entry_id -> (status, queue item id) of its most recent queue item."""
    with _db_lock:
        rows = _queue_conn.execute("SELECT id, catalog_entry_id, status FROM queue_items ORDER BY id").fetchall()
    return {r["catalog_entry_id"]: (r["status"], r["id"]) for r in rows}


def _entry_payload(row, queue_status: dict | None = None) -> dict:
    d = dict(row)
    d["prati_kind"] = pdf_intake.source_kind(d.get("prati_link"))
    d["preview_url"] = pdf_intake.preview_url(d["prati_link"]) if d.get("prati_link") else None
    d["has_kosha"] = bool(d.get("kosha_link") and d["kosha_link"].startswith("http"))
    d["kosha_preview_url"] = pdf_intake.preview_url(d["kosha_link"]) if d["has_kosha"] else None
    d["can_queue"] = d["prati_kind"] != "unsupported"
    status, item_id = (queue_status or {}).get(d["id"], (None, None))
    d["queue_status"], d["queue_item_id"] = status, item_id
    return d


@app.get("/api/catalog")
def list_catalog(search: str = "", kosha: str = "any", limit: int = 50, offset: int = 0):
    if kosha not in ("any", "has", "none"):
        raise HTTPException(400, "kosha must be any, has or none")
    limit = max(1, min(limit, 200))
    conn = _catalog_conn()
    try:
        search_sql, params = "", []
        if search.strip():
            search_sql = "(e.entry_id LIKE ? OR p.name LIKE ? OR k.name LIKE ? OR pub.name LIKE ?)"
            like = f"%{search.strip()}%"
            params = [like, like, like, like]
        kosha_sql = {"any": "", "has": HAS_KOSHA_SQL, "none": NO_KOSHA_SQL}[kosha]
        where = " AND ".join(x for x in (search_sql, kosha_sql) if x)
        where = f"WHERE {where}" if where else ""

        def count(kosha_filter: str) -> int:
            clauses = " AND ".join(x for x in (search_sql, kosha_filter) if x)
            return conn.execute(
                f"SELECT COUNT(*) AS n FROM ({CATALOG_QUERY} {'WHERE ' + clauses if clauses else ''})", params
            ).fetchone()["n"]

        counts = {"any": count(""), "has": count(HAS_KOSHA_SQL), "none": count(NO_KOSHA_SQL)}  # for the filter labels
        rows = conn.execute(
            f"{CATALOG_QUERY} {where} ORDER BY e.id LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()
        qs = _queue_status_by_entry()
        return {"total": counts[kosha], "counts": counts, "entries": [_entry_payload(r, qs) for r in rows]}
    finally:
        conn.close()


@app.get("/api/catalog/{entry_id}")
def get_catalog_entry(entry_id: int):
    conn = _catalog_conn()
    try:
        row = conn.execute(f"{CATALOG_QUERY} WHERE e.id = ?", (entry_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such catalog entry")
        return _entry_payload(row, _queue_status_by_entry())
    finally:
        conn.close()


class EnqueueRequest(BaseModel):
    catalog_entry_id: int


def _enqueue_entry(row) -> str:
    """'added' | 'already_queued' | 'unsupported' for one catalog row."""
    if pdf_intake.source_kind(row["prati_link"]) == "unsupported":
        return "unsupported"
    with _db_lock:
        active = _queue_conn.execute(
            "SELECT id FROM queue_items WHERE catalog_entry_id = ? AND status NOT IN ('done','failed')", (row["id"],)
        ).fetchone()
        done = _queue_conn.execute(
            "SELECT id FROM queue_items WHERE catalog_entry_id = ? AND status = 'done'", (row["id"],)
        ).fetchone()
        if active or done:
            return "already_queued"
        qdb.enqueue(
            _queue_conn,
            catalog_entry_id=row["id"],
            entry_code=row["entry_id"],
            book_id=row["entry_id"],
            title=row["prasanga_name"] or row["entry_id"],
            prati_link=row["prati_link"],
        )
    return "added"


@app.post("/api/queue")
def enqueue(req: EnqueueRequest):
    conn = _catalog_conn()
    try:
        row = conn.execute(f"{CATALOG_QUERY} WHERE e.id = ?", (req.catalog_entry_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "no such catalog entry")
    outcome = _enqueue_entry(row)
    if outcome == "unsupported":
        raise HTTPException(400, "this entry's prati link is not a Drive, archive.org or PDF link")
    with _db_lock:
        item = _queue_conn.execute(
            "SELECT id FROM queue_items WHERE catalog_entry_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()
    return {"queue_item_id": item["id"], "outcome": outcome}


class BulkEnqueueRequest(BaseModel):
    catalog_entry_ids: list[int]


@app.post("/api/queue/bulk")
def enqueue_bulk(req: BulkEnqueueRequest):
    ids = list(dict.fromkeys(req.catalog_entry_ids))[:500]
    result = {"added": [], "already_queued": [], "unsupported": [], "missing": []}
    conn = _catalog_conn()
    try:
        for entry_id in ids:
            row = conn.execute(f"{CATALOG_QUERY} WHERE e.id = ?", (entry_id,)).fetchone()
            if row is None:
                result["missing"].append(entry_id)
            else:
                result[_enqueue_entry(row)].append(entry_id)
    finally:
        conn.close()
    return result


@app.post("/api/queue/{item_id}/retry")
def retry_item(item_id: int):
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        if item is None:
            raise HTTPException(404, "no such queue item")
        if item["status"] != qdb.STATUS_FAILED:
            raise HTTPException(409, f"item is {item['status']}, not failed")
        pages = qdb.list_pages(_queue_conn, item_id)
        if pages and all(p["approved"] for p in pages):
            # already through human review last time - just retry the OCR handoff
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_APPROVED, error=None)
        elif pages:
            # rasterized but review wasn't finished - resume the review step
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_AWAITING_REVIEW, error=None)
        else:
            # never got past download/rasterize - start over from the top
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_QUEUED, error=None)
    return {"ok": True}


@app.get("/api/queue")
def get_queue():
    with _db_lock:
        return {"items": [dict(r) for r in qdb.list_queue(_queue_conn)]}


@app.get("/api/queue/{item_id}")
def get_queue_item(item_id: int):
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        if item is None:
            raise HTTPException(404, "no such queue item")
        pages = qdb.list_pages(_queue_conn, item_id)
    return {"item": dict(item), "pages": [dict(p) for p in pages]}


@app.get("/api/queue/{item_id}/pages/{page_id}/image")
def get_page_image(item_id: int, page_id: int):
    with _db_lock:
        page = _queue_conn.execute("SELECT * FROM queue_pages WHERE id = ? AND queue_item_id = ?", (page_id, item_id)).fetchone()
    if page is None:
        raise HTTPException(404, "no such page")
    path = Path(page["raster_path"])
    if not path.exists():
        raise HTTPException(404, "raster image missing on disk")
    return FileResponse(path)


# ---------------- page editor: preview, auto-crop, auto-dewarp, approve ----------------

PREVIEW_SOURCE_MAX = 1600  # long side of the working copy used for live previews
_small_cache: dict = {}    # (path, mtime, rot90) -> (downscaled rotated image, scale); a few pages only


def _page_row(item_id: int, page_id: int):
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        page = _queue_conn.execute(
            "SELECT * FROM queue_pages WHERE id = ? AND queue_item_id = ?", (page_id, item_id)
        ).fetchone()
    if item is None or page is None:
        raise HTTPException(404, "no such queue item/page")
    return item, page


def _original_path(page) -> Path:
    return Path(page["original_path"] or page["raster_path"])


def _load_full(page, rot90: int) -> np.ndarray:
    img = cv2.imread(str(_original_path(page)), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(404, "page image missing on disk")
    try:
        return page_ops.rotate90(img, rot90)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _load_small(page, rot90: int):
    path = _original_path(page)
    key = (str(path), path.stat().st_mtime if path.exists() else 0, rot90 % 360)
    if key not in _small_cache:
        full = _load_full(page, rot90)
        s = min(1.0, PREVIEW_SOURCE_MAX / max(full.shape[:2]))
        small = cv2.resize(full, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else full
        if len(_small_cache) >= 4:
            _small_cache.pop(next(iter(_small_cache)))
        _small_cache[key] = (small, s)
    return _small_cache[key]


def _dewarp_cache_file(item, page, rot90: int, region: dict) -> Path:
    key = json.dumps([rot90 % 360, region["quad"], region.get("mids")], sort_keys=True)
    return Path(item["work_dir"]) / "dewarp" / f"p{page['id']}_{hashlib.sha1(key.encode()).hexdigest()[:12]}.png"


class PreviewRequest(BaseModel):
    rot90: int = 0
    regions: list[dict]
    active: int = 0
    max_width: int = 900


@app.post("/api/queue/{item_id}/pages/{page_id}/preview")
def preview_region(item_id: int, page_id: int, req: PreviewRequest):
    """The processed result of one crop (warp -> enhance), from a downscaled working copy so
    dragging handles feels live. Block size of adaptive binarize is scaled to match."""
    item, page = _page_row(item_id, page_id)
    small, s = _load_small(page, req.rot90)
    h, w = small.shape[:2]
    try:
        regions = page_ops.validate_regions(req.regions, w / s, h / s)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not 0 <= req.active < len(regions):
        raise HTTPException(400, "no such crop")
    r = regions[req.active]
    dewarped = None
    if r.get("dewarp") == "auto":
        f = _dewarp_cache_file(item, page, req.rot90, r)
        if f.exists():
            dewarped = cv2.imread(str(f))
    rs = page_ops.scale_region(r, s)
    rs["enhance"] = {**rs["enhance"], "block": max(3, int(rs["enhance"]["block"] * s)) | 1}
    if dewarped is not None:
        dewarped = cv2.resize(dewarped, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    out = page_ops.apply_region(small, rs, dewarped)
    if out.shape[1] > req.max_width:
        k = req.max_width / out.shape[1]
        out = cv2.resize(out, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    binary = r["enhance"]["binarize"] != "off"
    ok, buf = cv2.imencode(".png" if binary else ".jpg", out, [] if binary else [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Response(buf.tobytes(), media_type="image/png" if binary else "image/jpeg",
                    headers={"X-Dewarp": "cached" if dewarped is not None else ("missing" if r.get("dewarp") == "auto" else "none")})


class AutoCropRequest(BaseModel):
    rot90: int = 0


@app.post("/api/queue/{item_id}/pages/{page_id}/auto-crop")
def auto_crop(item_id: int, page_id: int, req: AutoCropRequest):
    _, page = _page_row(item_id, page_id)
    return {"regions": page_ops.auto_suggest(_load_full(page, req.rot90))}


class AutoDewarpRequest(BaseModel):
    rot90: int = 0
    regions: list[dict]
    index: int = 0


@app.post("/api/queue/{item_id}/pages/{page_id}/auto-dewarp")
def auto_dewarp(item_id: int, page_id: int, req: AutoDewarpRequest):
    """Slow (seconds): text-line dewarp of one crop, cached against its exact shape."""
    item, page = _page_row(item_id, page_id)
    full = _load_full(page, req.rot90)
    h, w = full.shape[:2]
    try:
        regions = page_ops.validate_regions(req.regions, w, h)
        r = regions[req.index]
        flat = page_ops.warp_region(full, r["quad"], r.get("mids"))
        out = page_ops.auto_dewarp(flat)
    except (ValueError, IndexError) as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    f = _dewarp_cache_file(item, page, req.rot90, r)
    f.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(f), out)
    return {"ok": True, "size": [out.shape[1], out.shape[0]]}


class ApprovePageRequest(BaseModel):
    rot90: int = 0
    regions: list[dict] | None = None
    # legacy payload from the old single-box editor; converted to regions
    crop_box: list[float] | None = None
    rotation: float = 0.0
    split: bool = False
    split_x: float | None = None


def _commit_page(item, page, rot90: int, regions_raw: list[dict] | None, legacy: tuple | None = None):
    """Render + write one page's approved crops. Image work runs outside the DB lock."""
    full = _load_full(page, rot90)
    h, w = full.shape[:2]
    if regions_raw is None:
        regions_raw = pdf_intake.legacy_to_regions(w, h, *legacy) if legacy else [page_ops.neutral_region(page_ops.full_quad(w, h))]
    try:
        regions = page_ops.validate_regions(regions_raw, w, h)
    except ValueError as e:
        raise HTTPException(400, str(e))
    dewarped = {}
    for i, r in enumerate(regions):
        if r.get("dewarp") == "auto":
            f = _dewarp_cache_file(item, page, rot90, r)
            if f.exists():
                dewarped[i] = cv2.imread(str(f))
            else:
                r["dewarp"] = "none"  # the shape changed since it was dewarped; never silently use a stale result
    # a page re-approved with a different number of crops must not leave the old files behind
    for old in json.loads(page["final_paths"] or "[]"):
        Path(old).unlink(missing_ok=True)
    written = pdf_intake.commit_regions(
        _original_path(page), Path(item["work_dir"]) / "approved", page["pdf_page_index"] + 1, rot90, regions,
        dewarped=dewarped,
    )
    return regions, [str(p) for p in written]


def _record_approval(item_id: int, page_id: int, rot90: int, regions: list[dict], paths: list[str]) -> bool:
    with _db_lock:
        qdb.approve_regions(_queue_conn, page_id, rot90 % 360, json.dumps(regions), json.dumps(paths))
        qdb.update_reviewed_count(_queue_conn, item_id)
        remaining = _queue_conn.execute(
            "SELECT COUNT(*) AS n FROM queue_pages WHERE queue_item_id = ? AND approved = 0", (item_id,)
        ).fetchone()["n"]
        # only the first review moves a book on to OCR; re-saving a page of a book that already
        # went through OCR (to re-crop / re-OCR it) must not start the whole book again
        if remaining == 0 and qdb.get_item(_queue_conn, item_id)["status"] == qdb.STATUS_AWAITING_REVIEW:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_APPROVED)
    return remaining == 0


def _require_review(item):
    if item["status"] != qdb.STATUS_AWAITING_REVIEW:
        raise HTTPException(409, f"item is {item['status']}, not awaiting review")


REOCR_OK_STATUSES = (qdb.STATUS_PAUSED, qdb.STATUS_DONE, qdb.STATUS_FAILED)


def _require_editable(item):
    """Pages can be (re)cropped while awaiting review, or after OCR has started but is not running."""
    if item["status"] != qdb.STATUS_AWAITING_REVIEW and item["status"] not in REOCR_OK_STATUSES:
        raise HTTPException(409, f"item is {item['status']}; pause the OCR first to edit a page")


@app.post("/api/queue/{item_id}/pages/{page_id}/approve")
def approve_page(item_id: int, page_id: int, req: ApprovePageRequest):
    item, page = _page_row(item_id, page_id)
    _require_editable(item)
    legacy = (req.crop_box, req.rotation, req.split, req.split_x) if req.regions is None and req.crop_box else None
    regions, paths = _commit_page(item, page, req.rot90, req.regions, legacy)
    return {"all_pages_approved": _record_approval(item_id, page_id, req.rot90, regions, paths), "files": len(paths)}


@app.post("/api/queue/{item_id}/accept-auto")
def accept_auto(item_id: int):
    """Approve every page not yet approved using its automatic crop suggestion (neutral
    enhancement). For clean books where the suggestions are simply right."""
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        if item is None:
            raise HTTPException(404, "no such queue item")
        _require_review(item)
        todo = [p for p in qdb.list_pages(_queue_conn, item_id) if not p["approved"]]
    all_done = False
    for page in todo:
        suggestion = json.loads(page["auto_regions"]) if page["auto_regions"] else None
        regions, paths = _commit_page(item, page, 0, suggestion)
        all_done = _record_approval(item_id, page["id"], 0, regions, paths)
    return {"approved": len(todo), "all_pages_approved": all_done or not todo}


@app.post("/api/queue/{item_id}/publish")
def republish(item_id: int):
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        if item is None:
            raise HTTPException(404, "no such queue item")
        if item["status"] != qdb.STATUS_DONE:
            raise HTTPException(409, f"item is {item['status']}; only finished (done) books can be published")
        qdb.set_publish(_queue_conn, item_id, None)  # the worker picks it up on its next tick
    return {"ok": True}


@app.post("/api/queue/{item_id}/pause")
def pause_ocr(item_id: int):
    """Stops OCR at the next snippet (or immediately if it has not started yet)."""
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        if item is None:
            raise HTTPException(404, "no such queue item")
        if item["status"] == qdb.STATUS_APPROVED:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_PAUSED)
        elif item["status"] == qdb.STATUS_OCR_RUNNING:
            _pause_requested.add(item_id)
        elif item["status"] != qdb.STATUS_PAUSED:
            raise HTTPException(409, f"item is {item['status']}, not running OCR")
    return {"ok": True}


@app.post("/api/queue/{item_id}/resume")
def resume_ocr(item_id: int):
    """Continues from the last finished page (run_book resume=True keeps everything already done)."""
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
        if item is None:
            raise HTTPException(404, "no such queue item")
        if item["status"] != qdb.STATUS_PAUSED:
            raise HTTPException(409, f"item is {item['status']}, not paused")
        _pause_requested.discard(item_id)
        qdb.set_status(_queue_conn, item_id, qdb.STATUS_APPROVED)
    return {"ok": True}


def _replace_page_records(result_path: Path, page_number: int, new_records: list[dict]):
    old = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else []
    kept = [r for r in old if r["page_number"] != page_number]
    merged = sorted(kept + new_records, key=lambda r: r["page_number"])  # stable: keeps each page's own order
    tmp = result_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(result_path)


def _reocr_worker(item, page, paths: list[Path]):
    item_id, page_number = item["id"], page["pdf_page_index"] + 1
    run_dir = Path(item["run_dir"])
    try:
        new_records = []
        cdb = pipeline.refiner.open_corrections_db(run_dir / "corrections.db")
        try:
            for p in paths:
                new_records.extend(pipeline.process_page(
                    p, run_dir / "snippets", run_dir / "pages", item["book_id"], True, cdb))
        finally:
            cdb.close()
        # only now, with the new text safely in hand, replace the old
        _replace_page_records(run_dir / "result.json", page_number, new_records)
        keep = {p.with_suffix(".png").name for p in paths}
        for f in (run_dir / "pages").glob("*.png"):  # thumbnails of an earlier crop layout (e.g. 1 crop -> 2)
            try:
                if pipeline.layout.parse_filename(f.name, book_id=item["book_id"]).page_number == page_number and f.name not in keep:
                    f.unlink()
            except Exception:
                pass
        if item["publish_status"] == "published":
            with _db_lock:  # ingest is idempotent and never overwrites text a reviewer touched
                qdb.set_publish(_queue_conn, item_id, None)
        _reocr_state[item_id] = {"page": page_number, "state": "done", "message": f"page {page_number} re-read"}
    except Exception as e:
        traceback.print_exc()
        _reocr_state[item_id] = {"page": page_number, "state": "error", "message": str(e)}
    finally:
        _ocr_lock.release()


@app.post("/api/queue/{item_id}/pages/{page_id}/reocr")
def reocr_page(item_id: int, page_id: int):
    """Re-reads one page with its current (re-cropped / re-enhanced) approved image(s), replacing
    only that page's text in the book's result. The book itself does not restart."""
    item, page = _page_row(item_id, page_id)
    if item["status"] not in REOCR_OK_STATUSES:
        raise HTTPException(409, f"item is {item['status']}; pause the OCR first, then re-read a page")
    if not item["run_dir"] or not page["approved"]:
        raise HTTPException(409, "this page has not been through OCR yet - resume the book instead")
    paths = [Path(p) for p in json.loads(page["final_paths"] or "[]")]
    if not paths or not all(p.exists() for p in paths):
        raise HTTPException(409, "the page's approved images are missing - save the page in the editor first")
    if not _ocr_lock.acquire(blocking=False):
        raise HTTPException(409, "another OCR job is running - try again when it finishes or is paused")
    _reocr_state[item_id] = {"page": page["pdf_page_index"] + 1, "state": "running", "message": ""}
    threading.Thread(target=_reocr_worker, args=(item, page, paths), daemon=True).start()
    return {"ok": True, "was_published": item["publish_status"] == "published"}


@app.get("/api/queue/{item_id}/progress")
def get_progress(item_id: int):
    with _db_lock:
        item = qdb.get_item(_queue_conn, item_id)
    if item is None:
        raise HTTPException(404, "no such queue item")
    run_dir = Path(item["run_dir"]) if item["run_dir"] else None
    pages = []
    records_by_page = {}
    if run_dir and run_dir.exists():
        pages_dir = run_dir / "pages"
        if pages_dir.exists():
            pages = sorted(p.name for p in pages_dir.glob("*.png"))
        result_path = run_dir / "result.json"
        if result_path.exists():
            try:
                records = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                records = []  # can be mid-write; next poll will pick up the completed version
            for r in records:
                key = f"{r['page_number']}_{r['side']}"
                records_by_page.setdefault(key, []).append(r)
    return {
        "status": item["status"],
        "page_count": item["page_count"],
        "processed_page_count": item["processed_page_count"],
        "current_snippet_index": item["current_snippet_index"],
        "current_snippet_total": item["current_snippet_total"],
        "pages": pages,
        "records_by_page": records_by_page,
        "book_id": item["book_id"],
        "reocr": _reocr_state.get(item_id),
        "publish_status": item["publish_status"],
    }


output_dir = OUTPUT_ROOT
output_dir.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=output_dir), name="output")

static_dir = Path(__file__).parent / "intake_static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


# ---------------- background worker: advances the queue one step at a time ----------------

def _advance_queue_forever():
    while True:
        try:
            _advance_one_step()
        except Exception:
            traceback.print_exc()
        time.sleep(2)


def _advance_one_step():
    with _db_lock:
        item = qdb.next_pending(_queue_conn)
        unpublished = qdb.next_unpublished(_queue_conn) if item is None else None
    if item is None:
        if unpublished is not None:
            _run_publish(unpublished)  # OCR is finished and nothing else is queued ahead of it
        return

    # DOWNLOADING/DOWNLOADED/RASTERIZING/OCR_RUNNING are normally only ever
    # observed transiently (set by the very call that's about to advance
    # past them) - the only way next_pending() hands one back here is if a
    # previous worker died or was restarted mid-step (e.g. a --reload
    # triggered by an edit) and orphaned it. Both handlers re-glob their
    # inputs fresh each call, so restarting the whole step is always safe.
    if item["status"] in (qdb.STATUS_QUEUED, qdb.STATUS_DOWNLOADING, qdb.STATUS_DOWNLOADED, qdb.STATUS_RASTERIZING):
        _run_download_and_rasterize(item)
    elif item["status"] in (qdb.STATUS_APPROVED, qdb.STATUS_OCR_RUNNING):
        _run_ocr(item)


def _run_download_and_rasterize(item):
    item_id = item["id"]
    work_dir = WORK_ROOT / item["book_id"]
    pdf_path = work_dir / "source.pdf"
    raster_dir = work_dir / "raster"

    with _db_lock:
        qdb.set_status(_queue_conn, item_id, qdb.STATUS_DOWNLOADING, work_dir=str(work_dir))
    try:
        pdf_intake.download_pdf(item["prati_link"], pdf_path)
        with _db_lock:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_DOWNLOADED)

        with _db_lock:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_RASTERIZING)
        raster_paths = pdf_intake.rasterize_pdf(pdf_path, raster_dir)
        pages = []
        for i, raster_path in enumerate(raster_paths):
            # The rasterized page is left untouched; the auto crop is only a suggestion the
            # reviewer sees drawn over it, so a wrong guess can always be widened later.
            regions = page_ops.auto_suggest(cv2.imread(str(raster_path), cv2.IMREAD_COLOR))
            pages.append({
                "pdf_page_index": i, "raster_path": str(raster_path), "original_path": str(raster_path),
                "auto_regions": json.dumps(regions), "is_likely_spread": page_ops.looks_like_spread(regions),
            })

        with _db_lock:
            qdb.add_pages(_queue_conn, item_id, pages)
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_AWAITING_REVIEW, page_count=len(pages))
    except Exception as e:
        traceback.print_exc()
        with _db_lock:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_FAILED, error=str(e))


def _run_ocr(item):
    item_id = item["id"]
    work_dir = Path(item["work_dir"])
    approved_dir = work_dir / "approved"
    run_dir = OUTPUT_ROOT / item["book_id"]

    with _ocr_lock:  # waits if a single-page re-OCR is in progress
        _run_ocr_locked(item_id, item, approved_dir, run_dir)


def _run_ocr_locked(item_id, item, approved_dir, run_dir):
    _pause_requested.discard(item_id)  # a stale click from before this run started
    with _db_lock:
        qdb.set_status(_queue_conn, item_id, qdb.STATUS_OCR_RUNNING, run_dir=str(run_dir), processed_page_count=0)
    try:
        image_paths = sorted(approved_dir.glob("*.tif"))
        if not image_paths:
            raise RuntimeError("no approved page images found")

        # Every approved page is already fully prepared - show them all
        # immediately rather than waiting for the (slow) OCR loop to reach
        # each one; process_page's own write overwrites each with the
        # further-preprocessed version as OCR actually gets to it.
        pdf_intake.copy_pages_preview(approved_dir, run_dir / "pages")

        def on_page(index, total, image_path):
            with _db_lock:
                qdb.update_progress(_queue_conn, item_id, index)
            if item_id in _pause_requested:
                raise OcrPaused()

        def on_snippet(page_index, page_total, snippet_index, snippet_total):
            with _db_lock:
                qdb.update_snippet_progress(_queue_conn, item_id, snippet_index, snippet_total)
            if item_id in _pause_requested:
                raise OcrPaused()

        # resume=True: a restart/reload/retry continues from the pages already
        # in result.json instead of redoing the whole book from page 1.
        pipeline.run_book(
            image_paths, item["book_id"], run_dir, refine=True, on_page=on_page, on_snippet=on_snippet,
            resume=True,
        )
        with _db_lock:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_DONE)
    except OcrPaused:
        # the page in progress is not in result.json yet, so resuming redoes just that page
        with _db_lock:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_PAUSED)
    except Exception as e:
        traceback.print_exc()
        with _db_lock:
            qdb.set_status(_queue_conn, item_id, qdb.STATUS_FAILED, error=str(e))


def _publish_configured() -> bool:
    return bool(os.environ.get("API_BASE_URL") and os.environ.get("INGEST_API_KEY"))


def _run_publish(item):
    """Pushes a finished book to the review platform (images -> storage, text
    + image URLs -> review API). A failure only marks the *publish* as failed
    - the OCR result stays 'done' and can be re-published with one click."""
    item_id = item["id"]
    if not _publish_configured():
        with _db_lock:
            qdb.set_publish(_queue_conn, item_id, "skipped", "API_BASE_URL / INGEST_API_KEY not set in .env")
        return
    with _db_lock:
        qdb.set_publish(_queue_conn, item_id, "publishing")
    try:
        kavi = None
        conn = _catalog_conn()
        try:
            row = conn.execute(f"{CATALOG_QUERY} WHERE e.id = ?", (item["catalog_entry_id"],)).fetchone()
            kavi = row["kavi_name"] if row else None
        finally:
            conn.close()
        api_base = os.environ["API_BASE_URL"]
        result = publisher.publish_book(
            Path(item["run_dir"]),
            item["book_id"],
            storagemod.from_env(api_base),
            publisher.http_post(api_base, os.environ["INGEST_API_KEY"]),
            title=item["title"],
            kavi=kavi,
            catalog_entry_id=item["catalog_entry_id"],
            progress=lambda m: print(f"[publish {item['book_id']}] {m}"),
        )
        print(f"[publish {item['book_id']}] done: {result}")
        with _db_lock:
            qdb.set_publish(_queue_conn, item_id, "published")
    except Exception as e:
        traceback.print_exc()
        with _db_lock:
            qdb.set_publish(_queue_conn, item_id, "failed", str(e))


if not os.environ.get("INTAKE_NO_WORKER"):  # tests set this so no background download/OCR ever starts
    _worker_thread = threading.Thread(target=_advance_queue_forever, daemon=True)
    _worker_thread.start()

    def _memlog_note():
        row = _queue_conn.execute("SELECT book_id, processed_page_count, current_snippet_index FROM queue_items WHERE status='ocr_running' LIMIT 1").fetchone()
        return f"{row['book_id']} page {row['processed_page_count'] + 1} snippet {row['current_snippet_index']}" if row else ""

    try:
        from lipisampada import memlog

        memlog.start(PROJECT_ROOT / "logs" / "memory_log.csv", _memlog_note)
    except ImportError:
        pass  # psutil not installed: the app works, just without the memory log
