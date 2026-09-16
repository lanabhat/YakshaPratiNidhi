# Lipi-Sampada — Phase 0/1/2 POV

Converts mobile-scanned Kannada book pages (Yakshagana/prose) into paragraph
snippets, runs EasyOCR + Tesseract + Surya (a local VLM-based OCR) on each
snippet, asks a local LLM (via Ollama) to pick/correct the best reading, and
writes a JSON comparison + a visual QA report. A small local web app then
lets human reviewers confirm or correct each snippet with a two-reviewer
consensus check. Fully local, no paid services.

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

2. **GPU-enabled EasyOCR (optional)** — `pip install easyocr` pulls in a
   CPU-only `torch` by default. `ocr_engines.py` auto-detects
   `torch.cuda.is_available()` and falls back to CPU automatically, so this
   step is purely a speedup, not required — skip it on a machine without an
   NVIDIA GPU. To enable it, install a CUDA build matching both your
   driver's CUDA version (`nvidia-smi` shows it top-right) and the `torch`
   version already pinned by the other deps (currently `2.14.0`); this
   machine has driver CUDA 13.2, so:
   ```
   .venv\Scripts\pip install --index-url https://download.pytorch.org/whl/cu132 torch==2.14.0+cu132 torchvision==0.29.0+cu132
   ```
   Find the right `cuXXX` tag for a different `torch`/driver combination by
   browsing `https://download.pytorch.org/whl/torch/` and
   `.../torchvision/` for your versions. This doesn't affect Surya or
   Ollama — they already use the GPU independently of this Python `torch`
   install (see step 4).

3. **Tesseract OCR engine** — this is a system binary, not a pip package.
   Install it (Windows): `winget install --id tesseract-ocr.tesseract`.

4. **Kannada language data** — Tesseract's Windows installer doesn't bundle
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

5. **Surya's llama-server binary** — Surya's current release is a VLM OCR
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

6. **Ollama, for the Phase 1 AI refiner** — install from
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
  - `corrections.db` — SQLite log of every LLM refinement (`source='llm'`)
    and, once the review app runs against this folder, every human-
    finalized decision too (`source='human'`, with the deciding
    reviewer(s) attributed) — this is the "self-correction dictionary"
    from the original brief; word-level frequency mining off this table
    is a follow-up increment, not built yet
  - `report.html` — open this in a browser to visually compare each
    snippet's four candidates against the source image; flagged rows are
    highlighted.

## Running the Phase 2 review app

A small FastAPI + vanilla-JS app implements the two-reviewer consensus
flow from the original brief: a snippet shown to reviewer A is confirmed
outright if they leave it unchanged; an edit sends it to a second,
independent reviewer B (shown the original AI draft, not A's edit); if B
submits the same edit it's `provisionally_verified`, otherwise it's
flagged `needs_expert` for a final, authoritative resolution. There's no
login — reviewers just type a name once (stored in the browser) — so
anyone can act as the "expert" on a flagged snippet; fine for a small
trusted group, worth revisiting before opening this to the wider public.

```
$env:LIPISAMPADA_RUN_DIR = "output\poc_5pages_refined"   # or any run with a result.json
.venv\Scripts\python -m uvicorn lipisampada.review_app:app --reload
```

Then open `http://127.0.0.1:8000`. On first launch it seeds a
`review.db` (SQLite, per run directory) from that run's `result.json`;
re-running is safe, existing rows aren't touched. Every time a snippet
reaches a terminal status (confirmed / provisionally_verified /
expert_approved), that decision is also logged into the same run's
`corrections.db` as a `source='human'` row — see "Running the
pipeline" above. `GET /api/stats` gives a quick progress readout
without opening the browser.

## Roadmap

Phase 0 (layout slicer + 3-engine OCR ensemble), Phase 1 (LLM refiner +
correction log), and Phase 2 (human review app with two-reviewer
consensus, feeding finalized decisions back into the correction log)
are built. Still ahead: Phase 3 (active learning / OCR fine-tuning loop
once ~1,000 corrections are collected).
