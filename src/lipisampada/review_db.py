"""Phase 2 data model: seeds a review.db from a pipeline run's result.json,
and implements the two-reviewer consensus logic from the original brief:

1. Snippet shown to reviewer A with the AI draft (refined_text).
2. If A submits it unchanged -> confirmed (one human confirmation of the
   AI draft is treated as sufficient; the AI already synthesized three
   engines' readings, so this isn't a lone unchecked guess).
3. If A edits it -> awaiting a second, independent reviewer B, who is shown
   the *original* AI draft (not A's edit) so their read is independent.
4. If B's submission matches A's edit -> provisionally_verified.
   If it differs from both A's edit and the original draft -> needs_expert.
   If B also leaves it unchanged (implicitly disagreeing with A that an
   edit was needed) -> needs_expert.
5. An expert review on a needs_expert snippet is final: expert_approved.
"""

import json
import sqlite3
from pathlib import Path

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_AWAITING_SECOND = "awaiting_second_review"
STATUS_PROVISIONALLY_VERIFIED = "provisionally_verified"
STATUS_NEEDS_EXPERT = "needs_expert"
STATUS_EXPERT_APPROVED = "expert_approved"

TERMINAL_STATUSES = {STATUS_CONFIRMED, STATUS_PROVISIONALLY_VERIFIED, STATUS_EXPERT_APPROVED}


def _snippet_id(r: dict) -> str:
    return f"{r['book_id']}:{r['page_number']}:{r['side']}:{r['paragraph_sequence']}"


def open_review_db(db_path: Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync endpoints in a threadpool,
    # but requests are handled one at a time against this single connection
    # (fine at this app's review-team scale; a real multi-writer deployment
    # would want a connection per request or a proper server-side DB).
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snippets (
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            side TEXT NOT NULL,
            paragraph_sequence INTEGER NOT NULL,
            image_path TEXT NOT NULL,
            page_image_path TEXT,
            bbox TEXT,
            easyocr_text TEXT,
            tesseract_text TEXT,
            surya_text TEXT,
            refined_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            final_text TEXT,
            pending_edit_text TEXT,
            pending_edit_reviewer TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    # Migration for review.db files created before page_image_path/bbox
    # existed (CREATE TABLE IF NOT EXISTS doesn't add columns to an
    # already-existing table) — same pattern as refiner.open_corrections_db.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(snippets)")}
    if "page_image_path" not in existing_cols:
        conn.execute("ALTER TABLE snippets ADD COLUMN page_image_path TEXT")
    if "bbox" not in existing_cols:
        conn.execute("ALTER TABLE snippets ADD COLUMN bbox TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snippet_id TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            shown_text TEXT NOT NULL,
            submitted_text TEXT NOT NULL,
            changed INTEGER NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def seed_from_result_json(conn: sqlite3.Connection, result_json_path: Path) -> int:
    """Inserts every paragraph record as a pending snippet. Records whose id
    already exists are left untouched (safe to re-run)."""
    records = json.loads(result_json_path.read_text(encoding="utf-8"))
    inserted = 0
    for r in records:
        refined = r.get("refined", {}).get("text") or r["easyocr"]["text"]
        # .get(): both fields postdate this project increment — older
        # result.json files won't have them, and should seed as NULL
        # rather than crash (the review app hides the "view full page"
        # affordance when page_image_path is missing).
        page_image_path = r.get("page_image_path")
        bbox = json.dumps(r["bbox"]) if "bbox" in r else None
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO snippets
                (id, book_id, page_number, side, paragraph_sequence, image_path, page_image_path, bbox,
                 easyocr_text, tesseract_text, surya_text, refined_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _snippet_id(r),
                r["book_id"],
                r["page_number"],
                r["side"],
                r["paragraph_sequence"],
                r["image_patch_path"],
                page_image_path,
                bbox,
                r["easyocr"]["text"],
                r["tesseract"]["text"],
                r["surya"]["text"],
                refined,
                STATUS_PENDING,
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def next_snippet(conn: sqlite3.Connection, reviewer: str) -> sqlite3.Row | None:
    """Next snippet needing a review from this reviewer: prefers
    needs_expert (oldest work first), then awaiting_second_review (never
    from the same reviewer who made the first edit), then pending."""
    row = conn.execute(
        "SELECT * FROM snippets WHERE status = ? ORDER BY updated_at LIMIT 1",
        (STATUS_NEEDS_EXPERT,),
    ).fetchone()
    if row:
        return row

    row = conn.execute(
        """
        SELECT * FROM snippets
        WHERE status = ? AND pending_edit_reviewer != ?
        ORDER BY updated_at LIMIT 1
        """,
        (STATUS_AWAITING_SECOND, reviewer),
    ).fetchone()
    if row:
        return row

    return conn.execute(
        "SELECT * FROM snippets WHERE status = ? ORDER BY updated_at LIMIT 1",
        (STATUS_PENDING,),
    ).fetchone()


def adjacent_snippet(conn: sqlite3.Connection, snippet_id: str, direction: str) -> sqlite3.Row | None:
    """The next/previous snippet in page order (SQLite rowid, which matches
    seeding order = page-processing order) regardless of review status —
    lets a reviewer browse back/forward, not just work the pending queue.
    Returns None at a boundary (first/last snippet)."""
    row = conn.execute("SELECT rowid FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such snippet: {snippet_id}")
    op = "<" if direction == "prev" else ">"
    order = "DESC" if direction == "prev" else "ASC"
    return conn.execute(
        f"SELECT * FROM snippets WHERE rowid {op} ? ORDER BY rowid {order} LIMIT 1",
        (row["rowid"],),
    ).fetchone()


def position_info(conn: sqlite3.Connection, snippet_id: str) -> tuple[int, int]:
    """1-based (position, total) of a snippet in the same page-order used by
    adjacent_snippet, for a "12 of 60" style label."""
    total = conn.execute("SELECT COUNT(*) AS n FROM snippets").fetchone()["n"]
    row = conn.execute("SELECT rowid FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such snippet: {snippet_id}")
    position = conn.execute(
        "SELECT COUNT(*) AS n FROM snippets WHERE rowid <= ?", (row["rowid"],)
    ).fetchone()["n"]
    return position, total


def submit_review(
    conn: sqlite3.Connection, snippet_id: str, reviewer: str, submitted_text: str, is_expert: bool
) -> str:
    """Applies one review to a snippet per the consensus rules above.
    Returns the snippet's new status."""
    row = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such snippet: {snippet_id}")

    submitted_text = submitted_text.strip()
    status = row["status"]

    if is_expert and status == STATUS_NEEDS_EXPERT:
        _record_review(conn, snippet_id, reviewer, row["refined_text"], submitted_text, "expert")
        new_status, final_text = STATUS_EXPERT_APPROVED, submitted_text

    elif status == STATUS_PENDING:
        changed = submitted_text != row["refined_text"].strip()
        _record_review(conn, snippet_id, reviewer, row["refined_text"], submitted_text, "first")
        if not changed:
            new_status, final_text = STATUS_CONFIRMED, row["refined_text"]
        else:
            conn.execute(
                "UPDATE snippets SET pending_edit_text = ?, pending_edit_reviewer = ? WHERE id = ?",
                (submitted_text, reviewer, snippet_id),
            )
            new_status, final_text = STATUS_AWAITING_SECOND, None

    elif status == STATUS_AWAITING_SECOND:
        if reviewer == row["pending_edit_reviewer"]:
            raise ValueError("the second reviewer must differ from the first")
        _record_review(conn, snippet_id, reviewer, row["refined_text"], submitted_text, "second")
        if submitted_text == row["pending_edit_text"] and submitted_text != row["refined_text"].strip():
            new_status, final_text = STATUS_PROVISIONALLY_VERIFIED, submitted_text
        else:
            new_status, final_text = STATUS_NEEDS_EXPERT, None

    else:
        raise ValueError(f"snippet {snippet_id} is already {status}, not open for review")

    conn.execute(
        "UPDATE snippets SET status = ?, final_text = ?, updated_at = datetime('now') WHERE id = ?",
        (new_status, final_text, snippet_id),
    )
    conn.commit()
    return new_status


def _record_review(conn, snippet_id, reviewer, shown_text, submitted_text, role):
    conn.execute(
        """
        INSERT INTO reviews (snippet_id, reviewer_name, shown_text, submitted_text, changed, role)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (snippet_id, reviewer, shown_text, submitted_text, int(submitted_text.strip() != shown_text.strip()), role),
    )


def reviews_for(conn: sqlite3.Connection, snippet_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reviews WHERE snippet_id = ? ORDER BY created_at", (snippet_id,)
    ).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM snippets GROUP BY status").fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    done = sum(counts.get(s, 0) for s in TERMINAL_STATUSES)
    return {"total": total, "done": done, "by_status": counts}
