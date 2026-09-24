"""Runs the optional `page-dewarp` package on one image in its own process:

    python -m lipisampada.dewarp_worker <in.png> <out.png> [--pad FRACTION]

Isolated because page-dewarp's optimizer can occasionally diverge on a page and ask for
absurd amounts of memory; in native code that aborts the whole process. As a subprocess that
costs one failed attempt (page_ops.auto_dewarp reports it), never the intake app.
Exit code 0 = wrote <out.png>; anything else = no result, reason on stderr."""

import contextlib
import io
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


def main(argv: list[str]) -> int:
    src, dst = Path(argv[1]), Path(argv[2])
    pad = float(argv[argv.index("--pad") + 1]) if "--pad" in argv else 0.0
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        print("cannot read input", file=sys.stderr)
        return 2
    if pad > 0:  # text pressed against the edge confuses its line finder; give it margin
        p = int(pad * max(img.shape[:2]))
        fill = tuple(int(v) for v in np.median(img.reshape(-1, 3), axis=0))
        img = cv2.copyMakeBorder(img, p, p, p, p, cv2.BORDER_CONSTANT, value=fill)
    try:
        from page_dewarp.image import WarpedImage
        from page_dewarp.options import Config
    except ImportError:
        print("page-dewarp is not installed", file=sys.stderr)
        return 3
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "page.png"
        cv2.imwrite(str(work), img)
        try:
            with contextlib.redirect_stdout(io.StringIO()):  # it narrates its optimizer to stdout
                WarpedImage(str(work), Config(OUTPUT_DIR=tmp, NO_BINARY=1, DEBUG_LEVEL=0))
        except BaseException as e:  # noqa: BLE001 - report anything, including MemoryError
            print(f"{type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            return 4
        outs = [q for q in Path(tmp).glob("*.png") if q.name != "page.png"]
        out = cv2.imread(str(outs[0])) if outs else None
        if out is None:
            print("no text lines found", file=sys.stderr)
            return 5
        # guard against a diverged fit that "succeeded" with an absurd result
        if out.shape[0] > 6 * img.shape[0] or out.shape[1] > 6 * img.shape[1] or min(out.shape[:2]) < 50:
            print(f"implausible result {out.shape[:2]} from input {img.shape[:2]}", file=sys.stderr)
            return 6
        cv2.imwrite(str(dst), out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
