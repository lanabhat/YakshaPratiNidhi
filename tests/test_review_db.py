import json

from lipisampada import review_db


def test_seed_from_result_json_handles_single_engine_records(tmp_path):
    """Regression: seed_from_result_json (the legacy Phase 2 review app's seeding step) unconditionally
    read r["easyocr"]["text"]/["tesseract"]/["surya"], which crashes (KeyError) for the now-default
    Tesseract-only records - same bug class as publisher.py and report.py hit for the same reason."""
    records = [
        {
            "book_id": "BK", "page_number": 1, "side": "P", "paragraph_sequence": 0,
            "image_patch_path": "snippets/p0.png", "page_image_path": "pages/p1.png", "bbox": [1, 2, 3, 4],
            "tesseract": {"text": "raw tess"},
            "refined": {"text": "raw tess", "model": "raw-single-engine"},
        },
        {
            # no "refined" either - falls back to whichever raw engine is present
            "book_id": "BK", "page_number": 1, "side": "P", "paragraph_sequence": 1,
            "image_patch_path": "snippets/p1.png", "page_image_path": "pages/p1.png", "bbox": [1, 2, 3, 4],
            "tesseract": {"text": "no refine yet"},
        },
    ]
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    conn = review_db.open_review_db(tmp_path / "review.db")
    inserted = review_db.seed_from_result_json(conn, result_path)
    assert inserted == 2

    rows = {r["paragraph_sequence"]: r for r in conn.execute("SELECT * FROM snippets").fetchall()}
    assert rows[0]["tesseract_text"] == "raw tess" and rows[0]["easyocr_text"] is None and rows[0]["surya_text"] is None
    assert rows[0]["refined_text"] == "raw tess"
    assert rows[1]["refined_text"] == "no refine yet"  # no "refined" key - falls back to the raw tesseract reading
