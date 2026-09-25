import gzip
import json

import pytest
from PIL import Image

from lipisampada import publisher
from lipisampada.reviewapi.app import create_app
from lipisampada.reviewapi.storage import LocalStorage


def make_run(tmp_path):
    run = tmp_path / "run"
    (run / "pages").mkdir(parents=True)
    (run / "snippets").mkdir()
    records = []
    for page in (1, 2):
        Image.new("L", (2400, 3400), 200).save(run / "pages" / f"IMG_20260101_{page:04d}_P.png")
        for seq in range(2):
            Image.new("L", (600, 80), 255).save(run / "snippets" / f"IMG_20260101_{page:04d}_P_p{seq}.png")
            records.append({
                "book_id": "BK", "page_number": page, "side": "P", "paragraph_sequence": seq, "column_index": 0,
                "bbox": [1, 2, 3, 4], "image_patch_path": f"snippets/IMG_20260101_{page:04d}_P_p{seq}.png",
                "page_image_path": f"pages/IMG_20260101_{page:04d}_P.png",
                "easyocr": {"text": "e"}, "tesseract": {"text": "t"}, "surya": {"text": "s"},
                "flags": {"refine_failed": "timed out"} if (page, seq) == (2, 1) else {},
                "refined": {"text": f"ai {page}.{seq}", "model": "m"},
            })
    (run / "result.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return run


def make_single_engine_run(tmp_path):
    """Today's default pipeline output: only Tesseract ran (see pipeline.ocr_snippet's `engines`) -
    no "easyocr"/"surya" keys at all, unlike make_run()'s always-all-three fake records."""
    run = tmp_path / "run_single"
    (run / "pages").mkdir(parents=True)
    (run / "snippets").mkdir()
    Image.new("L", (2400, 3400), 200).save(run / "pages" / "IMG_20260101_0001_P.png")
    Image.new("L", (600, 80), 255).save(run / "snippets" / "IMG_20260101_0001_P_p0.png")
    Image.new("L", (600, 80), 255).save(run / "snippets" / "IMG_20260101_0001_P_p1.png")
    records = [
        {
            "book_id": "BK", "page_number": 1, "side": "P", "paragraph_sequence": 0, "column_index": 0,
            "bbox": [1, 2, 3, 4], "image_patch_path": "snippets/IMG_20260101_0001_P_p0.png",
            "page_image_path": "pages/IMG_20260101_0001_P.png",
            "tesseract": {"text": "raw tess"}, "flags": {"single_engine": "tesseract"},
            "refined": {"text": "raw tess", "model": "raw-single-engine"},
        },
        {
            # no "refined" at all (refine=False path) - ai_text must still fall back to the raw reading
            "book_id": "BK", "page_number": 1, "side": "P", "paragraph_sequence": 1, "column_index": 0,
            "bbox": [1, 2, 3, 4], "image_patch_path": "snippets/IMG_20260101_0001_P_p1.png",
            "page_image_path": "pages/IMG_20260101_0001_P.png",
            "tesseract": {"text": "no refine yet"}, "flags": {"single_engine": "tesseract"},
        },
    ]
    (run / "result.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return run


def test_publish_book_handles_single_engine_records(tmp_path):
    """Regression: publish_book unconditionally read r["easyocr"]["text"], which crashed
    (KeyError: 'easyocr') for the now-default Tesseract-only records - this happened during
    _run_publish, after OCR itself had already completed successfully."""
    run = make_single_engine_run(tmp_path)
    storage = LocalStorage(tmp_path / "files", "http://api")
    posted = {}

    def fake_post(path, payload):
        posted[path] = payload
        return {"ok": True}

    res = publisher.publish_book(run, "BK", storage, fake_post, progress=lambda m: None)
    assert res["ok"] is True

    bundle = json.loads(gzip.decompress(storage.path_for("books/BK/bundle.json.gz").read_bytes()))
    assert bundle["BK:1:P:0"] == {"tesseract": "raw tess", "ai_text": "raw tess", "bbox": [1, 2, 3, 4], "flags": {"single_engine": "tesseract"}}
    assert "easyocr" not in bundle["BK:1:P:0"] and "surya" not in bundle["BK:1:P:0"]

    snippets = {(s["page_number"], s["seq"]): s for s in posted["/api/ingest/book"]["snippets"]}
    assert snippets[(1, 0)]["ai_text"] == "raw tess"
    assert snippets[(1, 1)]["ai_text"] == "no refine yet"  # no "refined" key - falls back to the raw tesseract reading


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("INGEST_API_KEY", "k")
    monkeypatch.setenv("REVIEW_API_DATA", str(tmp_path / "data"))
    app = create_app(tmp_path / "t.sqlite3", tmp_path / "files")
    client = app.test_client()

    def post(path, payload):
        r = client.post(path, json=payload, headers={"X-Ingest-Key": "k"})
        assert r.status_code == 200, r.text
        return r.json

    return client, LocalStorage(tmp_path / "files", "http://api"), post, make_run(tmp_path)


def test_publish_uploads_downscaled_images_bundle_and_ingests(env, tmp_path):
    client, storage, post, run = env
    res = publisher.publish_book(run, "BK", storage, post, title="Test Book", progress=lambda m: None)
    assert res["created"] == 4 and res["pages"] == 2 and res["images_uploaded"] == 6 and res["images_skipped"] == 0

    page_img = Image.open(storage.path_for("books/BK/pages/IMG_20260101_0001_P.webp"))
    assert page_img.width == publisher.PAGE_MAX_WIDTH and page_img.format == "WEBP"
    snip = Image.open(storage.path_for("books/BK/snippets/IMG_20260101_0001_P_p0.webp"))
    assert snip.width == 600  # small crops are not upscaled or shrunk

    bundle = json.loads(gzip.decompress(storage.path_for("books/BK/bundle.json.gz").read_bytes()))
    assert bundle["BK:1:P:0"]["easyocr"] == "e" and bundle["BK:1:P:0"]["bbox"] == [1, 2, 3, 4]

    b = client.get("/api/library").json["books"][0]
    assert b["title"] == "Test Book" and b["total"] == 4 and b["bundle_url"].endswith("bundle.json.gz")
    s = client.get("/api/snippet?id=BK:2:P:1").json
    assert s["refine_failed"] == 1 and s["ai_text"] == "ai 2.1"
    assert s["page_image_url"] == "http://api/files/books/BK/pages/IMG_20260101_0002_P.webp"


def test_republish_skips_unchanged_images_and_keeps_human_work(env):
    client, storage, post, run = env
    publisher.publish_book(run, "BK", storage, post, progress=lambda m: None)
    client.get("/api/me", headers={"Authorization": "Bearer dev:ed@x.org"})
    client.db = client.application.config["DB"]
    client.db.conn.execute("UPDATE users SET role='editor', application_status='approved' WHERE uid='dev:ed@x.org'")
    client.db.conn.commit()
    client.post("/api/finalize", headers={"Authorization": "Bearer dev:ed@x.org"}, json={"snippet_id": "BK:1:P:0", "text": "human"})
    res = publisher.publish_book(run, "BK", storage, post, progress=lambda m: None)
    assert res["images_uploaded"] == 0 and res["images_skipped"] == 6 and res["created"] == 0
    assert client.get("/api/snippet?id=BK:1:P:0").json["final_text"] == "human"


def test_state_is_per_storage_target(env, tmp_path):
    client, storage, post, run = env
    publisher.publish_book(run, "BK", storage, post, progress=lambda m: None)
    other = LocalStorage(tmp_path / "other", "http://elsewhere")
    res = publisher.publish_book(run, "BK", other, post, progress=lambda m: None)
    assert res["images_uploaded"] == 6 and res["images_skipped"] == 0


def test_served_images_have_correct_mime_type(env):
    client, storage, post, run = env
    publisher.publish_book(run, "BK", storage, post, progress=lambda m: None)
    r = client.get("/files/books/BK/pages/IMG_20260101_0001_P.webp")
    assert r.status_code == 200 and r.mimetype == "image/webp"
    assert client.get("/files/books/BK/bundle.json.gz").mimetype == "application/gzip"
    assert client.get("/files/../secret").status_code == 404
