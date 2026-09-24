"""Phase 1 AI Refiner: asks a local Ollama LLM to pick/correct the best
Kannada text from the three OCR engines' candidates for one snippet.

Deliberately not a confidence-weighted merge (see ensemble.py's module
docstring for why) — the LLM judges the actual text using Kannada
vocabulary and Yakshagana poetic convention, the same judgement call a
human reviewer would make."""

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

OLLAMA_MODEL = "qwen2.5:7b-instruct"
OLLAMA_HOST = "http://localhost:11434"
REFINE_TIMEOUT_SECONDS = 180
REFINE_RETRIES = 2  # a cold model load can genuinely exceed one timeout window

_PROMPT_TEMPLATE = """You are an expert in Kannada Yakshagana Prasanga texts (traditional verse-drama scripts).

Below are three OCR readings of the same line or paragraph from a scanned Yakshagana Prasanga book. The readings may contain character-recognition errors, especially: character swaps (e.g. ವ/ಪ, ಳ/ಲ), missing/extra spacing, and OCR misreadings of the verse-end punctuation ।।/॥ (danda marks) as Latin letters or digits like I, II, Il, 1, 2.

Reading A (EasyOCR): {easy}
Reading B (Tesseract): {tess}
Reading C (Surya): {surya}
{hint}
Task: produce the single best-corrected Kannada Unicode text for this line, using your knowledge of Kannada vocabulary, grammar, and Yakshagana poetic conventions (meter/raga names like ವಾರ್ಧಿಕ, ಸೌರಾಷ್ಟ್ರ, ಭಾವಿನಿ, verse-end markers ॥ with Kannada numerals). Prefer whichever reading (or combination of fragments across readings) is most linguistically plausible. Do not invent content not suggested by at least one reading.

Be conservative: your job is to pick the correct reading among what's already there, not to rewrite freely. If you are not confident a word is wrong, leave it as most readings have it. Do not "fix" a word into a different valid word just because it looks slightly more natural — an unnecessary change is as bad as leaving a real error uncorrected.

Punctuation: keep every existing ।/॥ danda mark unless you are certain it is a stray OCR artifact — when in doubt, keep it rather than remove it. Only normalize OCR-mangled verse-end markers (I, II, Il, digits, or | standing in for ॥) into proper ॥ marks with Kannada numerals when the Latin-substitute pattern is clear; do not touch danda marks that are already correct.

Respond with ONLY the corrected Kannada text on a single line, nothing else — no explanation, no quotes."""


def _consensus_words(easy_text: str, tess_text: str, surya_text: str) -> list[str]:
    """Words appearing identically in at least 2 of the 3 readings.

    A bag-of-words intersection, not positional alignment — the three
    engines segment/space text differently, so lining up word *positions*
    across all three isn't reliable, but a word occurring verbatim in two
    independent readings is still strong evidence it's correct regardless
    of where in the line it falls. This exists because the whole-line
    majority_agreement flag (ensemble.py) requires the *entire* snippet to
    match byte-for-byte, which real sentences essentially never do (one
    stray character anywhere breaks it) — so on any snippet longer than a
    couple of words, the refiner previously got no consensus signal at
    all and would "correct" words every engine actually agreed on."""
    from collections import Counter

    counts = Counter()
    for text in (easy_text, tess_text, surya_text):
        if text:
            counts.update(set(text.split()))
    return sorted(w for w, c in counts.items() if c >= 2)


def _danda_floor(easy_text: str, tess_text: str, surya_text: str) -> dict:
    """Max count of each danda mark across the 3 readings — OCR engines
    more often miss a danda than hallucinate one, so if any single reading
    saw N of a mark, the corrected text should too."""
    floor = {}
    for mark in ("।", "॥"):
        floor[mark] = max((text or "").count(mark) for text in (easy_text, tess_text, surya_text))
    return {mark: n for mark, n in floor.items() if n > 0}


def _build_hint(easy_text: str, tess_text: str, surya_text: str, flags: dict) -> str:
    lines = []
    if flags.get("majority_agreement"):
        lines.append(
            "Note: at least two of the three readings above already agree exactly on this "
            "text. Treat that agreement as strong evidence it's correct — only deviate from "
            "it if it is clearly not a valid Kannada word or phrase."
        )
    consensus = _consensus_words(easy_text, tess_text, surya_text)
    if consensus:
        lines.append(
            "Note: these individual words appear identically in at least two of the three "
            "readings and should be treated as verified correct — do not change them: "
            + ", ".join(consensus)
        )
    floor = _danda_floor(easy_text, tess_text, surya_text)
    if floor:
        parts = [f"at least {n} '{mark}'" for mark, n in floor.items()]
        lines.append(
            "Note: the readings contain " + " and ".join(parts) + " mark(s) between them — "
            "your corrected output should contain at least that many, not fewer."
        )
    if flags.get("surya_suspect"):
        lines.append(
            "Note: Reading C was flagged by an automated quality check as possibly corrupted "
            "(e.g. a repeated word/phrase loop, or a hallucinated block) — weigh it less than "
            "Readings A and B unless it clearly fills a gap they leave."
        )
    return ("\n" + "\n".join(lines) + "\n") if lines else "\n"


def refine(easy_text: str, tess_text: str, surya_text: str, flags: dict, model: str = OLLAMA_MODEL) -> str:
    """Raises on failure (including after retries) - callers that need to
    keep going regardless (e.g. an unattended batch run) should catch this
    and fall back to fallback_text() rather than lose the whole page/book;
    see pipeline.process_page."""
    prompt = _PROMPT_TEMPLATE.format(
        easy=easy_text or "(empty)",
        tess=tess_text or "(empty)",
        surya=surya_text or "(empty)",
        hint=_build_hint(easy_text, tess_text, surya_text, flags),
    )
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.1}}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    last_error = None
    for attempt in range(1 + REFINE_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=REFINE_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["response"].strip()
        except Exception as e:
            last_error = e
            if attempt < REFINE_RETRIES:
                time.sleep(2)
    raise last_error


def fallback_text(easy_text: str, tess_text: str, surya_text: str, flags: dict) -> str:
    """Best guess without the LLM, for when refine() fails even after
    retries: the majority reading if two engines exactly agree (the same
    strong signal ensemble.compare() uses for majority_agreement), else
    EasyOCR's text (the strongest single baseline engine)."""
    if flags.get("majority_agreement"):
        normalized = [" ".join((t or "").split()) for t in (easy_text, tess_text, surya_text)]
        for i, ni in enumerate(normalized):
            for nj in normalized[i + 1:]:
                if ni == nj:
                    return [easy_text, tess_text, surya_text][i]
    return easy_text or tess_text or surya_text or ""


def open_corrections_db(db_path: Path) -> sqlite3.Connection:
    """Self-correction dictionary: every LLM refinement and every
    human-finalized review decision, logged per snippet. Seed of the "gold
    standard" store described in the original brief — word-level frequency
    mining off this table is a follow-up increment, not built here.

    `source` distinguishes an LLM draft (logged by pipeline.py at OCR time)
    from a human-confirmed decision (logged by review_app.py once the
    Phase 2 consensus flow reaches a terminal status) — the same snippet
    can have both rows, and the human one is the actual ground truth."""
    # check_same_thread=False: review_app.py shares one connection across
    # FastAPI's threadpool (same reasoning as review_db.open_review_db);
    # harmless for pipeline.py's single-threaded use of this function.
    conn = sqlite3.connect(db_path, check_same_thread=False)
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
            source TEXT NOT NULL DEFAULT 'llm',
            reviewers TEXT,
            image_patch_path TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    # Migration for corrections.db files created before these columns
    # existed (CREATE TABLE IF NOT EXISTS doesn't add columns to an
    # already-existing table).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(corrections)")}
    if "source" not in existing_cols:
        conn.execute("ALTER TABLE corrections ADD COLUMN source TEXT NOT NULL DEFAULT 'llm'")
    if "reviewers" not in existing_cols:
        conn.execute("ALTER TABLE corrections ADD COLUMN reviewers TEXT")
    if "image_patch_path" not in existing_cols:
        conn.execute("ALTER TABLE corrections ADD COLUMN image_patch_path TEXT")
    conn.commit()
    return conn


def log_correction(conn: sqlite3.Connection, record: dict, refined_text: str, model: str = OLLAMA_MODEL) -> None:
    conn.execute(
        """
        INSERT INTO corrections
            (book_id, page_number, side, paragraph_sequence, easyocr_text, tesseract_text, surya_text, refined_text, model, source, image_patch_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm', ?)
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
            record.get("image_patch_path"),
        ),
    )
    conn.commit()


def log_human_correction(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    page_number: int,
    side: str,
    paragraph_sequence: int,
    easyocr_text: str,
    tesseract_text: str,
    surya_text: str,
    final_text: str,
    reviewers: list[str],
    image_patch_path: str,
) -> None:
    """Logs a Phase 2 human-finalized decision (confirmed / provisionally
    verified / expert-approved) as ground truth in the same dictionary the
    LLM refiner writes to. image_patch_path is what Phase 3's export script
    pairs this correction's text with — see active_learning.py."""
    conn.execute(
        """
        INSERT INTO corrections
            (book_id, page_number, side, paragraph_sequence, easyocr_text, tesseract_text, surya_text, refined_text, model, source, reviewers, image_patch_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'human', ?, ?)
        """,
        (
            book_id,
            page_number,
            side,
            paragraph_sequence,
            easyocr_text,
            tesseract_text,
            surya_text,
            final_text,
            "human-review",
            ",".join(reviewers),
            image_patch_path,
        ),
    )
    conn.commit()
