import json
import time

import cv2
import numpy as np
import pytest

from test_page_editor_api import client, env, make_item, quad, region  # noqa: F401  (fixtures + helpers)


def set_status(env, item, status, **cols):
    sets = ", ".join(["status=?"] + [f"{k}=?" for k in cols])
    env[0]._queue_conn.execute(f"UPDATE queue_items SET {sets} WHERE id=?", (status, *cols.values(), item))
    env[0]._queue_conn.commit()


def status_of(client, item):
    return client.get(f"/api/queue/{item}").json()["item"]["status"]


def rec(page, text, side="P"):
    return {"page_number": page, "side": side, "seq": 1, "easyocr": {"text": text}, "refined": {"text": text}}


def test_pause_flags_a_running_book_and_resume_requeues_a_paused_one(client, env):
    item, _, _ = make_item(env, 1, "p_flag")
    set_status(env, item, "ocr_running")
    assert client.post(f"/api/queue/{item}/pause").status_code == 200
    assert item in env[0]._pause_requested
    assert client.post(f"/api/queue/{item}/resume").status_code == 409        # only a paused book resumes
    set_status(env, item, "paused")
    assert client.post(f"/api/queue/{item}/resume").status_code == 200
    assert status_of(client, item) == "approved"                              # the worker picks it up from here
    assert item not in env[0]._pause_requested


def test_pause_is_refused_when_nothing_is_running(client, env):
    item, _, _ = make_item(env, 1, "p_idle")                                  # awaiting_review
    assert client.post(f"/api/queue/{item}/pause").status_code == 409


def test_a_paused_book_is_not_picked_up_by_the_worker_queue(env):
    from lipisampada import intake_queue_db as qdb
    item, _, _ = make_item(env, 1, "p_worker")
    set_status(env, item, "paused")
    nxt = qdb.next_pending(env[0]._queue_conn)
    assert nxt is None or nxt["id"] != item


def test_ocr_stops_at_the_next_snippet_and_lands_in_paused(env, monkeypatch, tmp_path):
    mod = env[0]
    item, _, work = make_item(env, 1, "p_run")
    (work / "approved").mkdir()
    cv2.imwrite(str(work / "approved" / "IMG_20260101_0001_P.tif"), np.full((50, 50, 3), 255, np.uint8))
    set_status(env, item, "approved")
    monkeypatch.setattr(mod, "OUTPUT_ROOT", tmp_path / "out")
    calls = []

    def fake_run_book(paths, book_id, run_dir, refine, on_page, on_snippet, resume, **kw):
        on_snippet(1, 1, 1, 3)
        calls.append("snippet1")
        mod._pause_requested.add(item)          # the user clicks Pause while it works
        on_snippet(1, 1, 2, 3)                  # must raise and stop the run
        calls.append("snippet2-not-reached")

    monkeypatch.setattr(mod.pipeline, "run_book", fake_run_book)
    mod._run_ocr(mod.qdb.get_item(mod._queue_conn, item))
    assert calls == ["snippet1"]
    assert mod.qdb.get_item(mod._queue_conn, item)["status"] == "paused"


def test_a_book_with_a_failed_page_is_marked_failed_not_done(env, monkeypatch, tmp_path):
    """Regression: PRS00004 landed on STATUS_DONE with only 1 of 55 pages actually OCR'd, because a
    per-page OCR failure was only ever printed, never surfaced to _run_ocr_locked."""
    mod = env[0]
    item, _, work = make_item(env, 1, "p_pagefail")
    (work / "approved").mkdir()
    cv2.imwrite(str(work / "approved" / "IMG_20260101_0001_P.tif"), np.full((50, 50, 3), 255, np.uint8))
    set_status(env, item, "approved")
    monkeypatch.setattr(mod, "OUTPUT_ROOT", tmp_path / "out")

    def fake_run_book(paths, book_id, run_dir, refine, on_page, on_snippet, resume, on_page_error=None, **kw):
        on_page_error(paths[0], RuntimeError("bad scan"))
        on_page(1, 1, paths[0])

    monkeypatch.setattr(mod.pipeline, "run_book", fake_run_book)
    mod._run_ocr(mod.qdb.get_item(mod._queue_conn, item))
    row = mod.qdb.get_item(mod._queue_conn, item)
    assert row["status"] == "failed"
    assert "1 of 1 page(s) failed OCR" in row["error"] and "bad scan" in row["error"]


def test_a_book_with_no_failed_pages_still_lands_in_done(env, monkeypatch, tmp_path):
    mod = env[0]
    item, _, work = make_item(env, 1, "p_pageok")
    (work / "approved").mkdir()
    cv2.imwrite(str(work / "approved" / "IMG_20260101_0001_P.tif"), np.full((50, 50, 3), 255, np.uint8))
    set_status(env, item, "approved")
    monkeypatch.setattr(mod, "OUTPUT_ROOT", tmp_path / "out")
    monkeypatch.setattr(mod.pipeline, "run_book", lambda *a, **kw: None)  # on_page_error never called
    mod._run_ocr(mod.qdb.get_item(mod._queue_conn, item))
    assert mod.qdb.get_item(mod._queue_conn, item)["status"] == "done"


def test_retry_works_from_a_done_book_and_resumes_ocr(client, env):
    item, pages, work = make_item(env, 1, "p_donefix")
    env[0]._queue_conn.execute("UPDATE queue_pages SET approved=1 WHERE queue_item_id=?", (item,))
    env[0]._queue_conn.commit()
    set_status(env, item, "done")
    assert client.post(f"/api/queue/{item}/retry").status_code == 200
    assert status_of(client, item) == "approved"  # the worker re-runs OCR (resume=True keeps what succeeded)


def test_retry_is_refused_from_statuses_other_than_failed_or_done(client, env):
    item, _, _ = make_item(env, 1, "p_noretry")  # awaiting_review by default
    assert client.post(f"/api/queue/{item}/retry").status_code == 409


def _paused_book_with_ocr(env, tmp_path, name):
    mod = env[0]
    item, pages, work = make_item(env, 2, name)
    run_dir = tmp_path / f"run_{name}"
    (run_dir / "pages").mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps([rec(1, "old one"), rec(2, "old two")]), encoding="utf-8")
    set_status(env, item, "paused", run_dir=str(run_dir))
    return item, pages, work, run_dir


def wait_reocr(client, item, want="done"):
    for _ in range(100):
        state = client.get(f"/api/queue/{item}/progress").json()["reocr"]
        if state and state["state"] != "running":
            assert state["state"] == want, state
            return state
        time.sleep(0.05)
    raise AssertionError("re-OCR did not finish")


def test_saving_a_page_of_a_paused_book_does_not_restart_the_book(client, env, tmp_path):
    item, pages, work, _ = _paused_book_with_ocr(env, tmp_path, "r_save")
    for p in pages:                              # approve both pages so the "all approved" branch is reached
        assert client.post(f"/api/queue/{item}/pages/{p['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]}).status_code == 200
    assert status_of(client, item) == "paused"


def test_reocr_replaces_only_that_page_and_keeps_the_rest(client, env, monkeypatch, tmp_path):
    mod = env[0]
    item, pages, work, run_dir = _paused_book_with_ocr(env, tmp_path, "r_one")
    for p in pages:
        client.post(f"/api/queue/{item}/pages/{p['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    seen = []

    def fake_process_page(path, snippets_dir, pages_dir, book_id, refine, cdb, on_snippet=None):
        seen.append(path.name)
        (pages_dir / f"{path.stem}.png").write_bytes(b"png")
        return [rec(1, "NEW one")]

    monkeypatch.setattr(mod.pipeline, "process_page", fake_process_page)
    r = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/reocr")
    assert r.status_code == 200
    wait_reocr(client, item)
    texts = {(x["page_number"]): x["refined"]["text"] for x in json.loads((run_dir / "result.json").read_text(encoding="utf-8"))}
    assert texts == {1: "NEW one", 2: "old two"}
    assert len(seen) == 1 and seen[0].endswith(".tif") and "_0001_" in seen[0]
    assert mod._ocr_lock.acquire(blocking=False)   # released afterwards
    mod._ocr_lock.release()


def test_a_failed_reocr_keeps_the_old_text(client, env, monkeypatch, tmp_path):
    mod = env[0]
    item, pages, work, run_dir = _paused_book_with_ocr(env, tmp_path, "r_fail")
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    monkeypatch.setattr(mod.pipeline, "process_page", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama down")))
    assert client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/reocr").status_code == 200
    wait_reocr(client, item, want="error")
    texts = [x["refined"]["text"] for x in json.loads((run_dir / "result.json").read_text(encoding="utf-8"))]
    assert texts == ["old one", "old two"]


def test_reocr_is_refused_while_the_book_is_running_or_another_job_holds_the_gpu(client, env, tmp_path):
    mod = env[0]
    item, pages, work, run_dir = _paused_book_with_ocr(env, tmp_path, "r_busy")
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    set_status(env, item, "ocr_running")
    assert client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/reocr").status_code == 409
    assert client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(0, 0, 500, 500))]}).status_code == 409
    set_status(env, item, "paused")
    assert mod._ocr_lock.acquire(blocking=False)
    try:
        assert client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/reocr").status_code == 409
    finally:
        mod._ocr_lock.release()


def test_reocr_of_a_published_book_queues_it_for_republishing(client, env, monkeypatch, tmp_path):
    mod = env[0]
    item, pages, work, run_dir = _paused_book_with_ocr(env, tmp_path, "r_pub")
    set_status(env, item, "done", local_publish_status="published", web_publish_status="published")
    client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/approve", json={"regions": [region(quad(100, 100, 900, 1300))]})
    monkeypatch.setattr(mod.pipeline, "process_page", lambda p, s, pg, b, r, c, on_snippet=None: [rec(1, "NEW")])
    body = client.post(f"/api/queue/{item}/pages/{pages[0]['id']}/reocr").json()
    assert body["was_published"] is True
    wait_reocr(client, item)
    item_row = client.get(f"/api/queue/{item}").json()["item"]
    assert item_row["local_publish_status"] is None  # eligible for the worker's next auto-pickup
    assert item_row["web_publish_status"] == "requested"  # web is never auto-picked-up from NULL
