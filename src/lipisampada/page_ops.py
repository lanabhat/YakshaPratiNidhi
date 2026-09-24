"""Image operations behind the intake page editor (no web code here).

A page is edited as a list of *regions*. A region is
    {"quad":    [[x, y] * 4]            corners TL, TR, BR, BL, in image pixels (free-form)
     "mids":    [[dx, dy] * 4] | None   offsets of the top/right/bottom/left edge midpoints
                                        from the straight chord; non-zero bends that edge
     "enhance": {...}                   brightness / contrast / binarize, see DEFAULT_ENHANCE
     "dewarp":  "none" | "auto"}        "auto" = use a page-dewarp result for this quad

Processing order: rotate page by rot90 -> warp region flat -> (auto dewarp) ->
enhance. What comes out of apply_region is exactly what OCR will read."""

import cv2
import numpy as np

MAX_REGIONS = 8
CURVE_SAMPLES = 48
DEFAULT_ENHANCE = {"brightness": 0, "contrast": 1.0, "binarize": "off", "threshold": 128, "block": 35, "c": 15}
BINARIZE_MODES = ("off", "global", "otsu", "adaptive")


# ---------------------------------------------------------------- helpers
def rotate90(img: np.ndarray, degrees: int) -> np.ndarray:
    degrees %= 360
    if degrees == 0:
        return img
    code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(degrees)
    if code is None:
        raise ValueError("rot90 must be 0, 90, 180 or 270")
    return cv2.rotate(img, code)


def order_quad(pts) -> list[list[float]]:
    """Any four points -> [TL, TR, BR, BL]."""
    p = sorted(np.asarray(pts, dtype=float).reshape(4, 2).tolist(), key=lambda q: q[1])
    top, bottom = sorted(p[:2], key=lambda q: q[0]), sorted(p[2:], key=lambda q: q[0])
    return [top[0], top[1], bottom[1], bottom[0]]


def full_quad(w: int, h: int) -> list[list[float]]:
    return [[0.0, 0.0], [w - 1.0, 0.0], [w - 1.0, h - 1.0], [0.0, h - 1.0]]


def neutral_region(quad, kind: str | None = None) -> dict:
    r = {"quad": [list(map(float, p)) for p in quad], "mids": None, "enhance": dict(DEFAULT_ENHANCE), "dewarp": "none"}
    if kind:
        r["kind"] = kind
    return r


def side_letters(n: int) -> list[str]:
    """Filename 'side' for each of n regions of one page: a single crop is the
    page itself (P), two are left/right (as spreads always were), more are A, B, C..."""
    if n == 1:
        return ["P"]
    if n == 2:
        return ["L", "R"]
    return [chr(ord("A") + i) for i in range(n)]


def validate_regions(regions: list[dict], w: int, h: int) -> list[dict]:
    if not regions:
        raise ValueError("a page needs at least one crop")
    if len(regions) > MAX_REGIONS:
        raise ValueError(f"at most {MAX_REGIONS} crops per page")
    out = []
    for r in regions:
        quad = r.get("quad")
        if not quad or len(quad) != 4 or any(len(p) != 2 for p in quad):
            raise ValueError("each crop needs four corner points")
        pts = np.asarray(quad, dtype=float)
        if not np.isfinite(pts).all() or pts.min() < -w or pts.max() > 2 * max(w, h):
            raise ValueError("crop corner is outside the image")
        area = 0.5 * abs(np.dot(pts[:, 0], np.roll(pts[:, 1], -1)) - np.dot(pts[:, 1], np.roll(pts[:, 0], -1)))
        if area < 100:
            raise ValueError("crop is too small")
        mids = r.get("mids")
        if mids is not None and (len(mids) != 4 or any(len(m) != 2 for m in mids)):
            raise ValueError("mids must be four [dx, dy] offsets")
        enh = {**DEFAULT_ENHANCE, **(r.get("enhance") or {})}
        if enh["binarize"] not in BINARIZE_MODES:
            raise ValueError("unknown binarize mode")
        out.append({"quad": pts.tolist(), "mids": mids, "enhance": enh,
                    "dewarp": r.get("dewarp", "none"), **({"kind": r["kind"]} if "kind" in r else {})})
    return out


# ---------------------------------------------------------------- geometry
def _bezier(p0, p1, offset, t):
    """Quadratic curve from p0 to p1 that passes through chord-midpoint + offset at t = 0.5."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    mid = (p0 + p1) / 2 + np.asarray(offset, float)
    ctrl = 2 * mid - (p0 + p1) / 2
    t = np.asarray(t, float)[:, None]
    return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * ctrl + t ** 2 * p1


def _edges(quad, mids):
    tl, tr, br, bl = [np.asarray(p, float) for p in quad]
    m = mids if mids is not None else [[0, 0]] * 4
    return (tl, tr, br, bl), (m[0], m[1], m[2], m[3])  # offsets: top, right, bottom, left


def is_curved(mids, eps: float = 0.5) -> bool:
    return mids is not None and any(abs(v) > eps for m in mids for v in m)


def output_size(quad, mids) -> tuple[int, int]:
    (tl, tr, br, bl), (mt, mr, mb, ml) = _edges(quad, mids)
    t = np.linspace(0, 1, CURVE_SAMPLES)

    def length(a, b, off):
        c = _bezier(a, b, off, t)
        return float(np.linalg.norm(np.diff(c, axis=0), axis=1).sum())

    w = (length(tl, tr, mt) + length(bl, br, mb)) / 2
    h = (length(tl, bl, ml) + length(tr, br, mr)) / 2
    return max(2, int(round(w))), max(2, int(round(h)))


def coons_maps(quad, mids, w: int, h: int):
    """Sampling maps sending an output rectangle onto the quad whose four edges are
    (possibly curved) - a Coons patch: blend of the boundary curves minus the bilinear corners."""
    (tl, tr, br, bl), (mt, mr, mb, ml) = _edges(quad, mids)
    u = np.linspace(0, 1, w)
    v = np.linspace(0, 1, h)
    top, bottom = _bezier(tl, tr, mt, u), _bezier(bl, br, mb, u)      # (w, 2), indexed by u
    left, right = _bezier(tl, bl, ml, v), _bezier(tr, br, mr, v)      # (h, 2), indexed by v
    U, V = u[None, :, None], v[:, None, None]
    p = (
        (1 - V) * top[None, :, :] + V * bottom[None, :, :]
        + (1 - U) * left[:, None, :] + U * right[:, None, :]
        - ((1 - U) * (1 - V) * tl + U * (1 - V) * tr + (1 - U) * V * bl + U * V * br)
    )
    return p[..., 0].astype(np.float32), p[..., 1].astype(np.float32)


def warp_region(img: np.ndarray, quad, mids=None, force_coons: bool = False) -> np.ndarray:
    """Flatten the quad (straight or curved edges) into an upright rectangle."""
    w, h = output_size(quad, mids)
    if not force_coons and not is_curved(mids):
        src = np.asarray(quad, np.float32)
        dst = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
        return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
    mx, my = coons_maps(quad, mids, w, h)
    return cv2.remap(img, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


# ---------------------------------------------------------------- enhancement
def enhance(img: np.ndarray, cfg: dict | None) -> np.ndarray:
    """brightness (-100..100 added), contrast (multiplier about mid-grey), then optional binarize."""
    c = {**DEFAULT_ENHANCE, **(cfg or {})}
    out = img
    if c["brightness"] or c["contrast"] != 1.0:
        out = np.clip((img.astype(np.float32) - 128.0) * float(c["contrast"]) + 128.0 + float(c["brightness"]), 0, 255).astype(np.uint8)
    mode = c["binarize"]
    if mode != "off":
        gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) if out.ndim == 3 else out
        if mode == "global":
            _, bw = cv2.threshold(gray, int(c["threshold"]), 255, cv2.THRESH_BINARY)
        elif mode == "otsu":
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            block = max(3, int(c["block"]) | 1)
            bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, float(c["c"]))
        out = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    return out


def otsu_threshold(img: np.ndarray) -> int:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    t, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return int(t)


def apply_region(img_rotated: np.ndarray, region: dict, dewarped: np.ndarray | None = None) -> np.ndarray:
    """warp -> (auto-dewarped image replaces the warp result) -> enhance."""
    flat = dewarped if (dewarped is not None and region.get("dewarp") == "auto") else warp_region(
        img_rotated, region["quad"], region.get("mids"))
    return enhance(flat, region.get("enhance"))


def scale_region(region: dict, s: float) -> dict:
    """Region expressed for an image scaled by s (used to work on a small preview copy)."""
    r = dict(region)
    r["quad"] = [[x * s, y * s] for x, y in region["quad"]]
    if region.get("mids") is not None:
        r["mids"] = [[dx * s, dy * s] for dx, dy in region["mids"]]
    return r


# ---------------------------------------------------------------- auto crop
def _paper_quad(gray_small: np.ndarray):
    """Outline of a sheet of paper photographed on a different background, or None."""
    h, w = gray_small.shape
    blur = cv2.GaussianBlur(gray_small, (7, 7), 0)
    for invert in (False, True):
        _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU if invert else cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        c = max(contours, key=cv2.contourArea)
        frac = cv2.contourArea(c) / float(w * h)
        if not 0.35 <= frac <= 0.97:  # >0.97: the sheet fills the frame (a clean PDF render), nothing to detect
            continue
        # A sheet photographed on a desk sits inside the frame. If the bright region spans the frame
        # on two or more sides it is the page itself filling the picture (dark scanner patches
        # notch it), not a sheet on a background.
        bx, by, bw, bh = cv2.boundingRect(c)
        m = max(2, int(0.01 * max(w, h)))
        if sum((bx <= m, by <= m, bx + bw >= w - m, by + bh >= h - m)) >= 2:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return order_quad(approx.reshape(4, 2))
        hull = cv2.convexHull(c)  # rounded/curled corners: fall back to the tightest 4-sided fit
        box = cv2.boxPoints(cv2.minAreaRect(hull))
        return order_quad(box)
    return None


def _text_skew_degrees(pts: np.ndarray) -> float:
    """Tilt of the text lines: the angle (within +-4 deg) at which ink points pile up
    into the sharpest horizontal rows. Far steadier than fitting a rectangle to ragged text."""
    if len(pts) > 30000:
        pts = pts[np.random.default_rng(0).choice(len(pts), 30000, replace=False)]
    c = pts.mean(axis=0)
    p = pts - c

    def score(deg):
        t = np.deg2rad(deg)
        y = -p[:, 0] * np.sin(t) + p[:, 1] * np.cos(t)
        hist, _ = np.histogram(y, bins=max(20, int((y.max() - y.min()) / 3)))
        return float((hist.astype(np.float64) ** 2).sum())

    coarse = max(np.linspace(-4, 4, 33), key=score)
    best = max(np.linspace(coarse - 0.25, coarse + 0.25, 11), key=score)
    return 0.0 if abs(best) < 0.15 else float(best)


def _ink_quad(gray_small: np.ndarray):
    """Tight, skew-corrected box around the text. Ink touching the image border is ignored:
    that is scanner shadow / page edge, not text."""
    h, w = gray_small.shape
    binary = cv2.adaptiveThreshold(cv2.GaussianBlur(gray_small, (3, 3), 0), 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 15)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    merged = cv2.dilate(binary, cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 60), max(5, h // 120))))
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    big = [c for c in contours if cv2.contourArea(c) > 0.0004 * w * h]        # drop dust
    m = max(2, int(0.006 * max(w, h)))

    def touches_border(c):
        x, y, bw, bh = cv2.boundingRect(c)
        return x <= m or y <= m or x + bw >= w - m or y + bh >= h - m

    keep = [c for c in big if not touches_border(c)] or big
    if not keep:
        return None
    mask = np.zeros_like(binary)
    cv2.drawContours(mask, keep, -1, 255, -1)
    ys, xs = np.nonzero(binary & mask)
    if len(xs) < 50:
        return None
    pts = np.column_stack([xs, ys]).astype(np.float64)
    deg = _text_skew_degrees(pts)
    t = np.deg2rad(deg)
    c0 = pts.mean(axis=0)
    rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])         # into the deskewed frame
    q = (pts - c0) @ rot                                                       # rows: (x', y') with y' along the text lines
    lo, hi = np.percentile(q, 0.2, axis=0), np.percentile(q, 99.8, axis=0)     # ignore a few stray specks
    pad = 0.012 * max(w, h)
    lo, hi = lo - pad, hi + pad
    corners = np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]])
    back = corners @ rot.T + c0
    return np.clip(back, [0, 0], [w - 1, h - 1]).tolist()


def auto_suggest(img_bgr: np.ndarray) -> list[dict]:
    """One suggested region for a page image: the paper outline for a phone photo,
    else a tight, skew-corrected box around the text, else the whole image."""
    h, w = img_bgr.shape[:2]
    s = min(1.0, 1200.0 / max(h, w))
    small = cv2.resize(img_bgr, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA) if s < 1 else img_bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    for kind, finder in (("paper", _paper_quad), ("ink", _ink_quad)):
        q = finder(gray)
        if q is not None:
            return [neutral_region([[x / s, y / s] for x, y in q], kind)]
    return [neutral_region(full_quad(w, h), "full")]


def looks_like_spread(regions: list[dict]) -> bool:
    """A page whose auto crop is clearly wider than tall is probably a two-page spread."""
    w, h = output_size(regions[0]["quad"], None)
    return h > 0 and (w / h) > 1.15


# ---------------------------------------------------------------- automatic dewarp
def auto_dewarp(flat_bgr: np.ndarray, timeout: float = 150.0) -> np.ndarray:
    """Text-line based dewarp via the optional open-source `page-dewarp` package, run in its own
    process (see dewarp_worker.py: its optimizer can occasionally diverge and abort the interpreter).

    It is a best guess, not a promise: on real text pages it usually flattens curled lines but it is
    unstable, and its margin sensitivity is why several margins are tried in turn. The result is
    rescaled to the input's resolution (its own output size is arbitrary). Raises RuntimeError with a
    reason a person can act on; callers should let the reviewer see the result and accept or drop it."""
    import os
    import subprocess
    import sys
    import tempfile
    import time
    from pathlib import Path

    pkg_root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(x for x in (pkg_root, os.environ.get("PYTHONPATH", "")) if x)}
    deadline = time.time() + timeout
    last = "no attempt finished"
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.png", Path(tmp) / "out.png"
        cv2.imwrite(str(src), flat_bgr)
        for pad in (0.06, 0.12, 0.0):
            remaining = deadline - time.time()
            if remaining < 5:
                break
            dst.unlink(missing_ok=True)
            try:
                r = subprocess.run([sys.executable, "-m", "lipisampada.dewarp_worker", str(src), str(dst), "--pad", str(pad)],
                                   env=env, capture_output=True, text=True, timeout=remaining)
            except subprocess.TimeoutExpired:
                last = "it took too long"
                break
            if r.returncode == 3:
                raise RuntimeError("automatic dewarp needs the optional package: pip install page-dewarp")
            if r.returncode == 0 and dst.exists():
                out = cv2.imread(str(dst))
                if out is not None:
                    k = max(flat_bgr.shape[:2]) / max(out.shape[:2])
                    if abs(k - 1) > 0.05:
                        out = cv2.resize(out, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
                    return out
            lines = r.stderr.strip().splitlines()
            last = lines[-1][:80] if lines else f"exit code {r.returncode}"
    raise RuntimeError(f"automatic dewarp could not straighten this page ({last}) - use the curve handles instead")
