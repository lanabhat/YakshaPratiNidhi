"""Phase 1 AI Refiner: asks a local Ollama LLM to pick/correct the best
Kannada text from the three OCR engines' candidates for one snippet.

Deliberately not a confidence-weighted merge (see ensemble.py's module
docstring for why) — the LLM judges the actual text using Kannada
vocabulary and Yakshagana poetic convention, the same judgement call a
human reviewer would make."""

import json
import sqlite3
import urllib.request
from pathlib import Path

OLLAMA_MODEL = "qwen2.5:7b-instruct"
OLLAMA_HOST = "http://localhost:11434"

_PROMPT_TEMPLATE = """You are an expert in Kannada Yakshagana Prasanga texts (traditional verse-drama scripts).

Below are three OCR readings of the same line or paragraph from a scanned Yakshagana Prasanga book. The readings may contain character-recognition errors, especially: character swaps (e.g. ವ/ಪ, ಳ/ಲ), missing/extra spacing, and OCR misreadings of the verse-end punctuation ।।/॥ (danda marks) as Latin letters or digits like I, II, Il, 1, 2.

Reading A (EasyOCR): {easy}
Reading B (Tesseract): {tess}
Reading C (Surya): {surya}
{hint}
Task: produce the single best-corrected Kannada Unicode text for this line, using your knowledge of Kannada vocabulary, grammar, and Yakshagana poetic conventions (meter/raga names like ವಾರ್ಧಿಕ, ಸೌರಾಷ್ಟ್ರ, ಭಾವಿನಿ, verse-end markers ॥ with Kannada numerals). Prefer whichever reading (or combination of fragments across readings) is most linguistically plausible. Do not invent content not suggested by at least one reading. Normalize OCR-mangled verse-end markers (I, II, Il, digits, or | standing in for ॥) to proper ॥ marks with Kannada numerals when the pattern is clear, while preserving single । marks already present at line breaks within a verse.

Respond with ONLY the corrected Kannada text on a single line, nothing else — no explanation, no quotes."""


def _build_hint(flags: dict) -> str:
    lines = []
    if flags.get("majority_agreement"):
        lines.append(
            "Note: at least two of the three readings above already agree exactly on this "
            "text. Treat that agreement as strong evidence it's correct — only deviate from "
            "it if it is clearly not a valid Kannada word or phrase."
        )
    if flags.get("surya_suspect"):
        lines.append(
            "Note: Reading C was flagged by an automated quality check as possibly corrupted "
            "(e.g. a repeated word/phrase loop, or a hallucinated block) — weigh it less than "
            "Readings A and B unless it clearly fills a gap they leave."
        )
    return ("\n" + "\n".join(lines) + "\n") if lines else "\n"


def refine(easy_text: str, tess_text: str, surya_text: str, flags: dict, model: str = OLLAMA_MODEL) -> str:
    prompt = _PROMPT_TEMPLATE.format(
        easy=easy_text or "(empty)",
        tess=tess_text or "(empty)",
        surya=surya_text or "(empty)",
        hint=_build_hint(flags),
    )
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.1}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["response"].strip()


def open_corrections_db(db_path: Path) -> sqlite3.Connection:
    """Self-correction dictionary: every LLM refinement, logged per snippet.
    Seed of the "gold standard" store described in the original brief —
    word-level frequency mining off this table is a follow-up increment,
    not built here."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            side TEXT NOT NULL,
            paragraph_sequence INTEGER NOT NULL,
            easyocr_text TEXT,
            tesseract_text TEXT,
            surya_text TEXT,
            refined_text TEXT,
            model TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def log_correction(conn: sqlite3.Connection, record: dict, refined_text: str, model: str = OLLAMA_MODEL) -> None:
    conn.execute(
        """
        INSERT INTO corrections
            (book_id, page_number, side, paragraph_sequence, easyocr_text, tesseract_text, surya_text, refined_text, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["book_id"],
            record["page_number"],
            record["side"],
            record["paragraph_sequence"],
            record["easyocr"]["text"],
            record["tesseract"]["text"],
            record["surya"]["text"],
            refined_text,
            model,
        ),
    )
    conn.commit()
