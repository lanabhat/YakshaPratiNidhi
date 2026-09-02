# Lipi-Sampada — Phase 0 POV

Converts mobile-scanned Kannada book pages (Yakshagana/prose) into paragraph
snippets, runs EasyOCR + Tesseract on each snippet, and runs Surya (a local
VLM-based OCR) once per whole page, producing a JSON comparison and a
visual QA report. Fully local, no paid services.

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
   `ocr_engines.py` points Surya at this binary via `LLAMA_CPP_BINARY`.
   Surya is only run once per whole page (`run_surya_page`, which downscales
   to ~800px wide first) — its full-page mode fell into a repetition loop
   on our small pre-cropped paragraph snippets, and ran extremely slowly on
   full-resolution pages, but works well (~10-30s/page) on a downscaled
   whole page.

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
