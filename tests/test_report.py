from lipisampada import report


def _single_engine_record(**overrides):
    r = {
        "page_number": 1, "side": "P", "paragraph_sequence": 0,
        "image_patch_path": "snippets/p0.png",
        "tesseract": {"text": "ಪಠ್ಯ", "avg_confidence": 0.8},
        "flags": {"single_engine": "tesseract"},
        "refined": {"text": "ಪಠ್ಯ", "model": "raw-single-engine"},
    }
    r.update(overrides)
    return r


def _full_ensemble_record(**overrides):
    r = {
        "page_number": 1, "side": "P", "paragraph_sequence": 0,
        "image_patch_path": "snippets/p0.png",
        "easyocr": {"text": "A", "avg_confidence": 0.8},
        "tesseract": {"text": "B", "avg_confidence": 0.7},
        "surya": {"text": "C", "avg_confidence": 0.9},
        "flags": {"disagreement": True, "majority_agreement": False, "low_confidence": False, "surya_suspect": False},
        "refined": {"text": "merged", "model": "qwen2.5:7b-instruct"},
    }
    r.update(overrides)
    return r


def test_generate_report_does_not_crash_on_single_engine_records(tmp_path):
    """Regression: generate_report unconditionally indexed flags["disagreement"]/["majority_agreement"]/
    etc and easyocr/tesseract/surya keys, which no longer all exist for a single-engine snippet (today's
    default) - this crashed run_book AFTER a book's OCR had already succeeded and been written to
    result.json, marking the whole book "failed" with error message "'disagreement'"."""
    out = tmp_path / "report.html"
    report.generate_report([_single_engine_record()], tmp_path, out)
    html = out.read_text(encoding="utf-8")
    assert "ಪಠ್ಯ" in html
    assert "Tesseract" in html
    assert "EasyOCR" not in html  # wasn't run - must not claim it was


def test_generate_report_still_handles_full_ensemble_records(tmp_path):
    out = tmp_path / "report.html"
    report.generate_report([_full_ensemble_record()], tmp_path, out)
    html = out.read_text(encoding="utf-8")
    assert "merged" in html and "EasyOCR" in html and "Surya" in html


def test_generate_report_handles_a_mix_of_both_in_one_run(tmp_path):
    out = tmp_path / "report.html"
    report.generate_report([_single_engine_record(), _full_ensemble_record(paragraph_sequence=1)], tmp_path, out)
    html = out.read_text(encoding="utf-8")
    assert "2 paragraph snippets" in html


def test_generate_report_handles_a_single_engine_record_with_no_refined_yet(tmp_path):
    # refine=False path (pipeline.run_book(..., refine=False)) never adds a "refined" key at all
    out = tmp_path / "report.html"
    r = _single_engine_record()
    del r["refined"]
    report.generate_report([r], tmp_path, out)
    assert out.exists()
