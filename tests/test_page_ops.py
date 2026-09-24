import cv2
import numpy as np
import pytest

from lipisampada import page_ops as po


def text_page(w=900, h=1200, bg=245):
    """A page of horizontal 'text lines' (thick dark bars with gaps) - easy to measure straightness on."""
    img = np.full((h, w, 3), bg, np.uint8)
    for y in range(120, h - 100, 60):
        for x in range(90, w - 90, 140):
            cv2.rectangle(img, (x, y), (x + 110, y + 18), (20, 20, 20), -1)
    return img


def row_profile_sharpness(img):
    """Higher = text lines are more horizontal / better aligned."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float(g.sum(axis=1).std())


# ------------------------------------------------------------ geometry
def test_order_quad_any_input_order():
    pts = [[100, 20], [10, 200], [10, 10], [110, 210]]
    assert po.order_quad(pts) == [[10, 10], [100, 20], [110, 210], [10, 200]]


def test_perspective_warp_undoes_a_known_homography():
    flat = text_page()
    h, w = flat.shape[:2]
    src = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    dst = np.float32([[80, 40], [w + 60, 10], [w - 20, h + 90], [30, h + 30]])  # a slanted, keystoned photo of the page
    H = cv2.getPerspectiveTransform(src, dst)
    photo = cv2.warpPerspective(flat, H, (w + 200, h + 200), borderValue=(60, 60, 60))
    back = po.warp_region(photo, dst.tolist())
    back = cv2.resize(back, (w, h))
    err = np.abs(back.astype(int) - flat.astype(int)).mean()
    assert err < 12, err  # residual is interpolation blur on thin bars, not geometry


def test_left_edge_can_be_slanted_independently():
    """The user's case: only the left side leans. Moving just the two left corners must square it up."""
    flat = text_page()
    h, w = flat.shape[:2]
    src = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    dst = np.float32([[140, 40], [w + 100, 40], [w + 100, h + 40], [40, h + 40]])  # only the left corners differ: TL right 100? BL left 100
    dst = np.float32([[60 + 90, 40], [w + 100, 40], [w + 100, h + 40], [60 - 30, h + 40]])  # left edge leans, right edge vertical
    photo = cv2.warpPerspective(flat, cv2.getPerspectiveTransform(src, dst), (w + 220, h + 120), borderValue=(80, 80, 80))
    back = cv2.resize(po.warp_region(photo, dst.tolist()), (w, h))
    assert np.abs(back.astype(int) - flat.astype(int)).mean() < 12


def test_coons_with_straight_edges_matches_perspective_on_a_parallelogram():
    img = text_page()
    q = [[100, 100], [700, 130], [730, 900], [130, 870]]  # parallelogram: bilinear == projective exactly
    a = po.warp_region(img, q)
    b = po.warp_region(img, q, force_coons=True)
    assert a.shape == b.shape
    assert np.abs(a.astype(int) - b.astype(int)).mean() < 3


def test_zero_mids_are_not_treated_as_curved():
    assert not po.is_curved([[0, 0]] * 4) and not po.is_curved(None) and po.is_curved([[0, 9], [0, 0], [0, 0], [0, 0]])


def test_curve_handles_straighten_a_bent_page():
    """A page whose lines bow like a sine (the classic phone-photo curl): dragging the top and bottom
    midpoint handles onto the bowed edges must flatten it."""
    flat = text_page()
    h, w = flat.shape[:2]
    A = 55.0
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # bowed photo: the content at (x, y) appears at y + A*sin(pi x / w)
    bowed = cv2.remap(flat, xx, yy - A * np.sin(np.pi * xx / w).astype(np.float32), cv2.INTER_LINEAR, borderValue=(245, 245, 245))
    pad = 120
    canvas = cv2.copyMakeBorder(bowed, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(245, 245, 245))
    quad = [[pad, pad], [pad + w - 1, pad], [pad + w - 1, pad + h - 1], [pad, pad + h - 1]]
    straight = po.warp_region(canvas, quad)
    bent = po.warp_region(canvas, quad, [[0, A], [0, 0], [0, A], [0, 0]])  # top and bottom edges bow down by A at the middle
    assert row_profile_sharpness(bent) > 1.5 * row_profile_sharpness(straight)
    assert bent.shape[0] == pytest.approx(h, abs=6) and w <= bent.shape[1] <= w * 1.03  # a bowed edge is a little longer than its chord


def test_output_size_uses_curve_length():
    q = [[0, 0], [100, 0], [100, 50], [0, 50]]
    w0, _ = po.output_size(q, None)
    w1, _ = po.output_size(q, [[0, 30], [0, 0], [0, 30], [0, 0]])
    assert w0 == 100 and w1 > w0


# ------------------------------------------------------------ enhancement
def grey(v, n=8):
    return np.full((n, n, 3), v, np.uint8)


def test_brightness_and_contrast_math():
    assert po.enhance(grey(100), {"brightness": 50})[0, 0, 0] == 150
    assert po.enhance(grey(100), {"contrast": 2.0})[0, 0, 0] == 72          # (100-128)*2+128
    assert po.enhance(grey(200), {"brightness": 100})[0, 0, 0] == 255        # clipped, not wrapped
    assert (po.enhance(grey(90), {}) == grey(90)).all()                       # neutral is a no-op


def test_binarize_modes_only_produce_black_and_white():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (60, 60, 3), dtype=np.uint8)
    for mode in ("global", "otsu", "adaptive"):
        out = po.enhance(img, {"binarize": mode})
        assert set(np.unique(out)) <= {0, 255} and out.shape == img.shape
    assert (po.enhance(grey(100), {"binarize": "global", "threshold": 128}) == 0).all()
    assert (po.enhance(grey(200), {"binarize": "global", "threshold": 128}) == 255).all()


def test_otsu_threshold_splits_two_populations():
    img = np.concatenate([grey(40, 10), grey(210, 10)], axis=0)
    assert 40 <= po.otsu_threshold(img) <= 210


def test_scale_region_scales_quad_and_mids():
    r = po.neutral_region([[10, 20], [110, 20], [110, 220], [10, 220]])
    r["mids"] = [[0, 8], [0, 0], [0, 8], [0, 0]]
    s = po.scale_region(r, 0.5)
    assert s["quad"][1] == [55, 10] and s["mids"][0] == [0, 4]


# ------------------------------------------------------------ auto crop
def test_auto_suggest_finds_a_tilted_sheet_on_a_dark_desk():
    desk = np.full((1400, 1000, 3), 55, np.uint8)
    truth = np.float32([[170, 120], [860, 190], [820, 1240], [120, 1180]])
    sheet = np.full((1100, 700, 3), 235, np.uint8)
    sheet[100:1000:60, 80:620] = (25, 25, 25)
    H = cv2.getPerspectiveTransform(np.float32([[0, 0], [699, 0], [699, 1099], [0, 1099]]), truth)
    photo = np.where(cv2.warpPerspective(np.full_like(sheet, 255), H, (1000, 1400)) > 0,
                     cv2.warpPerspective(sheet, H, (1000, 1400)), desk)
    (region,) = po.auto_suggest(photo)
    assert region["kind"] == "paper"
    err = np.abs(np.asarray(region["quad"]) - truth).max()
    assert err < 45, err  # within ~3% of the frame


def test_auto_suggest_on_clean_page_gives_a_tight_text_box():
    page = np.full((1600, 1100, 3), 255, np.uint8)
    for y in range(300, 1300, 55):
        cv2.rectangle(page, (250, y), (900, y + 20), (0, 0, 0), -1)
    (region,) = po.auto_suggest(page)
    assert region["kind"] == "ink"
    q = np.asarray(region["quad"])
    assert 200 <= q[:, 0].min() <= 260 and 890 <= q[:, 0].max() <= 950     # around the text, with a little margin
    assert 250 <= q[:, 1].min() <= 310 and 1290 <= q[:, 1].max() <= 1350


def test_auto_suggest_corrects_a_slightly_rotated_text_block():
    page = np.full((1600, 1100, 3), 255, np.uint8)
    for y in range(300, 1300, 55):
        cv2.rectangle(page, (250, y), (900, y + 20), (0, 0, 0), -1)
    M = cv2.getRotationMatrix2D((550, 800), 3.0, 1.0)
    rotated = cv2.warpAffine(page, M, (1100, 1600), borderValue=(255, 255, 255))
    (region,) = po.auto_suggest(rotated)
    flat = po.warp_region(rotated, region["quad"])
    assert row_profile_sharpness(flat) > 0.9 * row_profile_sharpness(po.warp_region(page, po.auto_suggest(page)[0]["quad"]))


def test_blank_page_falls_back_to_the_whole_image():
    (region,) = po.auto_suggest(np.full((500, 400, 3), 255, np.uint8))
    assert region["kind"] == "full" and region["quad"][2] == [399.0, 499.0]


def test_looks_like_spread_uses_aspect_ratio():
    wide = [po.neutral_region([[0, 0], [1600, 0], [1600, 1000], [0, 1000]])]
    tall = [po.neutral_region([[0, 0], [1000, 0], [1000, 1400], [0, 1400]])]
    assert po.looks_like_spread(wide) and not po.looks_like_spread(tall)


# ------------------------------------------------------------ regions / naming
def test_side_letters():
    assert po.side_letters(1) == ["P"] and po.side_letters(2) == ["L", "R"] and po.side_letters(4) == ["A", "B", "C", "D"]


def test_validate_regions_rejects_bad_input_and_fills_defaults():
    ok = po.validate_regions([{"quad": [[0, 0], [100, 0], [100, 100], [0, 100]]}], 500, 500)
    assert ok[0]["enhance"]["binarize"] == "off" and ok[0]["dewarp"] == "none"
    with pytest.raises(ValueError, match="at least one"):
        po.validate_regions([], 500, 500)
    with pytest.raises(ValueError, match="four corner"):
        po.validate_regions([{"quad": [[0, 0], [1, 1]]}], 500, 500)
    with pytest.raises(ValueError, match="too small"):
        po.validate_regions([{"quad": [[0, 0], [2, 0], [2, 2], [0, 2]]}], 500, 500)
    with pytest.raises(ValueError, match="outside"):
        po.validate_regions([{"quad": [[0, 0], [99999, 0], [99999, 100], [0, 100]]}], 500, 500)
    with pytest.raises(ValueError, match="binarize"):
        po.validate_regions([{"quad": [[0, 0], [100, 0], [100, 100], [0, 100]], "enhance": {"binarize": "magic"}}], 500, 500)
    with pytest.raises(ValueError, match="at most"):
        po.validate_regions([{"quad": [[0, 0], [100, 0], [100, 100], [0, 100]]}] * 9, 500, 500)


def test_apply_region_runs_warp_then_enhance():
    img = text_page()
    r = po.neutral_region([[90, 100], [810, 100], [810, 1100], [90, 1100]])
    r["enhance"] = {**po.DEFAULT_ENHANCE, "binarize": "global", "threshold": 128}
    out = po.apply_region(img, r)
    assert set(np.unique(out)) <= {0, 255} and out.shape[0] == pytest.approx(1000, abs=2)


def test_rotate90():
    img = np.zeros((10, 20, 3), np.uint8)
    assert po.rotate90(img, 90).shape[:2] == (20, 10) and po.rotate90(img, 180).shape[:2] == (10, 20) and po.rotate90(img, 0) is img
    with pytest.raises(ValueError):
        po.rotate90(img, 45)


def test_auto_suggest_ignores_dark_scanner_edges_touching_the_border():
    page = np.full((1600, 1100, 3), 250, np.uint8)
    cv2.rectangle(page, (0, 0), (500, 90), (15, 15, 15), -1)          # dark scanner-bed patch on the top-left edge
    cv2.rectangle(page, (0, 1450), (1100, 1600), (15, 15, 15), -1)     # and along the bottom
    for y in range(300, 1300, 55):
        cv2.rectangle(page, (250, y), (900, y + 20), (0, 0, 0), -1)
    (region,) = po.auto_suggest(page)
    q = np.asarray(region["quad"])
    assert q[:, 1].min() > 200 and q[:, 1].max() < 1400, q.tolist()


def test_auto_suggest_estimates_text_tilt_accurately():
    page = np.full((1600, 1100, 3), 255, np.uint8)
    for y in range(300, 1300, 55):
        cv2.rectangle(page, (250, y), (900, y + 20), (0, 0, 0), -1)
    for deg in (-2.5, 1.0, 3.0):
        tilted = cv2.warpAffine(page, cv2.getRotationMatrix2D((550, 800), deg, 1.0), (1100, 1600), borderValue=(255, 255, 255))
        (region,) = po.auto_suggest(tilted)
        tl, tr = np.asarray(region["quad"][0]), np.asarray(region["quad"][1])
        measured = np.degrees(np.arctan2(tr[1] - tl[1], tr[0] - tl[0]))
        assert abs(measured + deg) < 0.6 or abs(measured - deg) < 0.6, (deg, measured)     # top edge runs along the text lines


# ------------------------------------------------------------ auto dewarp (worker is faked; real run is manual)
class _Run:
    def __init__(self, code, stderr=""):
        self.returncode, self.stderr = code, stderr


def fake_worker(monkeypatch, plan, out_shape=(300, 200)):
    """plan: exit codes returned by successive worker calls; on 0 the worker 'writes' a result image."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        code = plan[min(len(calls) - 1, len(plan) - 1)]
        if code == 0:
            cv2.imwrite(cmd[cmd.index("-m") + 3], np.full((*out_shape, 3), 90, np.uint8))
        return _Run(code, "MemoryError: nope" if code == 4 else "")

    import subprocess

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_auto_dewarp_retries_other_margins_and_rescales_to_input_resolution(monkeypatch):
    calls = fake_worker(monkeypatch, [4, 0], out_shape=(600, 400))      # first margin diverges, second works
    flat = np.full((900, 600, 3), 200, np.uint8)
    out = po.auto_dewarp(flat)
    assert [c[-1] for c in calls] == ["0.06", "0.12"]
    assert max(out.shape[:2]) == 900 and out.shape[:2] == (900, 600)     # long side matches the input again


def test_auto_dewarp_gives_up_with_actionable_message(monkeypatch):
    fake_worker(monkeypatch, [4])
    with pytest.raises(RuntimeError, match="curve handles"):
        po.auto_dewarp(np.full((900, 600, 3), 200, np.uint8))


def test_auto_dewarp_reports_missing_package(monkeypatch):
    fake_worker(monkeypatch, [3])
    with pytest.raises(RuntimeError, match="pip install page-dewarp"):
        po.auto_dewarp(np.full((900, 600, 3), 200, np.uint8))


def test_auto_dewarp_timeout(monkeypatch):
    import subprocess

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(RuntimeError, match="too long"):
        po.auto_dewarp(np.full((900, 600, 3), 200, np.uint8))
