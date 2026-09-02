"""Column/paragraph slicer: splits a page into ordered paragraph snippets
using vertical/horizontal projection profiles of the binarized page."""

import re
from dataclasses import dataclass

import numpy as np

# IMG_20220327_0047_1L.tif -> page 47, side "1L"
FILENAME_PATTERN = re.compile(r"IMG_(?P<date>\d{8})_(?P<page>\d+)_(?P<side>\w+)")


@dataclass
class PageMeta:
    book_id: str
    page_number: int
    side: str


@dataclass
class Snippet:
    bbox: tuple[int, int, int, int]  # (x0, y0, x1, y1) in page-pixel coords
    paragraph_sequence: int
    column_index: int


def parse_filename(filename: str, book_id: str = "Prasanga") -> PageMeta:
    match = FILENAME_PATTERN.search(filename)
    if not match:
        return PageMeta(book_id=book_id, page_number=0, side="")
    return PageMeta(
        book_id=book_id,
        page_number=int(match.group("page")),
        side=match.group("side"),
    )


def _find_ranges(profile: np.ndarray, min_gap: int, min_run: int) -> list[tuple[int, int]]:
    """Find contiguous runs of "ink present" (profile > 0) in a 1D
    projection profile, merging runs separated by gaps smaller than
    min_gap, and dropping runs shorter than min_run."""
    ink = profile > 0
    ranges: list[tuple[int, int]] = []
    start = None
    gap = 0
    for i, has_ink in enumerate(ink):
        if has_ink:
            if start is None:
                start = i
            gap = 0
        else:
            if start is not None:
                gap += 1
                if gap > min_gap:
                    end = i - gap
                    if end - start >= min_run:
                        ranges.append((start, end))
                    start = None
                    gap = 0
    if start is not None:
        end = len(ink) - gap
        if end - start >= min_run:
            ranges.append((start, end))
    return ranges


def find_columns(binary: np.ndarray, min_gap: int = 15, min_width: int = 40) -> list[tuple[int, int]]:
    """Vertical projection profile: sum ink (0-valued pixels) per column
    to find text columns separated by whitespace gutters."""
    ink_mask = (binary == 0).astype(np.uint8)
    col_profile = ink_mask.sum(axis=0)
    return _find_ranges(col_profile, min_gap=min_gap, min_run=min_width)


def find_paragraphs(binary: np.ndarray, x0: int, x1: int, min_gap: int = 8, min_height: int = 15) -> list[tuple[int, int]]:
    """Horizontal projection profile within one column to find paragraph
    (or line-block) boundaries."""
    column = binary[:, x0:x1]
    ink_mask = (column == 0).astype(np.uint8)
    row_profile = ink_mask.sum(axis=1)
    return _find_ranges(row_profile, min_gap=min_gap, min_run=min_height)


def slice_page(binary: np.ndarray) -> list[Snippet]:
    """Returns ordered paragraph snippets: left-to-right by column, then
    top-to-bottom within each column."""
    snippets: list[Snippet] = []
    seq = 0
    columns = find_columns(binary)
    for col_idx, (x0, x1) in enumerate(columns):
        paragraphs = find_paragraphs(binary, x0, x1)
        for y0, y1 in paragraphs:
            snippets.append(
                Snippet(bbox=(x0, y0, x1, y1), paragraph_sequence=seq, column_index=col_idx)
            )
            seq += 1
    return snippets


def crop_snippet(image: np.ndarray, snippet: Snippet, padding: int = 4) -> np.ndarray:
    x0, y0, x1, y1 = snippet.bbox
    h, w = image.shape[:2]
    x0 = max(0, x0 - padding)
    y0 = max(0, y0 - padding)
    x1 = min(w, x1 + padding)
    y1 = min(h, y1 + padding)
    return image[y0:y1, x0:x1]
