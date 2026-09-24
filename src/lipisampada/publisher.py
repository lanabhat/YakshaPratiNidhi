"""Publishes a finished OCR run to the review platform: downscaled page
images and snippet crops + one gzipped bundle of the raw OCR readings go to
image storage (local folder in development, Supabase in production), then the
book's text and image URLs are POSTed to the review API.

Design points:
- Page images are downscaled WebP for display (a 300 DPI page is 3-11 MB; a
  review screen needs ~200-400 KB). Originals stay on this machine.
- Raw readings (3 engines + boxes + flags) are bulky and read-mostly, so they
  live in one gzipped bundle per book in storage, not in the live database.
- Safe to re-run: uploads whose source file is unchanged are skipped
  (publish_state.json in the run dir), and the API's ingest never overwrites
  text a person has already worked on.
- The whole book goes to ingest in one request: page ordering (page_index)
  is computed across the whole book."""

import gzip
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

PAGE_MAX_WIDTH = 1400
PAGE_QUALITY = 80
SNIPPET_MAX_WIDTH = 1200
SNIPPET_QUALITY = 85
UPLOAD_WORKERS = 8


def _webp_bytes(path: Path, max_width: int, quality: int) -> bytes:
    with Image.open(path) as im:
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        if im.width > max_width:
            im = im.resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=quality, method=4)
        return buf.getvalue()


def _signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def snippet_id(book_id: str, rec: dict) -> str:
    return f"{book_id}:{rec['page_number']}:{rec['side']}:{rec['paragraph_sequence']}"


def http_post(api_base: str, ingest_key: str):
    def post(path: str, payload: dict) -> dict:
        r = requests.post(
            f"{api_base.rstrip('/')}{path}", json=payload, headers={"X-Ingest-Key": ingest_key}, timeout=300
        )
        if r.status_code >= 400:
            raise RuntimeError(f"API {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    return post


def publish_book(
    run_dir: Path,
    book_id: str,
    storage,
    post,
    title: str | None = None,
    kavi: str | None = None,
    catalog_entry_id: int | None = None,
    progress=print,
) -> dict:
    run_dir = Path(run_dir)
    records = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if not records:
        raise RuntimeError("result.json has no records - nothing to publish")

    # Resume state is per storage target: uploads recorded against one target
    # (e.g. a test folder) must never make a publish to another skip images.
    state_path = run_dir / "publish_state.json"
    target = storage.url_for("")
    saved = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    state = saved.get("files", {}) if saved.get("target") == target else {}

    # -- image uploads (page images once each, snippet crops per record) ---------
    jobs = {}  # key -> (source path, max_width, quality)
    for r in records:
        for rel, mw, q in ((r.get("page_image_path"), PAGE_MAX_WIDTH, PAGE_QUALITY),
                           (r["image_patch_path"], SNIPPET_MAX_WIDTH, SNIPPET_QUALITY)):
            if rel:
                src = run_dir / rel
                key = f"books/{book_id}/{Path(rel).parent.name}/{src.stem}.webp"
                jobs[key] = (src, mw, q)

    def upload(item):
        key, (src, mw, q) = item
        sig = _signature(src)
        if state.get(key) == sig:
            return key, storage.url_for(key), True
        url = storage.put_bytes(key, _webp_bytes(src, mw, q), "image/webp")
        return key, url, False

    urls, uploaded, skipped = {}, 0, 0
    with ThreadPoolExecutor(UPLOAD_WORKERS) as pool:
        for i, (key, url, was_skipped) in enumerate(pool.map(upload, jobs.items()), 1):
            urls[key] = url
            state[key] = _signature(jobs[key][0])
            skipped += was_skipped
            uploaded += not was_skipped
            if i % 50 == 0:
                progress(f"  uploaded {i}/{len(jobs)} images")
    state_path.write_text(json.dumps({"target": target, "files": state}), encoding="utf-8")

    # -- bundle of raw readings ---------------------------------------------------
    bundle = {
        snippet_id(book_id, r): {
            "easyocr": r["easyocr"]["text"],
            "tesseract": r["tesseract"]["text"],
            "surya": r["surya"]["text"],
            "ai_text": (r.get("refined") or {}).get("text") or r["easyocr"]["text"],
            "bbox": r["bbox"],
            "flags": r["flags"],
        }
        for r in records
    }
    bundle_bytes = gzip.compress(json.dumps(bundle, ensure_ascii=False).encode("utf-8"), 9)
    bundle_url = storage.put_bytes(f"books/{book_id}/bundle.json.gz", bundle_bytes, "application/gzip")

    # -- ingest ---------------------------------------------------------------------
    def url(rel):
        return urls.get(f"books/{book_id}/{Path(rel).parent.name}/{Path(rel).stem}.webp") if rel else None

    snippets = []
    for r in records:
        refined = r.get("refined") or {}
        snippets.append(
            {
                "page_number": r["page_number"],
                "side": r["side"],
                "seq": r["paragraph_sequence"],
                "ai_text": refined.get("text") or r["easyocr"]["text"],
                "page_image_url": url(r.get("page_image_path")),
                "snippet_image_url": url(r["image_patch_path"]),
                "bbox": r["bbox"],
                "refine_failed": bool(r["flags"].get("refine_failed")),
            }
        )
    result = post(
        "/api/ingest/book",
        {
            "book": {
                "id": book_id,
                "title": title,
                "kavi": kavi,
                "catalog_entry_id": catalog_entry_id,
                "page_count": len({(s["page_number"], s["side"]) for s in snippets}),
                "bundle_url": bundle_url,
            },
            "snippets": snippets,
        },
    )
    result.update(images_uploaded=uploaded, images_skipped=skipped, bundle_url=bundle_url)
    return result
