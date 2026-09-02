"""Wrappers for EasyOCR and Tesseract returning a common output shape:
{"text": str, "avg_confidence": float in [0,1], "word_confidences": [...]}."""

import os
from dataclasses import dataclass, field

import numpy as np
import pytesseract

_TESSDATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tessdata")
if os.path.isdir(_TESSDATA_DIR):
    os.environ.setdefault("TESSDATA_PREFIX", _TESSDATA_DIR)

_DEFAULT_TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.isfile(_DEFAULT_TESSERACT_EXE):
    pytesseract.pytesseract.tesseract_cmd = _DEFAULT_TESSERACT_EXE

_easyocr_reader = None


@dataclass
class OcrResult:
    text: str
    avg_confidence: float
    word_confidences: list[dict] = field(default_factory=list)


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr

        _easyocr_reader = easyocr.Reader(["kn", "en"], gpu=False)
    return _easyocr_reader


def run_easyocr(image: np.ndarray) -> OcrResult:
    reader = _get_easyocr_reader()
    results = reader.readtext(image, detail=1, paragraph=False)

    words = []
    for _bbox, text, conf in results:
        words.append({"text": text, "confidence": float(conf)})

    full_text = " ".join(w["text"] for w in words)
    avg_conf = sum(w["confidence"] for w in words) / len(words) if words else 0.0
    return OcrResult(text=full_text, avg_confidence=avg_conf, word_confidences=words)


def run_tesseract(image: np.ndarray, lang: str = "kan") -> OcrResult:
    data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)

    words = []
    for text, conf in zip(data["text"], data["conf"]):
        text = text.strip()
        conf = float(conf)
        if not text or conf < 0:
            continue
        words.append({"text": text, "confidence": conf / 100.0})

    full_text = " ".join(w["text"] for w in words)
    avg_conf = sum(w["confidence"] for w in words) / len(words) if words else 0.0
    return OcrResult(text=full_text, avg_confidence=avg_conf, word_confidences=words)
