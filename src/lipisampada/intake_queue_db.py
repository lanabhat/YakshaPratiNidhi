"""Queue state for the PDF intake app: one row per catalog book a reviewer
has queued for OCR, tracked through download -> rasterize -> human crop
review -> OCR handoff. Separate from db/pratisangraha.sqlite3 (the handed-off
catalog snapshot, which this never writes to) and separate from any book's
own review.db (seeded only once OCR has actually run)."""

import sqlite3
from pathlib import Path

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_DOWNLOADED = "downloaded"
STATUS_RASTERIZING = "rasterizing"
STATUS_AWAITING_REVIEW = "awaiting_review"
STATUS_APPROVED = "approved"
STATUS_OCR_RUNNING = "ocr_running"
STATUS_PAUSED = "paused"  # OCR stopped by the user; not picked up by next_pending until resumed
STATUS_DONE = "done"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = {STATUS_DONE, STATUS_FAILED}


def open_queue_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS queue_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_entry_id INTEGER NOT NULL,
            entry_code TEXT NOT NULL,
            book_id TEXT NOT NULL,
            title TEXT,
            prati_link TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            error TEXT,
            work_dir TEXT,
            run_dir TEXT,
            page_count INTEGER,
            reviewed_page_count INTEGER NOT NULL DEFAULT 0,
            processed_page_count INTEGER NOT NULL DEFAULT 0,
            current_snippet_index INTEGER,
            current_snippet_total INTEGER,
            publish_status TEXT,
            publish_error TEXT,
            published_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    existing_item_cols = {row[1] for row in conn.execute("PRAGMA table_info(queue_items)")}
    if "processed_page_count" not in existing_item_cols:
        conn.execute("ALTER TABLE queue_items ADD COLUMN processed_page_count INTEGER NOT NULL DEFAULT 0")
    if "current_snippet_index" not in existing_item_cols:
        conn.execute("ALTER TABLE queue_items ADD COLUMN current_snippet_index INTEGER")
        conn.execute("ALTER TABLE queue_items ADD COLUMN current_snippet_total INTEGER")
    if "publish_status" not in existing_item_cols:
        conn.execute("ALTER TABLE queue_items ADD COLUMN publish_status TEXT")
        conn.execute("ALTER TABLE queue_items ADD COLUMN publish_error TEXT")
        conn.execute("ALTER TABLE queue_items ADD COLUMN published_at TEXT")
    if "publish_target" not in existing_item_cols:
        conn.execute("ALTER TABLE queue_items ADD COLUMN publish_target TEXT")
    if "local_publish_status" not in existing_item_cols:
        for col in ("local_publish_status", "local_publish_error", "local_published_at", "local_publish_target",
                    "web_publish_status", "web_publish_error", "web_published_at", "web_publish_target"):
            conn.execute(f"ALTER TABLE queue_items ADD COLUMN {col} TEXT")
        # everything published before local/web were split out was, in effect, a publish to web
        conn.execute(
            "UPDATE queue_items SET web_publish_status = publish_status, web_publish_error = publish_error, "
            "web_published_at = published_at, web_publish_target = publish_target WHERE publish_status IS NOT NULL"
        )
    if "local_publish_progress" not in existing_item_cols:
        conn.execute("ALTER TABLE queue_items ADD COLUMN local_publish_progress TEXT")
        conn.execute("ALTER TABLE queue_items ADD COLUMN web_publish_progress TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS queue_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_item_id INTEGER NOT NULL,
            pdf_page_index INTEGER NOT NULL,
            raster_path TEXT NOT NULL,
            is_likely_spread INTEGER NOT NULL DEFAULT 0,
            crop_box TEXT,
            rotation REAL NOT NULL DEFAULT 0,
            split INTEGER NOT NULL DEFAULT 0,
            split_x REAL,
            approved INTEGER NOT NULL DEFAULT 0,
            final_paths TEXT,
            original_path TEXT,
            auto_regions TEXT,
            regions TEXT,
            rot90 INTEGER NOT NULL DEFAULT 0,
            UNIQUE(queue_item_id, pdf_page_index)
        )
        """
    )
    # Migration for queue databases created before the page editor v2 (CREATE TABLE
    # IF NOT EXISTS does not add columns to an existing table).
    page_cols = {row[1] for row in conn.execute("PRAGMA table_info(queue_pages)")}
    for col, decl in (("original_path", "TEXT"), ("auto_regions", "TEXT"), ("regions", "TEXT"),
                      ("rot90", "INTEGER NOT NULL DEFAULT 0")):
        if col not in page_cols:
            conn.execute(f"ALTER TABLE queue_pages ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def enqueue(conn: sqlite3.Connection, catalog_entry_id: int, entry_code: str, book_id: str, title: str, prati_link: str) -> int:
    existing = conn.execute(
        "SELECT id FROM queue_items WHERE catalog_entry_id = ? AND status NOT IN ('done','failed')",
        (catalog_entry_id,),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO queue_items (catalog_entry_id, entry_code, book_id, title, prati_link) VALUES (?, ?, ?, ?, ?)",
        (catalog_entry_id, entry_code, book_id, title, prati_link),
    )
    conn.commit()
    return cur.lastrowid


def list_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM queue_items ORDER BY id").fetchall()


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()


def next_pending(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The next item the background runner should advance, in FIFO order -
    one book at a time, matching the single-GPU/single-Ollama constraint
    the rest of the pipeline already has.

    Includes DOWNLOADING/RASTERIZING/OCR_RUNNING (not just their "not
    started yet" counterparts QUEUED/APPROVED) so a worker restart (e.g. a
    code reload) that killed an in-progress step resumes it instead of
    leaving the item silently stuck forever - every step this can land on
    mid-restart (_run_download_and_rasterize, _run_ocr) re-globs its inputs
    fresh each call, so restarting a step from its beginning is always
    safe, just possibly redundant work for whatever that step had already
    done before being interrupted."""
    return conn.execute(
        """
        SELECT * FROM queue_items
        WHERE status IN (?, ?, ?, ?, ?, ?)
        ORDER BY id LIMIT 1
        """,
        (
            STATUS_QUEUED,
            STATUS_DOWNLOADING,
            STATUS_DOWNLOADED,
            STATUS_RASTERIZING,
            STATUS_APPROVED,
            STATUS_OCR_RUNNING,
        ),
    ).fetchone()


def next_unpublished(conn: sqlite3.Connection, stage: str) -> sqlite3.Row | None:
    """A finished (OCR done) book still to be pushed to `stage` ('local' or 'web').

    'local' auto-publishes the moment OCR finishes - NULL means "never attempted, eligible for
    auto-pickup" (or interrupted mid-publish by a restart), matching this query's original single-
    target behavior. 'web' never fires on its own: only an explicit publish-to-web click (which sets
    web_publish_status='requested', see set_publish/the /publish/web route) makes it eligible - a
    fresh, never-published book's web_publish_status stays NULL and is never auto-picked-up here.
    Either stage: failed/skipped items wait for an explicit retry rather than looping forever."""
    if stage == "local":
        return conn.execute(
            "SELECT * FROM queue_items WHERE status = ? AND (local_publish_status IS NULL OR local_publish_status = 'publishing') "
            "ORDER BY id LIMIT 1",
            (STATUS_DONE,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM queue_items WHERE status = ? AND web_publish_status IN ('requested', 'publishing') ORDER BY id LIMIT 1",
        (STATUS_DONE,),
    ).fetchone()


def set_publish(conn: sqlite3.Connection, item_id: int, stage: str, publish_status: str | None, error: str | None = None, target: str | None = None):
    """stage: 'local' or 'web' - always a literal from our own code, never request input, so it's safe
    to interpolate as a column-name prefix here (never build it from user-supplied data).
    target: where this publish attempt went (e.g. "yakshapratinidhi.pythonanywhere.com + supabase
    storage"), so the queue UI can tell a publish to production apart from one to a local dev API/
    storage. Left alone (COALESCE) when not known for this call, e.g. the 'skipped' case, which never
    got far enough to know a target - the UI then still shows whatever the last real attempt used."""
    assert stage in ("local", "web")
    prefix = f"{stage}_"
    conn.execute(
        f"UPDATE queue_items SET {prefix}publish_status = ?, {prefix}publish_error = ?, "
        f"{prefix}publish_target = COALESCE(?, {prefix}publish_target), {prefix}publish_progress = NULL, "
        f"{prefix}published_at = CASE WHEN ? = 'published' THEN datetime('now') ELSE {prefix}published_at END, "
        "updated_at = datetime('now') WHERE id = ?",
        (publish_status, error, target, publish_status, item_id),
    )
    conn.commit()


def set_publish_progress(conn: sqlite3.Connection, item_id: int, stage: str, message: str):
    """A short human status line while a publish is actively running (e.g. "uploaded 12/94 images"),
    polled by the queue UI. set_publish() clears this back to NULL on every status transition, so it
    only ever reflects the *current* attempt, never a stale one left over from a previous run."""
    assert stage in ("local", "web")
    conn.execute(f"UPDATE queue_items SET {stage}_publish_progress = ? WHERE id = ?", (message, item_id))
    conn.commit()


def set_status(conn: sqlite3.Connection, item_id: int, status: str, error: str | None = None, **fields):
    cols = ["status = ?", "updated_at = datetime('now')"]
    params = [status]
    if error is not None or status != STATUS_FAILED:
        cols.append("error = ?")
        params.append(error)
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        params.append(v)
    params.append(item_id)
    conn.execute(f"UPDATE queue_items SET {', '.join(cols)} WHERE id = ?", params)
    conn.commit()


def update_progress(conn: sqlite3.Connection, item_id: int, processed_page_count: int):
    conn.execute(
        "UPDATE queue_items SET processed_page_count = ?, current_snippet_index = NULL, "
        "current_snippet_total = NULL, updated_at = datetime('now') WHERE id = ?",
        (processed_page_count, item_id),
    )
    conn.commit()


def update_snippet_progress(conn: sqlite3.Connection, item_id: int, snippet_index: int, snippet_total: int):
    conn.execute(
        "UPDATE queue_items SET current_snippet_index = ?, current_snippet_total = ? WHERE id = ?",
        (snippet_index, snippet_total, item_id),
    )
    conn.commit()


def add_pages(conn: sqlite3.Connection, queue_item_id: int, pages: list[dict]):
    """pages: [{pdf_page_index, raster_path, is_likely_spread, original_path?, auto_regions?}, ...]
    raster_path/original_path point at the untouched rasterized page; auto_regions is the
    automatic crop suggestion (JSON), shown to the reviewer to accept or adjust."""
    for p in pages:
        conn.execute(
            """
            INSERT OR IGNORE INTO queue_pages
                (queue_item_id, pdf_page_index, raster_path, is_likely_spread, original_path, auto_regions)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (queue_item_id, p["pdf_page_index"], p["raster_path"], int(p["is_likely_spread"]),
             p.get("original_path"), p.get("auto_regions")),
        )
    conn.commit()


def list_pages(conn: sqlite3.Connection, queue_item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM queue_pages WHERE queue_item_id = ? ORDER BY pdf_page_index", (queue_item_id,)
    ).fetchall()


def update_page(conn: sqlite3.Connection, page_id: int, **fields):
    cols = [f"{k} = ?" for k in fields]
    params = list(fields.values()) + [page_id]
    conn.execute(f"UPDATE queue_pages SET {', '.join(cols)} WHERE id = ?", params)
    conn.commit()


def approve_page(conn: sqlite3.Connection, page_id: int, crop_box: str, rotation: float, split: bool, split_x: float | None, final_paths: str):
    conn.execute(
        """
        UPDATE queue_pages
        SET crop_box = ?, rotation = ?, split = ?, split_x = ?, final_paths = ?, approved = 1
        WHERE id = ?
        """,
        (crop_box, rotation, int(split), split_x, final_paths, page_id),
    )
    conn.commit()


def approve_regions(conn: sqlite3.Connection, page_id: int, rot90: int, regions_json: str, final_paths: str):
    conn.execute(
        "UPDATE queue_pages SET rot90 = ?, regions = ?, final_paths = ?, approved = 1 WHERE id = ?",
        (rot90, regions_json, final_paths, page_id),
    )
    conn.commit()


def update_reviewed_count(conn: sqlite3.Connection, item_id: int) -> int:
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM queue_pages WHERE queue_item_id = ? AND approved = 1", (item_id,)
    ).fetchone()["n"]
    conn.execute("UPDATE queue_items SET reviewed_page_count = ? WHERE id = ?", (n, item_id))
    conn.commit()
    return n
