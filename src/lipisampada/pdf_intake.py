"""PDF intake: download a book's PDF from its public Google Drive link,
rasterize its pages at OCR-quality resolution, auto-prep them (gross
deskew + margin trim + spread-likely heuristic), and — once a human has
approved each page's crop/rotation/split in the review UI — commit final
page images named to match the existing pipeline's filename convention
(IMG_<date>_<page>_<side>.tif, see layout.FILENAME_PATTERN) so they can be
handed to pipeline.run_book() completely unchanged."""

import datetime
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import cv2
import gdown
import numpy as np
import pymupdf as fitz
import requests

from lipisampada import page_ops

RASTER_DPI = 300
# A single book page is portrait or near-square; a two-page spread
# photographed/scanned as one image is noticeably wider than tall.
SPREAD_ASPECT_THRESHOLD = 1.15


_DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([\w-]+)"),
    re.compile(r"[?&]id=([\w-]+)"),
]


def extract_drive_file_id(link: str) -> str | None:
    for pattern in _DRIVE_ID_PATTERNS:
        m = pattern.search(link)
        if m:
            return m.group(1)
    return None


def source_kind(link: str | None) -> str:
    """Where a catalog link points, i.e. how it can be previewed/downloaded:
    drive | archive (archive.org /details/<id>) | direct_pdf (any http URL
    ending in .pdf) | unsupported."""
    if not link or not link.startswith("http"):
        return "unsupported"
    host = urlparse(link).netloc.lower()
    if "drive.google.com" in host and extract_drive_file_id(link):
        return "drive"
    if host.endswith("archive.org") and _archive_id(link):
        return "archive"
    if urlparse(link).path.lower().endswith(".pdf"):
        return "direct_pdf"
    return "unsupported"


def _archive_id(link: str) -> str | None:
    m = re.search(r"archive\.org/(?:details|embed|download)/([^/?#]+)", link)
    return m.group(1) if m else None


def preview_url(link: str | None) -> str | None:
    """An <iframe>-embeddable viewer URL for the link, no download needed, so
    browsing the catalog and previewing before queuing stays cheap."""
    kind = source_kind(link)
    if kind == "drive":
        return f"https://drive.google.com/file/d/{extract_drive_file_id(link)}/preview"
    if kind == "archive":
        return f"https://archive.org/embed/{_archive_id(link)}"
    if kind == "direct_pdf":
        return link
    return None


drive_preview_url = preview_url  # older name, kept so existing callers/tests keep working


def _check_pdf(path: Path, source: str) -> Path:
    with open(path, "rb") as f:
        head = f.read(1024)
    if b"%PDF" not in head:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"{source} did not return a PDF (got an error page or a restricted item)")
    return path


def _stream_to_file(url: str, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=(15, 120), allow_redirects=True) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def _archive_pdf_url(identifier: str) -> str:
    """archive.org names the PDF inside each item; ask its metadata API which
    file it is instead of assuming <id>.pdf."""
    r = requests.get(f"https://archive.org/metadata/{identifier}", timeout=30)
    r.raise_for_status()
    files = (r.json() or {}).get("files") or []
    pdfs = [f["name"] for f in files if f.get("name", "").lower().endswith(".pdf")]
    if not pdfs:
        raise RuntimeError(f"archive.org item {identifier} has no downloadable PDF (lending-only or removed)")
    # prefer the item's own scan over derivative copies when several exist
    name = next((n for n in pdfs if n == f"{identifier}.pdf"), pdfs[0])
    return f"https://archive.org/download/{identifier}/{requests.utils.quote(name)}"


def download_pdf(prati_link: str, dest_path: Path) -> Path:
    """Downloads the book's PDF from wherever its link points: a public Google
    Drive file (via gdown, which handles Drive's confirmation-token redirect),
    an archive.org item, or a plain .pdf URL."""
    kind = source_kind(prati_link)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "drive":
        file_id = extract_drive_file_id(prati_link)
        result = gdown.download(id=file_id, output=str(dest_path), quiet=False)
        if result is None:
            raise RuntimeError(f"Could not download PDF from {prati_link} (not publicly accessible, or link expired)")
        return _check_pdf(Path(result), prati_link)
    if kind == "archive":
        _stream_to_file(_archive_pdf_url(_archive_id(prati_link)), dest_path)
        return _check_pdf(dest_path, prati_link)
    if kind == "direct_pdf":
        _stream_to_file(prati_link, dest_path)
        return _check_pdf(dest_path, prati_link)
    raise RuntimeError(f"Unsupported link (not Drive, archive.org or a PDF URL): {prati_link}")


def rasterize_pdf(pdf_path: Path, out_dir: Path, dpi: int = RASTER_DPI) -> list[Path]:
    """Renders every page of the PDF to a PNG at the given DPI. Returns the
    raster paths in page order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0  # PDF points are 1/72in; fitz's default render is 72 DPI
    matrix = fitz.Matrix(zoom, zoom)
    paths = []
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            raster_path = out_dir / f"raw_{i:04d}.png"
            pix.save(str(raster_path))
            paths.append(raster_path)
    return paths


def mint_filename(intake_date: datetime.date, page_number: int, side: str) -> str:
    """Matches layout.FILENAME_PATTERN (IMG_<8-digit-date>_<page>_<side>)
    so finished pages feed pipeline.process_page unchanged. side is "P" for
    an ordinary single page, "L"/"R" for a split spread's two halves."""
    return f"IMG_{intake_date.strftime('%Y%m%d')}_{page_number:04d}_{side}.tif"


def commit_regions(
    original_path: Path,
    work_dir: Path,
    page_number: int,
    rot90: int,
    regions: list[dict],
    intake_date: datetime.date | None = None,
    dewarped: dict[int, "np.ndarray"] | None = None,
) -> list[Path]:
    """Renders every approved crop of a page (rotate -> warp/straighten -> optional
    auto-dewarp -> brightness/contrast/binarize) and writes each as its own page image
    named like the rest of the pipeline expects (IMG_<date>_<page>_<side>.tif): one crop
    is side P, two are L/R, more are A, B, C... in the order the reviewer arranged them.
    Returns the written paths in that order."""
    intake_date = intake_date or datetime.date.today()
    img = cv2.imread(str(original_path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"cannot read {original_path}")
    img = page_ops.rotate90(img, rot90)
    work_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for i, (region, side) in enumerate(zip(regions, page_ops.side_letters(len(regions)))):
        out = page_ops.apply_region(img, region, (dewarped or {}).get(i))
        path = work_dir / mint_filename(intake_date, page_number, side)
        cv2.imwrite(str(path), out)
        written.append(path)
    return written


def legacy_to_regions(w: int, h: int, crop_box, rotation: float, split: bool, split_x) -> list[dict]:
    """Old editor payload (one axis-aligned box in an image rotated by `rotation` degrees,
    optionally split at split_x) -> regions on the original image, so an old browser tab
    still works against the new API."""
    x0, y0, x1, y1 = crop_box if crop_box else (0, 0, w, h)
    boxes = [(x0, y0, x1, y1)]
    if split:
        sx = split_x if split_x is not None else (x0 + x1) / 2
        boxes = [(x0, y0, sx, y1), (sx, y0, x1, y1)]
    inv = cv2.invertAffineTransform(cv2.getRotationMatrix2D((w // 2, h // 2), rotation, 1.0)) if rotation else None
    regions = []
    for bx0, by0, bx1, by1 in boxes:
        quad = []
        for x, y in ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)):
            if inv is not None:
                x, y = (inv @ np.array([x, y, 1.0])).tolist()
            quad.append([x, y])
        regions.append(page_ops.neutral_region(quad))
    return regions


def copy_pages_preview(approved_dir: Path, pages_dir: Path) -> list[Path]:
    """Copies every approved page's TIF into pages_dir as a PNG, named to
    match what pipeline.process_page will later write there itself (same
    stem) - so the review app / a person browsing the run dir can see every
    page immediately once OCR starts, rather than waiting for the (slow)
    OCR loop to reach each one. process_page's own write later overwrites
    each file with the further-preprocessed (denoised/deskewed) version;
    no wasted OCR work, just earlier visibility."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for tif_path in sorted(approved_dir.glob("*.tif")):
        img = cv2.imread(str(tif_path))
        if img is None:
            continue
        out_path = pages_dir / f"{tif_path.stem}.png"
        if not out_path.exists():  # on a resumed run, keep the already-processed version
            cv2.imwrite(str(out_path), img)
        written.append(out_path)
    return written
