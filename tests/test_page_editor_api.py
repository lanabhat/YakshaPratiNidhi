import importlib
import json
import os
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from lipisampada import intake_queue_db as qdb
from lipisampada import page_ops


def text_page(w=1000, h=1400):
    img = np.full((h, w, 3), 245, np.uint8)
    for y in range(200, h - 200, 60):
        for x in range(150, w - 150, 130):
            cv2.rectangle(img, (x, y), (x + 100, y + 18), (20, 20, 20), -1)
    return img


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("editor")
    catalog = tmp / "catalog.sqlite3"
    c = sqlite3.connect(catalog)
    c.executescript(
        "CREATE TABLE catalog_kavi (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE catalog_prasanga (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE catalog_publisher (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE catalog_catalogentry (id INTEGER PRIMARY KEY, entry_id TEXT, kosha_link TEXT, prati_link TEXT,"
        " publish_date_kannada TEXT, publish_date_english TEXT, view_count INTEGER, kavi_id INTEGER, prasanga_id INTEGER, publisher_id INTEGER);"
    )
    c.commit()
    c.close()
    old = {k: os.environ.get(k) for k in ("INTAKE_CATALOG_DB", "INTAKE_QUEUE_DB", "INTAKE_NO_WORKER")}
    os.environ.update(INTAKE_CATALOG_DB=str(catalog), INTAKE_QUEUE_DB=str(tmp / "queue.sqlite3"), INTAKE_NO_WORKER="1")
    from lipisampada import intake_app

    mod = importlib.reload(intake_app)
    yield mod, tmp
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    return TestClient(env[0].app)


def make_item(env, n_pages=2, name="book"):
    """A queue item 'awaiting review' with n real page images on disk and auto suggestions stored."""
    mod, tmp = env
    work = tmp / name
    (work / "raster").mkdir(parents=True)
    conn = mod._queue_conn
    cur = conn.execute(
        "INSERT INTO queue_items (catalog_entry_id, entry_code, book_id, title, prati_link, status, work_dir, page_count) "
        "VALUES (1, ?, ?, 't', 'x', 'awaiting_review', ?, ?)", (name, name, str(work), n_pages))
    item_id = cur.lastrowid
    for i in range(n_pages):
        p = work / "raster" / f"raw_{i:04d}.png"
        cv2.imwrite(str(p), text_page())
        regions = page_ops.auto_suggest(cv2.imread(str(p)))
        conn.execute(
            "INSERT INTO queue_pages (queue_item_id, pdf_page_index, raster_path, original_path, auto_regions, is_likely_spread) VALUES (?,?,?,?,?,0)",
            (item_id, i, str(p), str(p), json.dumps(regions)))
    conn.commit()
    pages = [dict(r) for r in conn.execute("SELECT * FROM queue_pages WHERE queue_item_id=? ORDER BY pdf_page_index", (item_id,))]
    return item_id, pages, work


def quad(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def region(q, **enh):
    return {"quad": q, "mids": None, "enhance": {**page_ops.DEFAULT_ENHANCE, **enh}, "dewarp": "none"}


def approved_files(work):
    return sorted(p.name.split("_", 2)[2] for p in (work / "approved").glob("*.tif")) if (work / "approved").exists() else []


def imread_gray(path):
    return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)


# ------------------------------------------------------------------ migration
def test_old_queue_database_gains_the_new_page_columns(tmp_path):
    db = tmp_path / "old.sqlite3"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE queue_pages (id INTEGER PRIMARY KEY AUTOINCREMENT, queue_item_id INTEGER NOT NULL, pdf_page_index INTEGER NOT NULL, "
              "raster_path TEXT NOT NULL, is_likely_spread INTEGER NOT NULL DEFAULT 0, crop_box TEXT, rotation REAL NOT NULL DEFAULT 0, split INTEGER NOT NULL DEFAULT 0, "
              "split_x REAL, approved INTEGER NOT NULL DEFAULT 0, final_paths TEXT, UNIQUE(queue_item_id, pdf_page_index))")
    c.execute("INSERT INTO queue_pages (queue_item_id, pdf_page_index, raster_path) VALUES (1, 0, 'x.png')")
    c.commit()
    c.close()
    conn = qdb.open_queue_db(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(queue_pages)")}
    assert {"original_path", "auto_regions", "regions", "rot90"} <= cols
    assert conn.execute("SELECT rot90, original_path FROM queue_pages").fetchone()["rot90"] == 0  # old row survives


# ------------------------------------------------------------------ auto-crop + preview
def test_auto_crop_endpoint_returns_a_suggestion(client, env):
    item, pages, _ = make_item(env, 1, "b_auto")
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/auto-crop", json={"rot90": 0})
    assert r.status_code == 200 and len(r.json()["regions"]) == 1 and len(r.json()["regions"][0]["quad"]) == 4


def test_preview_returns_an_image_and_reacts_to_the_crop(client, env):
    item, pages, _ = make_item(env, 1, "b_prev")
    url = f"/api/queue/{item}/pages/{pages[0]['id']}/preview"
    big = client.post(url, json={"regions": [region(quad(100, 100, 900, 1300))], "active": 0})
    small = client.post(url, json={"regions": [region(quad(100, 100, 500, 700))], "active": 0})
    assert big.status_code == 200 and big.headers["content-type"] == "image/jpeg"
    a = cv2.imdecode(np.frombuffer(big.content, np.uint8), 1)
    b = cv2.imdecode(np.frombuffer(small.content, np.uint8), 1)
    assert a.shape[1] > b.shape[1] and a.shape[0] > b.shape[0]


def test_preview_binarize_is_png_with_only_black_and_white(client, env):
    item, pages, _ = make_item(env, 1, "b_bin")
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/preview",
                    json={"regions": [region(quad(100, 100, 900, 1300), binarize="global", threshold=128)], "active": 0})
    assert r.headers["content-type"] == "image/png"
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), 0)
    assert set(np.unique(img)) <= {0, 255}


def test_preview_curve_handles_change_the_output(client, env):
    item, pages, _ = make_item(env, 1, "b_curve")
    url = f"/api/queue/{item}/pages/{pages[0]['id']}/preview"
    straight = region(quad(100, 100, 900, 1300))
    curved = {**straight, "mids": [[0, 40], [0, 0], [0, 40], [0, 0]]}
    a = cv2.imdecode(np.frombuffer(client.post(url, json={"regions": [straight], "active": 0}).content, np.uint8), 1)
    b = cv2.imdecode(np.frombuffer(client.post(url, json={"regions": [curved], "active": 0}).content, np.uint8), 1)
    assert a.shape[1] != b.shape[1] or np.abs(a.astype(int) - b.astype(int)).mean() > 1


def test_preview_rejects_bad_input(client, env):
    item, pages, _ = make_item(env, 1, "b_bad")
    url = f"/api/queue/{item}/pages/{pages[0]['id']}/preview"
    assert client.post(url, json={"regions": [], "active": 0}).status_code == 400
    assert client.post(url, json={"regions": [region(quad(0, 0, 500, 500))], "active": 3}).status_code == 400
    assert client.post(url, json={"regions": [region(quad(0, 0, 500, 500), binarize="magic")], "active": 0}).status_code == 400
    assert client.post(f"/api/queue/{item}/pages/99999/preview", json={"regions": [region(quad(0, 0, 500, 500))]}).status_code == 404


# ------------------------------------------------------------------ approve / naming
def test_one_crop_writes_a_single_P_page(client, env):
    item, pages, work = make_item(env, 2, "b_one")
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(100, 150, 900, 1250))]})
    assert r.status_code == 200 and r.json() == {"all_pages_approved": False, "files": 1}
    assert approved_files(work) == ["0001_P.tif"]
    out = cv2.imread(str(next((work / "approved").glob("*.tif"))))
    assert abs(out.shape[1] - 800) <= 2 and abs(out.shape[0] - 1100) <= 2


def test_two_crops_are_L_and_R_and_three_are_A_B_C_in_the_order_given(client, env):
    item, pages, work = make_item(env, 2, "b_multi")
    two = [region(quad(50, 100, 480, 1300)), region(quad(520, 100, 950, 1300))]
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": two})
    three = [region(quad(50, 100, 300, 1300)), region(quad(350, 100, 650, 1300)), region(quad(700, 100, 950, 1300))]
    client.post(f"/api/queue/{item}/pages/{pages[1]['id']}/approve", json={"regions": three})
    assert approved_files(work) == ["0001_L.tif", "0001_R.tif", "0002_A.tif", "0002_B.tif", "0002_C.tif"]
    left, right = (imread_gray(work / "approved" / f"IMG_{__import__('datetime').date.today():%Y%m%d}_0001_{s}.tif") for s in "LR")
    assert left.shape[1] == pytest.approx(430, abs=2) and right.shape[1] == pytest.approx(430, abs=2)


def test_reapproving_with_fewer_crops_removes_the_old_files(client, env):
    item, pages, work = make_item(env, 2, "b_redo")
    url = f"/api/queue/{item}/pages/{pages[0]['id']}/approve"
    client.post(url, json={"regions": [region(quad(50, 100, 480, 1300)), region(quad(520, 100, 950, 1300))]})
    assert approved_files(work) == ["0001_L.tif", "0001_R.tif"]
    client.post(url, json={"regions": [region(quad(50, 100, 950, 1300))]})
    assert approved_files(work) == ["0001_P.tif"]


def test_rot90_rotates_the_page_before_the_crop(client, env):
    item, pages, work = make_item(env, 2, "b_rot")
    # rotated 90 deg the page is 1400 wide x 1000 tall
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"rot90": 90, "regions": [region(quad(0, 0, 1399, 999))]})
    out = cv2.imread(str(next((work / "approved").glob("*.tif"))))
    assert abs(out.shape[1] - 1399) <= 2 and abs(out.shape[0] - 999) <= 2


def test_enhancement_is_baked_into_the_saved_page(client, env):
    item, pages, work = make_item(env, 2, "b_enh")
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve",
                json={"regions": [region(quad(100, 100, 900, 1300), binarize="global", threshold=128)]})
    out = imread_gray(next((work / "approved").glob("*.tif")))
    assert set(np.unique(out)) <= {0, 255} and (out == 0).any() and (out == 255).any()


def test_approve_validates_and_requires_awaiting_review(client, env):
    item, pages, work = make_item(env, 2, "b_val")
    url = f"/api/queue/{item}/pages/{pages[0]['id']}/approve"
    assert client.post(url, json={"regions": [{"quad": [[0, 0], [1, 1]]}]}).status_code == 400
    assert client.post(url, json={"regions": [region(quad(0, 0, 500, 500))] * 9}).status_code == 400
    assert approved_files(work) == []                                   # nothing written on a rejected request
    env[0]._queue_conn.execute("UPDATE queue_items SET status='queued' WHERE id=?", (item,))
    env[0]._queue_conn.commit()
    assert client.post(url, json={"regions": [region(quad(0, 0, 500, 500))]}).status_code == 409


def test_approving_every_page_moves_the_item_on_and_counts_reviewed(client, env):
    item, pages, work = make_item(env, 2, "b_all")
    r1 = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    assert r1.json()["all_pages_approved"] is False
    assert client.get(f"/api/queue/{item}").json()["item"]["reviewed_page_count"] == 1
    r2 = client.post(f"/api/queue/{item}/pages/{pages[1]['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    assert r2.json()["all_pages_approved"] is True
    body = client.get(f"/api/queue/{item}").json()
    assert body["item"]["status"] == "approved" and body["item"]["reviewed_page_count"] == 2


def test_legacy_single_box_payload_still_works(client, env):
    item, pages, work = make_item(env, 2, "b_legacy")
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve",
                    json={"crop_box": [100, 100, 900, 1300], "rotation": 0, "split": True, "split_x": 500})
    assert r.status_code == 200 and approved_files(work) == ["0001_L.tif", "0001_R.tif"]


def test_accept_all_remaining_uses_the_auto_suggestions(client, env):
    item, pages, work = make_item(env, 3, "b_acc")
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    r = client.post(f"/api/queue/{item}/accept-auto")
    assert r.status_code == 200 and r.json() == {"approved": 2, "all_pages_approved": True}
    assert approved_files(work) == ["0001_P.tif", "0002_P.tif", "0003_P.tif"]
    assert client.get(f"/api/queue/{item}").json()["item"]["status"] == "approved"
    assert client.post(f"/api/queue/{item}/accept-auto").status_code == 409     # no longer awaiting review


def test_original_page_image_is_never_modified(client, env):
    item, pages, work = make_item(env, 2, "b_orig")
    before = Path(pages[0]["raster_path"]).read_bytes()
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(300, 300, 600, 600))]})
    assert Path(pages[0]["raster_path"]).read_bytes() == before


# ------------------------------------------------------------------ auto dewarp
def test_auto_dewarp_reports_a_clear_error_when_unavailable(client, env, monkeypatch):
    item, pages, _ = make_item(env, 1, "b_dw_err")

    def boom(_img):
        raise RuntimeError("automatic dewarp needs the optional package: pip install page-dewarp")

    monkeypatch.setattr(page_ops, "auto_dewarp", boom)
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/auto-dewarp", json={"regions": [region(quad(100, 100, 900, 1300))], "index": 0})
    assert r.status_code == 422 and "page-dewarp" in r.json()["detail"]


def test_auto_dewarp_result_is_cached_used_and_invalidated_by_edits(client, env, monkeypatch):
    item, pages, work = make_item(env, 2, "b_dw")
    marker = np.full((300, 200, 3), 77, np.uint8)  # recognizable stand-in for a dewarped crop
    monkeypatch.setattr(page_ops, "auto_dewarp", lambda flat: marker.copy())
    q = quad(100, 100, 900, 1300)
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/auto-dewarp", json={"regions": [region(q)], "index": 0})
    assert r.status_code == 200 and r.json()["size"] == [200, 300]

    dewarped = {**region(q), "dewarp": "auto"}
    prev = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/preview", json={"regions": [dewarped], "active": 0})
    assert prev.headers["x-dewarp"] == "cached"
    moved = {**region(quad(100, 100, 899, 1300)), "dewarp": "auto"}          # crop edited afterwards -> stale
    assert client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/preview", json={"regions": [moved], "active": 0}).headers["x-dewarp"] == "missing"

    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [dewarped]})
    out = cv2.imread(str(next((work / "approved").glob("*.tif"))))
    assert out.shape[:2] == (300, 200) and abs(int(out.mean()) - 77) <= 2       # the cached dewarp is what got saved
    client.post(f"/api/queue/{item}/pages/{pages[1]['id']}/approve", json={"regions": [moved]})
    second = cv2.imread(str(sorted((work / "approved").glob("*.tif"))[1]))
    assert second.shape[:2] != (300, 200)                                         # stale dewarp is never silently used
