"""Compares EasyOCR, Tesseract and Surya output to flag disagreements and
low-confidence snippets for later AI/human review.

Deliberately no word-level merge here: only EasyOCR and Tesseract report
per-word confidence (Surya reports one mean-token-probability per
generation), so a confidence-weighted merge would have no signal from the
engine that has looked strongest so far. Choosing between candidates is
left to the Phase 1 LLM refiner, which can use dictionary and poetic
context rather than confidence alone."""

from difflib import SequenceMatcher

from lipisampada.ocr_engines import OcrResult

LOW_CONFIDENCE_THRESHOLD = 0.90


def _similarity(a: OcrResult, b: OcrResult) -> float:
    return SequenceMatcher(None, a.text.split(), b.text.split()).ratio()


def _has_low_confidence(result: OcrResult) -> bool:
    if result.avg_confidence is not None and result.avg_confidence < LOW_CONFIDENCE_THRESHOLD:
        return True
    return any(w["confidence"] < LOW_CONFIDENCE_THRESHOLD for w in result.word_confidences)


def compare(easyocr_result: OcrResult, tesseract_result: OcrResult, surya_result: OcrResult) -> dict:
    texts = [easyocr_result.text, tesseract_result.text, surya_result.text]
    normalized = [" ".join(t.split()) for t in texts]

    # Exact agreement between any two engines is a strong signal that needs
    # no confidence scores, so it survives Surya having none.
    majority_agreement = len(set(normalized)) < len(normalized)

    return {
        "disagreement": len(set(normalized)) > 1,
        "majority_agreement": majority_agreement,
        "low_confidence": any(
            _has_low_confidence(r) for r in (easyocr_result, tesseract_result, surya_result)
        ),
        "surya_suspect": surya_result.suspect,
        "similarity": {
            "easyocr_tesseract": round(_similarity(easyocr_result, tesseract_result), 4),
            "easyocr_surya": round(_similarity(easyocr_result, surya_result), 4),
            "tesseract_surya": round(_similarity(tesseract_result, surya_result), 4),
        },
    }
