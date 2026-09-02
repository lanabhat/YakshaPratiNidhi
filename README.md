# Lipi-Sampada — Phase 0 POV

Converts mobile-scanned Kannada book pages (Yakshagana/prose) into paragraph
snippets and runs two free/open OCR engines (EasyOCR + Tesseract) on each,
producing a JSON comparison and a visual QA report. Fully local, no paid
services.

## Setup

1. **Python deps** (already set up in `.venv` if you're continuing this session):
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

2. **Tesseract OCR engine** — this is a system binary, not a pip package.
   Install it (Windows): `winget install --id tesseract-ocr.tesseract`.

3. **Kannada language data** — Tesseract's Windows installer doesn't bundle
   the `kan` language pack, and this project doesn't have admin rights to
   write into `Program Files\Tesseract-OCR\tessdata`. Instead, `kan.traineddata`
   and `eng.traineddata` are downloaded into a project-local `tessdata/`
   folder (gitignored — re-download if missing):
   ```powershell
   Invoke-WebRequest -Uri "https://github.com/tesseract-ocr/tessdata/raw/main/kan.traineddata" -OutFile "tessdata\kan.traineddata"
   Invoke-WebRequest -Uri "https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata" -OutFile "tessdata\eng.traineddata"
   ```
   `ocr_engines.py` points Tesseract at this folder automatically via
   `TESSDATA_PREFIX` / `--tessdata-dir`.

## Running the POV pipeline

```
.venv\Scripts\python -m lipisampada.pipeline --input Input_Prasanga --pages 3
```

- `--pages N` limits to the first N scans (sorted by filename) — good for a
  quick smoke test before running the full batch.
- Omit `--pages` to process every `.tif` in the input folder.
- Output goes to `output/<timestamp>/`:
  - `snippets/` — cropped paragraph images
  - `result.json` — per-paragraph OCR ensemble output (text + confidence
    from both engines, disagreement/low-confidence flags)
  - `report.html` — open this in a browser to visually compare each
    snippet against both engines' output; flagged rows are highlighted.

## Roadmap

This is Phase 0 of a larger pipeline. See the project plan for Phase 1
(Ollama-based semantic refiner + self-correction dictionary), Phase 2
(human-in-the-loop crowd review app), and Phase 3 (active learning /
fine-tuning loop).
