"""Wrappers for EasyOCR, Tesseract, and Surya returning a common output shape:
{"text": str, "avg_confidence": float | None, "word_confidences": [...]}."""

import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pytesseract
from PIL import Image

_TESSDATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tessdata")
if os.path.isdir(_TESSDATA_DIR):
    os.environ.setdefault("TESSDATA_PREFIX", _TESSDATA_DIR)

_DEFAULT_TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.isfile(_DEFAULT_TESSERACT_EXE):
    pytesseract.pytesseract.tesseract_cmd = _DEFAULT_TESSERACT_EXE

_LLAMA_SERVER_EXE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools", "llamacpp_cuda", "llama-server.exe"
)
if os.path.isfile(_LLAMA_SERVER_EXE):
    os.environ.setdefault("LLAMA_CPP_BINARY", _LLAMA_SERVER_EXE)

_easyocr_reader = None


@dataclass
class OcrResult:
    text: str
    avg_confidence: Optional[float]
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


_surya_recognition_predictor = None


def _get_surya_recognition_predictor():
    """Uses Surya's llama.cpp backend (GPU-offloaded via the CUDA build in
    tools/llamacpp_cuda/). Its vllm backend requires Docker, which isn't
    installed here and is out of scope for this POV."""
    global _surya_recognition_predictor
    if _surya_recognition_predictor is None:
        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor

        manager = SuryaInferenceManager(method="llamacpp")
        _surya_recognition_predictor = RecognitionPredictor(manager=manager)
    return _surya_recognition_predictor


def run_surya(image: np.ndarray) -> OcrResult:
    """Runs Surya's VLM-based full-page OCR on an image.

    Surya's current (VLM) recognizer doesn't expose per-word or per-block
    confidence through its public API, so avg_confidence is None here and
    ensemble comparisons treat that as "no confidence signal" rather than
    "low confidence"."""
    import re

    predictor = _get_surya_recognition_predictor()
    pil_image = Image.fromarray(image).convert("RGB")
    [page_result] = predictor([pil_image], full_page=True)

    html_parts = [block.html for block in page_result.blocks if not block.skipped]
    full_text = re.sub(r"<[^>]+>", " ", " ".join(html_parts))
    full_text = " ".join(full_text.split())

    return OcrResult(text=full_text, avg_confidence=None, word_confidences=[])


def run_surya_page(page_image: np.ndarray, target_width: int = 800) -> OcrResult:
    """Runs Surya on a whole page, downscaled to target_width first.

    Surya's VLM prefill is far slower than expected even on our modest
    (~1000px-wide) scans, and full-resolution pages either stall on image
    prefill or (when fed small pre-cropped snippets instead) fall into a
    repetition loop. Downscaling to ~800px wide fixed both problems in
    testing: prefill dropped from "still incomplete after 10+ minutes" to
    ~11 seconds, generation ran at 47-138 tok/s, and output was coherent,
    correctly-ordered Kannada text for the whole page."""
    import cv2

    h, w = page_image.shape[:2]
    if w > target_width:
        scale = target_width / w
        page_image = cv2.resize(page_image, (target_width, int(h * scale)), interpolation=cv2.INTER_AREA)
    return run_surya(page_image)


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
