"""Wrappers for EasyOCR, Tesseract, and Surya returning a common output shape:
{"text": str, "avg_confidence": float | None, "word_confidences": [...]}."""

import os
import re
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

# Surya's default ceiling (12288) is sized for whole pages. We feed it single
# paragraph snippets, where the largest observed output was ~1.5k tokens, so
# cap generation well below the default: if a sparse crop still trips the
# decoder's repetition loop, it gets cut off in seconds instead of ~90s.
os.environ.setdefault("SURYA_MAX_TOKENS_FULL_PAGE", "4096")

# Deliberately left at Surya's default (off). SURYA_FULLPAGE_REGEN sounds
# like the right fix for a crop that loops on greedy decoding — it
# re-requests at escalating temperature, up to 6 rounds, when Surya's own
# repeat-loop detector fires. In testing it did resolve the loop the
# first time round 1 fired. But for one specific crop it then kept going
# far past 6 rounds (server task-ID counters in the thousands, a minute
# of continuous activity on a single snippet with no result) — a real
# retry-storm, not the bounded behavior the setting document. Rather than
# depend on that, every snippet gets exactly one capped Surya call
# (SURYA_MAX_TOKENS_FULL_PAGE bounds it to ~30s worst case), and whatever
# comes out — including an occasional loop or blank result — is surfaced
# via _looks_suspect for human/LLM-refiner review instead of retried.

_easyocr_reader = None


@dataclass
class OcrResult:
    text: str
    avg_confidence: Optional[float]
    word_confidences: list[dict] = field(default_factory=list)
    suspect: bool = False


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

# Kannada block + the punctuation/digits our OCR outputs actually use
# (danda marks, verse numbers, Latin fallback digits Tesseract/EasyOCR
# sometimes emit). Anything else counts toward the "wrong script" check.
_KANNADA_RANGE = (0x0C80, 0x0CFF)
_COMMON_CHARS = set("।॥|/Il.,:;-—'\"()[]0123456789")


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


# Largest legitimate snippet output measured in testing (a big multi-line
# paragraph block) was ~650 chars. Our snippets are always single
# paragraphs, never whole pages, so this is a generous but firm ceiling —
# runaway generation is the only way to exceed it.
_MAX_PLAUSIBLE_CHARS = 1200


def _looks_suspect(text: str) -> bool:
    """Flags decoder failures SURYA_FULLPAGE_REGEN's internal retries can
    still miss: looping on one word/phrase, hallucinating output in the
    wrong script (Thai, once, for a Kannada snippet), generating
    thousands of tokens of varied, non-repeating, real-looking Kannada
    that's simply too long to be this snippet's actual content, or
    returning nothing at all (Surya's own "does this predicted block read
    as blank" heuristic dropping a real short line).

    This does not trigger a retry — testing found these failures can be
    deterministic for a given crop (same crop, same result, on a freshly
    started server), so retrying (even via a real process restart) isn't
    reliable and risks unbounded runtime chasing an input that won't
    change its answer. Instead this is surfaced as a flag: ensemble.py
    folds it into the "flagged for review" signal the pipeline already
    has for OCR disagreement, same as a low-confidence EasyOCR/Tesseract
    result would be."""
    import zlib

    if not text:
        return True
    if len(text) > _MAX_PLAUSIBLE_CHARS:
        return True
    # Repetition compresses extremely well regardless of whether it's one
    # word or a multi-word phrase repeating. Threshold is set from real
    # samples: a genuine repetition loop measured 0.197, the most
    # repetitive *legitimate* paragraph (long, verse-structured, naturally
    # compressible Kannada poetry) measured 0.344 — 0.28 sits in the gap
    # between them, off a sample of one bad case so treat it as
    # provisional rather than load-bearing.
    if len(text) > 40:
        compressed = zlib.compress(text.encode("utf-8"), level=9)
        if len(compressed) / len(text.encode("utf-8")) < 0.28:
            return True
    non_kannada = sum(
        1 for ch in text if not ch.isspace() and ch not in _COMMON_CHARS
        and not (_KANNADA_RANGE[0] <= ord(ch) <= _KANNADA_RANGE[1])
    )
    return non_kannada / max(len(text), 1) > 0.3


def run_surya(image: np.ndarray) -> OcrResult:
    """Runs Surya's VLM-based OCR on one snippet crop, exactly once — no
    retry. See the SURYA_FULLPAGE_REGEN comment above for why: a retry
    layer looked easy to add but wasn't reliably bounded in practice.
    Whatever this one call returns is surfaced via _looks_suspect for
    human/LLM-refiner review rather than retried — see its docstring.

    Unlike EasyOCR/Tesseract, Surya reports no per-word confidence: its
    recognizer attaches the generation's mean token probability to every
    block it emits, so avg_confidence here is a whole-snippet signal and
    word_confidences stays empty."""
    predictor = _get_surya_recognition_predictor()
    pil_image = Image.fromarray(image).convert("RGB")
    [page_result] = predictor([pil_image], full_page=True)

    blocks = [block for block in page_result.blocks if not block.skipped]
    full_text = re.sub(r"<[^>]+>", " ", " ".join(block.html for block in blocks))
    full_text = " ".join(full_text.split())

    confidences = [block.confidence for block in blocks if block.confidence is not None]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return OcrResult(
        text=full_text, avg_confidence=avg_conf, word_confidences=[], suspect=_looks_suspect(full_text)
    )


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
