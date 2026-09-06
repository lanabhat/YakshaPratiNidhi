# Lipi-Sampada — Phase 0/1 POV

Converts mobile-scanned Kannada book pages (Yakshagana/prose) into paragraph
snippets, runs EasyOCR + Tesseract + Surya (a local VLM-based OCR) on each
snippet, asks a local LLM (via Ollama) to pick/correct the best reading, and
writes a JSON comparison + a visual QA report. Fully local, no paid services.

## Setup

1. **Python deps** (already set up in `.venv` if you're continuing this session):
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   `surya-ocr` pulls in `opencv-python-headless` as a transitive dependency,
   which silently shadows `opencv-python` in the shared `cv2` import
   namespace and can change image-processing output (this happened once —
   it changed adaptive-threshold/denoise behavior enough to alter paragraph
   slicing counts). After installing, force-reinstall the intended package
   to make sure it wins:
   ```
   .venv\Scripts\pip uninstall -y opencv-python-headless
   .venv\Scripts\pip install --force-reinstall --no-deps opencv-python==5.0.0.93
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

4. **Surya's llama-server binary** — Surya's current release is a VLM OCR
   whose llama.cpp backend spawns a standalone `llama-server` binary (not
   the `llama-cpp-python` pip package, which isn't used). A CUDA build is
   downloaded (no compiler needed) into a project-local `tools/llamacpp_cuda/`
   folder (gitignored), matching this machine's NVIDIA GPU:
   ```powershell
   $ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow otherwise
   $dest = "tools\llamacpp_cuda"
   New-Item -ItemType Directory -Force -Path $dest | Out-Null
   Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/download/b10760/llama-b10760-bin-win-cuda-12.4-x64.zip" -OutFile "$dest\llama.zip"
   Invoke-WebRequest -Uri "https://github.com/ggml-org/llama.cpp/releases/download/b10760/cudart-llama-bin-win-cuda-12.4-x64.zip" -OutFile "$dest\cudart.zip"
   Expand-Archive -Path "$dest\llama.zip" -DestinationPath $dest -Force
   Expand-Archive -Path "$dest\cudart.zip" -DestinationPath $dest -Force
   Remove-Item "$dest\llama.zip", "$dest\cudart.zip"
   ```
   `ocr_engines.py` points Surya at this binary via `LLAMA_CPP_BINARY`. Surya
   runs once per paragraph snippet (matching EasyOCR/Tesseract), not once per
   page — it needs `layout.trim_to_ink`'s ink-density reframing to behave
   (see that function's docstring: too-sparse or too-tight crops send its
   decoder into a repetition loop or make it drop the block as blank). Even
   with that fix, occasional bad output still gets through — `ocr_engines.py`
   flags it via `run_surya`'s `suspect` field rather than retrying
   indefinitely (see that module's comments for why an internal-regen and an
   external kill-and-restart retry were both tried and dropped: real
   failures turned out to be deterministic per crop, and one pathological
   snippet burned 10+ minutes trying to retry its way to a clean result).

5. **Ollama, for the Phase 1 AI refiner** — install from
   [ollama.com](https://ollama.com) or `winget install --id Ollama.Ollama`,
   then pull the model used here:
   ```
   ollama pull qwen2.5:7b-instruct
   ```
   `refiner.py` talks to Ollama's local HTTP API (`localhost:11434`, no
   Python client library needed) — make sure `ollama serve` is running (the
   Windows installer starts it as a background service automatically).
   Llama3.1:8b was tested as an alternative and rejected: it was better at
   respecting the majority-agreement hint but worse at normalizing OCR-mangled
   verse markers and once kept a Surya hallucination the refiner should have
   discarded. Qwen isn't perfect either — the refiner has been seen to
   introduce a new error on a snippet none of the three engines got wrong on
   ("ತುಂಟತನ" → "ತುಂಡತನ"), a reminder that its output is a strong draft for
   Phase 2 human review, not ground truth.

## Running the pipeline

```
.venv\Scripts\python -m lipisampada.pipeline --input Input_Prasanga --pages 3
```

- `--pages N` limits to the first N scans (sorted by filename) — good for a
  quick smoke test before running the full batch.
- Omit `--pages` to process every `.tif` in the input folder.
- `--no-refine` skips the LLM step (Phase 0 behavior; doesn't need Ollama
  running).
- Output goes to `output/<timestamp>/`:
  - `snippets/` — cropped paragraph images
  - `result.json` — per-paragraph engine outputs, ensemble flags
    (disagreement/low-confidence/majority-agreement/surya-suspect), and the
    LLM-refined text
  - `corrections.db` — SQLite log of every LLM refinement (the seed of the
    "self-correction dictionary" from the original brief; word-level
    frequency mining off this table is a follow-up increment, not built yet)
  - `report.html` — open this in a browser to visually compare each
    snippet's four candidates against the source image; flagged rows are
    highlighted.

## Roadmap

Phase 0 (layout slicer + 3-engine OCR ensemble) and the first pass of
Phase 1 (LLM refiner + correction log) are built. Still ahead: Phase 2
(human-in-the-loop crowd review app with two-reviewer consensus) and
Phase 3 (active learning / OCR fine-tuning loop once ~1,000 corrections
are collected).
