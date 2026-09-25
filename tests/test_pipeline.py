import json

import numpy as np

from lipisampada import layout, pipeline, refiner, report
from lipisampada.ocr_engines import OcrResult


def _stub_layout_and_preprocessing(monkeypatch):
    """process_page's own image handling (preprocess_page/slice_page/crop_snippet) isn't what these
    tests are about - stub it to one fixed fake snippet so the tests can focus on which OCR engines
    actually get called."""
    monkeypatch.setattr(pipeline.preprocessing, "preprocess_page", lambda path: (np.zeros((10, 10)), np.zeros((10, 10))))
    monkeypatch.setattr(pipeline.layout, "slice_page", lambda binary: [layout.Snippet(bbox=(0, 0, 5, 5), paragraph_sequence=0, column_index=0)])
    monkeypatch.setattr(pipeline.layout, "crop_snippet", lambda gray, snippet: np.ones((5, 5), dtype=np.uint8))
    monkeypatch.setattr(pipeline.cv2, "imwrite", lambda *a, **k: True)  # no real files to write to


def _counting_engine(name, text="ಪಠ್ಯ"):
    calls = []

    def run(crop):
        calls.append(crop)
        return OcrResult(text=text, avg_confidence=0.9)

    return calls, run


def test_default_engines_only_runs_tesseract_and_skips_the_llm_call(tmp_path, monkeypatch):
    """The default single-engine path deliberately does NOT call refine_single - a real accuracy check
    against human-finalized text found the LLM pass isn't reliably better than Tesseract's raw text (a
    wash on body text, worse on short non-poetic text), so ocr_snippet uses the raw reading as-is."""
    _stub_layout_and_preprocessing(monkeypatch)
    easy_calls, easy_run = _counting_engine("easyocr")
    tess_calls, tess_run = _counting_engine("tesseract", "ಪಠ್ಯ")
    surya_calls, surya_run = _counting_engine("surya")
    monkeypatch.setitem(pipeline._RUN_ENGINE, "easyocr", easy_run)
    monkeypatch.setitem(pipeline._RUN_ENGINE, "tesseract", tess_run)
    monkeypatch.setitem(pipeline._RUN_ENGINE, "surya", surya_run)
    refine_single_calls = []
    refine_calls = []
    monkeypatch.setattr(refiner, "refine_single", lambda text, engine, **k: refine_single_calls.append((text, engine)) or "should not be called")
    monkeypatch.setattr(refiner, "refine", lambda *a, **k: refine_calls.append(a) or "should not be called")

    records = pipeline.process_page(tmp_path / "p.tif", tmp_path / "snip", tmp_path / "pg", "B1", True, None)

    assert len(easy_calls) == 0 and len(surya_calls) == 0 and len(tess_calls) == 1
    assert len(records) == 1
    r = records[0]
    assert "tesseract" in r and "easyocr" not in r and "surya" not in r
    assert r["flags"] == {"single_engine": "tesseract"}
    assert r["refined"] == {"text": "ಪಠ್ಯ", "model": "raw-single-engine"}  # Tesseract's own text, untouched
    assert refine_single_calls == [] and refine_calls == []  # no LLM call made at all


def test_full_ensemble_engines_runs_all_three_and_uses_refine(tmp_path, monkeypatch):
    _stub_layout_and_preprocessing(monkeypatch)
    easy_calls, easy_run = _counting_engine("easyocr", "reading A")
    tess_calls, tess_run = _counting_engine("tesseract", "reading B")
    surya_calls, surya_run = _counting_engine("surya", "reading C")
    monkeypatch.setitem(pipeline._RUN_ENGINE, "easyocr", easy_run)
    monkeypatch.setitem(pipeline._RUN_ENGINE, "tesseract", tess_run)
    monkeypatch.setitem(pipeline._RUN_ENGINE, "surya", surya_run)
    refine_calls = []
    monkeypatch.setattr(refiner, "refine", lambda easy, tess, surya, flags, **k: refine_calls.append((easy, tess, surya)) or "merged")
    monkeypatch.setattr(refiner, "refine_single", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))

    records = pipeline.process_page(tmp_path / "p.tif", tmp_path / "snip", tmp_path / "pg", "B1", True, None,
                                     engines=pipeline.ALL_ENGINES)

    assert len(easy_calls) == 1 and len(tess_calls) == 1 and len(surya_calls) == 1
    r = records[0]
    assert set(r) >= {"easyocr", "tesseract", "surya"}
    assert "single_engine" not in r["flags"] and "disagreement" in r["flags"]  # real compare() ran
    assert r["refined"]["text"] == "merged"
    assert refine_calls == [("reading A", "reading B", "reading C")]


def test_reocr_snippet_reruns_full_ensemble_on_the_saved_crop(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    (run_dir / "snippets").mkdir(parents=True)
    crop_path = run_dir / "snippets" / "p0.png"
    import cv2
    cv2.imwrite(str(crop_path), np.ones((5, 5), dtype=np.uint8) * 255)  # a real, readable crop this time

    easy_calls, easy_run = _counting_engine("easyocr", "A")
    tess_calls, tess_run = _counting_engine("tesseract", "B")
    surya_calls, surya_run = _counting_engine("surya", "C")
    monkeypatch.setitem(pipeline._RUN_ENGINE, "easyocr", easy_run)
    monkeypatch.setitem(pipeline._RUN_ENGINE, "tesseract", tess_run)
    monkeypatch.setitem(pipeline._RUN_ENGINE, "surya", surya_run)
    monkeypatch.setattr(refiner, "refine", lambda easy, tess, surya, flags, **k: "merged")

    old_record = {
        "book_id": "B1", "page_number": 3, "side": "P", "paragraph_sequence": 0, "column_index": 0,
        "bbox": [1, 2, 3, 4], "image_patch_path": "snippets/p0.png", "page_image_path": "pages/p3.png",
        "tesseract": {"text": "old tesseract only", "avg_confidence": 0.5},
        "flags": {"single_engine": "tesseract"}, "refined": {"text": "old tesseract only", "model": "qwen2.5:7b-instruct"},
    }

    new_record = pipeline.reocr_snippet(run_dir, old_record, None)

    assert len(easy_calls) == 1 and len(tess_calls) == 1 and len(surya_calls) == 1  # fresh, not reused
    assert set(new_record) >= {"easyocr", "tesseract", "surya"}
    assert new_record["refined"]["text"] == "merged"
    # metadata carried over unchanged from the old record
    assert new_record["page_number"] == 3 and new_record["paragraph_sequence"] == 0 and new_record["bbox"] == [1, 2, 3, 4]


def test_reocr_snippet_raises_clearly_when_the_saved_crop_is_missing(tmp_path):
    record = {"book_id": "B1", "page_number": 1, "side": "P", "paragraph_sequence": 0, "column_index": 0,
              "bbox": [0, 0, 1, 1], "image_patch_path": "snippets/missing.png", "page_image_path": "pages/p1.png"}
    try:
        pipeline.reocr_snippet(tmp_path / "run", record, None)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "missing.png" in str(e)


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
