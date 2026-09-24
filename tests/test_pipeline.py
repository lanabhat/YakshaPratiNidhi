import json

from lipisampada import pipeline, report


def _touch_images(dir_path, names):
    dir_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        p = dir_path / name
        p.write_bytes(b"not a real image - process_page is monkeypatched")
        paths.append(p)
    return paths


def test_a_failed_page_is_reported_via_on_page_error_but_does_not_stop_the_book(tmp_path, monkeypatch):
    """Regression: PRS00004 ended up STATUS_DONE with only 1 of 55 pages actually OCR'd, because a
    per-page failure here was only ever printed (progress()), never surfaced to the caller."""
    images = _touch_images(tmp_path / "in", ["IMG_20260101_0001_P.tif", "IMG_20260101_0002_P.tif", "IMG_20260101_0003_P.tif"])
    run_dir = tmp_path / "run"

    def fake_process_page(image_path, snippets_dir, pages_dir, book_id, refine, corrections_db, on_snippet=None):
        if "0002" in image_path.name:
            raise RuntimeError("corrupt page")
        return [{"page_number": 1, "side": "P", "paragraph_sequence": 0, "text": "ok",
                 "flags": {"disagreement": False, "low_confidence": False, "surya_suspect": False}}]

    monkeypatch.setattr(pipeline, "process_page", fake_process_page)
    monkeypatch.setattr(report, "generate_report", lambda *a, **k: None)  # not what this test is about

    errors = []
    pipeline.run_book(images, "B1", run_dir, refine=False, progress=lambda m: None,
                       on_page_error=lambda path, e: errors.append((path.name, e)))

    assert [name for name, _ in errors] == ["IMG_20260101_0002_P.tif"]
    assert isinstance(errors[0][1], RuntimeError) and str(errors[0][1]) == "corrupt page"
    # the other two pages still made it into result.json - one bad page doesn't lose the rest
    records = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert len(records) == 2


def test_a_page_with_no_on_page_error_callback_still_just_gets_skipped(tmp_path, monkeypatch):
    images = _touch_images(tmp_path / "in", ["IMG_20260101_0001_P.tif"])
    run_dir = tmp_path / "run"
    monkeypatch.setattr(pipeline, "process_page", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    pipeline.run_book(images, "B1", run_dir, refine=False, progress=lambda m: None)  # on_page_error=None: must not raise

    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8")) == []
