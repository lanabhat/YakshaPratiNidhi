# Lipi-Sampada — Phase 0/1/2 POV

Converts mobile-scanned Kannada book pages (Yakshagana/prose) into paragraph
snippets, runs EasyOCR + Tesseract + Surya (a local VLM-based OCR) on each
snippet, asks a local LLM (via Ollama) to pick/correct the best reading, and
writes a JSON comparison + a visual QA report. A small local web app then
lets human reviewers confirm or correct each snippet with a two-reviewer
consensus check. Fully local, no paid services.

## Three apps

| # | App | What it does | Start it with | Local URL | Where it lives eventually |
|---|-----|------|------|------|------|
| 1 | **Prep & OCR** | Download/rasterize a book, crop pages in the browser, run OCR, auto-publish | `.\run_1_prep_and_ocr.ps1` | http://127.0.0.1:8100 | Always your own machine (needs your GPU + Ollama) |
| 2 | **Backend** | The review API: database, roles, image URLs | `.\run_2_backend.ps1` | http://127.0.0.1:8200 | PythonAnywhere |
| 3 | **Frontend** | The web page reviewers/editors/admins use | `.\run_3_frontend.ps1` | http://127.0.0.1:8300 | Firebase Hosting |

App 1 never talks to app 3 directly - it only pushes finished books to app 2 (via
`API_BASE_URL`/`INGEST_API_KEY` in `.env`), and app 3 only ever talks to app 2 (via
`web/config.js`'s `API_BASE`). To try the whole thing locally before deploying anything,
start them in order (2, then 3, then 1) with no `.env` storage/API changes needed - the
defaults are a local SQLite file and a local image folder, no Supabase or PythonAnywhere
required yet. (These three are thin wrappers around `run_api.ps1`/`run_web.ps1`/`run_intake.ps1`,
kept for anyone already used to those names.)

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
   discarded. Qwen isn't perfect either — the refiner was once seen
   introducing a new error on a snippet none of the three engines got wrong
   on ("ತುಂಟತನ" → "ತುಂಡತನ"). Root cause: `ensemble.py`'s `majority_agreement`
   flag requires the *entire* snippet's text to match byte-for-byte between
   engines, which real sentences essentially never do (one stray character
   anywhere breaks it), so on anything longer than a couple of words the
   refiner got no consensus signal at all. Fixed by adding a bag-of-words
   consensus check (`refiner._consensus_words`) that catches word-level
   agreement independent of the rest of the line, plus a danda-mark count
   floor (`refiner._danda_floor`) and stronger prompt wording against
   unnecessary "corrections" and punctuation loss — see that module for
   both.

   This isn't a complete fix, and isn't meant to be: the danda-mark floor
   is an aggregate count, not a positional guarantee, so a specific mark
   can still occasionally get dropped while the line's total count still
   passes; and on short snippets the LLM has been seen overriding an
   explicit "don't change this" instruction on genuinely ambiguous cases
   (two valid word forms, like ವಾರ್ಧಿಕ/ವಾರ್ಧಿಕೆ). Both are exactly the kind
   of thing the raw-engine-readings panel in Phase 2 review surfaces in
   seconds — this refiner is a draft-quality first pass, not a
   ground-truth guarantee, by design.

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
  - `pages/` — one full-page image per page (the un-cropped `gray` preprocessed
    page), so the review app can show a reviewer the surrounding context —
    e.g. a continuing verse/song — that a tightly-cropped snippet alone loses
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
Clicking a snippet's image (or the "View full page" button) opens the full
page it came from, with the current snippet highlighted — the highlight is
an orientation aid, not pixel-exact (it's drawn from `bbox`, captured
*before* `layout.trim_to_ink()`'s further reframing). The full-page panel
can be dragged (by its header) to dock beside the review form or stacked
above/below it, and resized via its native corner handle; the panel's
zoom and text-size sliders and the chosen layout all persist per-browser.
On narrow/mobile screens the panel instead opens as a full-screen layer on
top of the review form — a floating button swaps which one is in front
("Send to back" / "Bring to front") so the form is never permanently
blocked.

Prev/Next buttons browse every snippet in page order (not just the pending
queue) — a snippet already finalized shows read-only with no submit
buttons. Each of the 3 raw engine readings shows a live percentage next to
it — how closely that engine's text currently matches what's in the box —
recomputed as you type. Selecting a word or phrase in the text box pops up
each engine's corresponding text at that spot (click to replace) plus a
free-type field; the correspondence is found via a word-level alignment
between the current draft and each raw reading, so it holds up even when
the three engines split the text into different numbers of words.

```
.\run_review.ps1 output\poc_5pages_refined   # or any other run with a result.json; omit to use the newest run
```

Then open `http://127.0.0.1:8000`. On first launch it seeds a
`review.db` (SQLite, per run directory) from that run's `result.json`;
re-running is safe, existing rows aren't touched. Every time a snippet
reaches a terminal status (confirmed / provisionally_verified /
expert_approved), that decision is also logged into the same run's
`corrections.db` as a `source='human'` row — see "Running the
pipeline" above. `GET /api/stats` gives a quick progress readout
without opening the browser.

## Running the PDF intake & OCR queue app (Phase A of the next phase)

Feeds the OCR pipeline from the Pratisangraha catalog (`db/pratisangraha.sqlite3`,
a handed-off snapshot — 1871 cataloged books, each with a public Google Drive
PDF link) instead of hand-photographed `.tif` pages. Browse/search the
catalog, preview a book's PDF in-browser, and queue it; a background worker
then downloads the PDF, rasterizes every page at 300 DPI, and auto-preps each
page (gross deskew + margin trim + a two-page-spread heuristic). You then
review each page in the browser — drag the crop handles, rotate, and (if
flagged, or manually) split a spread into two pages — and approve it. Once
every page of a book is approved, the queue hands the finished pages straight
to the *same* `pipeline.run_book()` used above (unchanged — same OCR
ensemble, same LLM refiner, same `output/<book_id>/result.json` shape), and
moves on to the next queued book.

```
.\run_intake.ps1
```

Then open `http://127.0.0.1:8100`. The **Catalog** tab opens on **No kosha link**
(1,348 of the 1,871 books: a missing kosha link is stored either as empty or as
the placeholder `(ನಿರೀಕ್ಷಿಸಿ)`); switch to *Has kosha link* or *All* with the
selector. Each row has **Preview prati sangraha**, **Preview kosha** (greyed out
when there is none) and **Add to queue**; tick rows (or the header box) and
use **Add selected to queue** to queue many at once. Books already queued show
a status badge instead of a button. The queue always OCRs the **prati** scan.
The prati link can be a Google Drive file, an archive.org item, or a plain
`.pdf` URL. The catalog is paged (50 per page, with go-to-page).

**Where you crop / rotate / split:** Queue tab, once a book reaches *awaiting
review* (a yellow banner and a number on the Queue button tell you) click
**Review pages**. That opens the page editor: drag the crop handles, rotate,
tick *Split into two pages* for spreads, then *Approve this page*. When every
page is approved, OCR starts on its own. **The page editor** (Queue tab -> *Review pages*) works on the untouched
rasterized page, so an automatic crop can always be widened again:

- **Auto-crop** is drawn on every page when it opens: the sheet's outline for a phone
  photo, a tight skew-corrected box around the text for a clean scan. **Accept & next**
  takes it as is; **Accept all remaining auto-crops** skips ahead on clean books.
- **Free-form crop:** drag any of the four blue corners on its own (so a slanted left edge
  is just two corners moved), drag a yellow edge dot to **curve** that edge (for pages that
  bow), drag inside to move the crop, drag on empty space to draw another one.
- **Multiple crops per page** (columns, spreads, page plus margin notes). Each becomes its
  own page part in the order you arrange them: one crop is page 12, two are 12L / 12R,
  three or more are 12A, 12B, 12C. **Split in 2** is a shortcut for a spread.
- **Straightening:** every crop is warped flat (perspective for straight edges, a curved
  patch when edges are bent), **90 deg** rotate, and **Auto-dewarp**, a best-effort text-line
  dewarp (optional package `page-dewarp`, run in its own process). It is unstable by nature:
  check the preview, and use the curve handles when it misses.
- **Prepare for OCR** per crop, with a live preview of exactly what OCR will read:
  brightness, contrast, and black & white (threshold, automatic Otsu, or adaptive for uneven
  lighting). The saved page is the enhanced image.

The queue survives an interrupt/restart —
each book's progress (downloaded / rasterized / awaiting review / approved /
OCR running / done) is tracked in `db/intake_queue.sqlite3`, separate from
the read-only catalog snapshot. Books are processed one at a time end to
end, matching the OCR ensemble's single-GPU / single-Ollama-instance
constraint. Downloaded PDFs and rasterized/approved page images live under
`intake_work/<book_id>/` (gitignored, like `output/`).

## Review platform API + automatic publishing (local-first)

The multi-book review platform's backend (`src/lipisampada/reviewapi/`, Flask
so it runs as-is on PythonAnywhere) and the publisher that feeds it.

```
copy .env.example .env      # then edit: INGEST_API_KEY, SUPERADMIN_EMAILS (git-ignored)
.
un_api.ps1               # API on http://127.0.0.1:8200 (data in review_api_data/)
.
un_intake.ps1            # as before; finished books now publish themselves
```

When a queued book finishes OCR, the intake worker **publishes it
automatically**: page images (downscaled WebP, ~170 KB instead of 3-11 MB) and
snippet crops go to image storage, a gzipped bundle of the three engines' raw
readings goes next to them, and the book's text + image URLs are POSTed to the
API. Re-publishing is safe (unchanged images are skipped, and text a person has
already worked on is never overwritten); a failed publish shows a **Retry
publish** button in the Queue tab and never marks the OCR itself as failed.
Nothing is published until `API_BASE_URL` and `INGEST_API_KEY` are set in `.env`
(the button appears instead).

Model: anyone - even a guest - can attach a suggestion (an edited text, or a
"looks right" vote) to a snippet, one per person, latest wins. Suggestions are
diffed against the current text and tallied **per word** (same change from
several reviewers is grouped: `ತುಂಡತನ -> ತುಂಟತನ: 3 reviewers`). Guests are
recorded but never count toward the "needs attention" threshold (2 signed-in
reviewers agreeing). Editors/admins approve per word, per snippet, or per page
("approve all on this page", with a preview first: it applies only changes a
clear majority agreed on). Suggestions stay open after finalization, so a
finalized snippet can be challenged. Only admins re-open a finalized snippet or
change roles (guest / reviewer / editor / admin / superadmin).

Swappable pieces, so everything runs with no accounts today:
- **Image storage**: `STORAGE_BACKEND=local` (a folder the API serves) or
  `supabase`. The Supabase backend follows its documented REST API but has
  **not yet been exercised against a real project**.
- **Sign-in**: `AUTH_MODE=dev` (token `dev:<email>`, local development only) or
  `firebase` (Google sign-in; verification also **not yet exercised against a
  real Firebase project**, needs `pip install pyjwt cryptography`).

`python -m pytest` runs the tally, API and publisher tests.

### The review web app (`web/`)

Plain static files (no build step), so the same folder is what you deploy to
Firebase Hosting later. Locally: copy `web/config.example.js` to `web/config.js`
(git-ignored, like `.env` - keeps your per-deployment values, including your
Firebase project's config, out of the repo) and edit it (`API_BASE`, `AUTH_MODE`,
`FIREBASE_CONFIG`), then `.\run_api.ps1` then `.\run_web.ps1` and open
`http://127.0.0.1:8300`. "Sign in" in dev mode just asks for an email (the first email in
`SUPERADMIN_EMAILS` becomes superadmin); anyone not signed in browses and
suggests as a guest.

- **Library**: every published book with total snippets, finalized, pending,
  awaiting approval, untouched, needs-attention, completion %.
- **Review page** (`#/book/<id>/page/<n>`): the full page image beside that
  page's snippets. Each snippet shows word-level highlights and per-word
  reviewer counts, the three engines' raw readings with an agreement score
  each (loaded from the book's gzipped bundle), and *Looks right* / *Suggest
  this edit*. Selecting a word or more in the edit box pops up what each
  engine read there, to swap in with one click (or type a replacement).
  Editing without sending marks the snippet unsent (amber outline, "Looks
  right" disabled) and warns before you navigate away. Editors and admins
  also get *Accept* per word, *Approve as is*, *Approve my text*, and
  *Approve page...* (preview first); admins can *Re-open*. First / prev / next /
  last, go-to-page and a paginated page strip (green = fully finalized, amber =
  has snippets needing attention).
- **Full text** (`#/book/<id>/read/<n>`): five pages at a time as continuous
  text, coloured by status; click any paragraph to open it on the review page.
  Plain-text export of the current or finalized-only text.
- **Admin** (`#/admin`, admins only): five tabs - **Users** (search, change role,
  deactivate, ban, invite by email), **Applicants** (see below), **Contributors**
  (edits / "looks right" / approvals per person, optionally per book), **Books**
  (progress, hide or show a book, final-text download) and **Permissions** (the
  live table of who can do what).

### Admins, roles and rights

**Setting up the first admin.** Put your Google email in `SUPERADMIN_EMAILS` (in `.env`
locally, in the PythonAnywhere environment variables later; comma-separate several). The first
time you sign in with that email you become **superadmin**. Then open **Admin -> Users** and
invite everyone else by email with the role they should start with; the role is applied when
they first sign in, and the invite disappears. Anyone who signs in **without** an invite has to
fill in a short form first (see "Volunteer applications" below) before they count as more than a
guest. You can change anyone's role later.

**Real Google sign-in.** Set `AUTH_MODE=firebase` (in `.env` for the API, and in `web/config.js`
for the site) and:
1. Create a project at the [Firebase console](https://console.firebase.google.com), then
   **Authentication -> Sign-in method -> Google -> enable**.
2. **Project settings -> General -> Your apps -> add a web app** (no Firebase Hosting needed for
   this). Copy the `apiKey`/`authDomain`/`projectId` it gives you into `web/config.js`'s
   `FIREBASE_CONFIG`.
3. Set `FIREBASE_PROJECT_ID` in `.env` to the same project's ID.
4. `pip install pyjwt cryptography` (already in `requirements.txt`) - the API verifies Google's ID
   tokens itself against Google's published keys; no Firebase Admin SDK or service-account file
   needed.
No Firebase project exists yet for this app, so the Google-popup part is implemented per Firebase's
documented client SDK usage but hasn't been exercised end to end. Everything downstream of sign-in -
the onboarding form, admin approval, roles, bans - is fully working today through `AUTH_MODE=dev`,
since a dev sign-in creates a real account through the exact same code path a Google sign-in would.

**Volunteer applications.** A brand-new Google sign-in (no invite) is shown a short form - name,
place, "would you be reviewing books / volunteering?", and an optional free-text note - right after
they sign in. Until they answer, and while an admin hasn't yet decided, they have guest-level access
(read + suggest) regardless of anything else. Saying **no** signs them straight back out to guest
browsing and is remembered, so they're never asked again. Saying **yes** puts them in
**Admin -> Applicants** (with a badge count on the Admin nav link) for an admin to **Approve** or
**Reject**; approving makes them a full reviewer. There is no email notification - admins see this
in-app, the same way pending invites already work.

**Bans.** Deactivating (existing) keeps someone's read access but blocks writes - for a pause, not a
punishment. **Banning** is stronger: zero access at all, not even reading, for someone who is
actively unhelpful or posting bad-faith suggestions - the API refuses every request from them except
`/api/me`, so their own app shows a plain "you've been banned" message instead of the site. Both have
the same safeguards as role changes: nobody can act on their own account, and only a superadmin can
touch a superadmin.

**Keeping this from being trivially abused.** A real Google sign-in already filters out casual
scripted abuse (Firebase requires solving Google's own auth challenges). On top of that: a brand-new
account can only read and suggest - nothing it does carries any real weight until an admin approves
it, and even an approved reviewer's suggestions only become finalized text once an editor accepts
them or two-plus reviewers agree (see `tally.py`). `flask-limiter` caps how fast any one
account/guest/IP can call the write endpoints (60/minute on suggest/accept-word/finalize, 5/hour on
the application form, 300/minute overall) - enough headroom for real use, not for a script. This is
in-memory (single process), matching the project's current single-SQLite-writer scale; it is not
meant to withstand a determined bot farm, only to stop one runaway script.

**The roles** (each includes everything to its left):

| Permission | guest | reviewer | editor | admin | superadmin |
|---|:-:|:-:|:-:|:-:|:-:|
| read the library, pages, full text | x | x | x | x | x |
| suggest edits / "looks right" | x | x | x | x | x |
| approve text (accept a word, approve a snippet or page) | | | x | x | x |
| re-open a finalized snippet | | | | x | x |
| manage users (roles up to admin, invites, deactivate) | | | | x | x |
| manage books (hide/show), view contributor stats | | | | x | x |
| grant or change **superadmin** | | | | | x |

A *guest* is anyone not signed in: their suggestions count, but are shown separately from signed-in
reviewers. Nobody can change or deactivate their own account, and only a superadmin can grant or change
the superadmin role. A **deactivated** account can still read but cannot suggest or approve. A **hidden**
book disappears for everyone except admins; reviews already made are kept.

**Where rights are defined.** In one place, `src/lipisampada/reviewapi/permissions.py`: a table of
permission -> the minimum role that has it. Every check in the API calls `require(user, "<permission>")`,
and the web app asks the server which permissions the signed-in person has (`/api/me`), so the buttons
people see always match what the server will accept. To change what a role may do, edit that table and the
matrix test in `tests/test_permissions.py`. Rights are fixed by role (not editable per role in the app, and not
per book); that keeps them predictable, and the Permissions tab always shows the real table.

Not built yet: a page for the audit log, and Word/PDF export.

## Running the Phase 3 active learning tooling

`active_learning.py` aggregates every `source='human'` row (real
human-verified ground truth — `source='llm'` rows are excluded, since
fine-tuning on the LLM's own unverified guesses would just teach the
engine to repeat whatever it didn't catch) across every run's
`corrections.db`, checks whether the ~1,000-correction retraining
trigger from the original brief has been hit, and can export or stage
that data. It does not run a real fine-tune itself — see below.

```
.\run_active_learning.ps1                                          # just the trigger check
.\run_active_learning.ps1 --export-jsonl corrections_export.jsonl   # {original_ocr, human_correction, image_patch_path} pairs
.\run_active_learning.ps1 --prepare-tesstrain tesstrain_data        # image + .gt.txt pairs, ready for tesstrain
```

**Fine-tuning target: Tesseract**, via
[tesstrain](https://github.com/tesseract-ocr/tesstrain) — chosen over
EasyOCR/Surya because it has a documented, free/open fine-tuning
workflow and was our most accurate baseline engine. `--prepare-tesstrain`
stages `<name>.png` + `<name>.gt.txt` pairs in the layout tesstrain's
`data/<lang>-ground-truth/` expects; from there, fine-tuning is:
```
git clone https://github.com/tesseract-ocr/tesstrain
# copy the staged pairs into tesstrain/data/kan-ground-truth/
cd tesstrain
make training MODEL_NAME=kan_lipisampada START_MODEL=kan TESSDATA=<path to this project's tessdata/>
```
This needs Tesseract's training tools (`lstmtraining`,
`combine_lang_model`, ...), which the `tesseract-ocr.tesseract` winget
package used above does **not** include — expect to build them from
source or find a training-tools-inclusive distribution; not set up as
part of this project since there's not yet enough real correction data
to make running it worthwhile (see below).

At this project's actual data volume — a handful of real corrections
from testing Phase 2, not real review throughput — the retraining
trigger won't be anywhere near ready, and `--prepare-tesstrain` will
say so rather than pretending a few dozen pairs are enough to fine-tune
on meaningfully.

## Roadmap

Phase 0 (layout slicer + 3-engine OCR ensemble), Phase 1 (LLM refiner +
correction log), Phase 2 (human review app with two-reviewer consensus,
feeding finalized decisions back into the correction log), and the
tooling half of Phase 3 (retraining trigger, JSON-pair export, tesstrain
data staging) are built. Still ahead: actually running a fine-tune, once
real review throughput accumulates enough `source='human'` corrections
to make it worthwhile.
