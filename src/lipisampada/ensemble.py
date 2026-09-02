"""Compares EasyOCR and Tesseract output at word level to flag
disagreements and low-confidence words for later AI/human review."""

from difflib import SequenceMatcher

from lipisampada.ocr_engines import OcrResult

LOW_CONFIDENCE_THRESHOLD = 0.90


def compare(easyocr_result: OcrResult, tesseract_result: OcrResult) -> dict:
    words_a = easyocr_result.text.split()
    words_b = tesseract_result.text.split()

    matcher = SequenceMatcher(None, words_a, words_b)
    disagreement = matcher.ratio() < 1.0

    low_confidence = (
        easyocr_result.avg_confidence < LOW_CONFIDENCE_THRESHOLD
        or tesseract_result.avg_confidence < LOW_CONFIDENCE_THRESHOLD
        or any(w["confidence"] < LOW_CONFIDENCE_THRESHOLD for w in easyocr_result.word_confidences)
        or any(w["confidence"] < LOW_CONFIDENCE_THRESHOLD for w in tesseract_result.word_confidences)
    )

    return {
        "disagreement": disagreement,
        "low_confidence": low_confidence,
        "similarity_ratio": matcher.ratio(),
    }
