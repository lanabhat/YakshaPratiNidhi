from lipisampada import refiner


def test_refine_single_skips_the_llm_call_for_empty_text(monkeypatch):
    """Regression: with no other reading to ground it, asking the LLM to "correct" a lone empty
    reading produced hallucinated output (once, a stray non-Kannada word) instead of recognizing
    there's nothing there - found by re-running the accuracy analysis against real finalized text."""
    monkeypatch.setattr(refiner, "_call_ollama", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert refiner.refine_single("", "tesseract") == ""
    assert refiner.refine_single("   ", "tesseract") == "   "
    assert refiner.refine_single(None, "tesseract") == ""


def test_refine_single_calls_the_llm_for_real_text(monkeypatch):
    calls = []
    monkeypatch.setattr(refiner, "_call_ollama", lambda prompt, model: calls.append(prompt) or "corrected")
    out = refiner.refine_single("ಪಠ್ಯ", "tesseract")
    assert out == "corrected"
    assert len(calls) == 1 and "ಪಠ್ಯ" in calls[0] and "Tesseract" in calls[0]


def test_refine_single_prompt_omits_the_multi_reading_comparison_framing():
    # regression: refine_single must not reuse refine()'s 3-reading template, which would fabricate a
    # false "these readings agree" hint with no second/third reading to actually agree with
    from lipisampada.refiner import _SINGLE_PROMPT_TEMPLATE
    assert "Reading A" not in _SINGLE_PROMPT_TEMPLATE and "Reading B" not in _SINGLE_PROMPT_TEMPLATE
