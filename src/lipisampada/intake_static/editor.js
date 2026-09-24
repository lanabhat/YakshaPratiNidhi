/* Page editor v2 for the intake app.
 *
 * A page is a list of regions. Each region is four free corners [TL,TR,BR,BL] (so any edge can lean),
 * optional edge-midpoint offsets `mids` [top,right,bottom,left] that bend an edge into a curve, and
 * its own enhancement settings. Coordinates are in pixels of the page after the 90-degree rotation.
 * The server does all real image work (warp / dewarp / enhance); this file only edits the numbers and
 * shows the server's preview of the active crop.
 *
 * Uses helpers from the main page script: toast(), esc(), loadQueue().
 */
"use strict";

const ED = {
  itemId: null, pages: [], idx: 0, page: null,
  img: null, rot: 0, rotCanvas: null, W: 0, H: 0, scale: 1,
  regions: [], active: 0, drag: null, temp: null, forceDraw: false,
  previewTimer: null, previewCtl: null, previewSeq: 0, busy: false,
};
const ED_MAX_REGIONS = 8;
const ED_NEUTRAL = { brightness: 0, contrast: 1.0, binarize: "off", threshold: 128, block: 35, c: 15 };
const $e = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ geometry helpers */
const edMid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
function edBez(p0, p1, off, t) {                       // quadratic edge that passes through chord-mid + off at t=0.5
  const mx = (p0[0] + p1[0]) / 2 + off[0], my = (p0[1] + p1[1]) / 2 + off[1];
  const cx = 2 * mx - (p0[0] + p1[0]) / 2, cy = 2 * my - (p0[1] + p1[1]) / 2, u = 1 - t;
  return [u * u * p0[0] + 2 * u * t * cx + t * t * p1[0], u * u * p0[1] + 2 * u * t * cy + t * t * p1[1]];
}
function edMids(r) { return r.mids || [[0, 0], [0, 0], [0, 0], [0, 0]]; }
function edEdges(r) {                                  // [from, to, offset] for top, right, bottom, left
  const [tl, tr, br, bl] = r.quad, m = edMids(r);
  return [[tl, tr, m[0]], [tr, br, m[1]], [bl, br, m[2]], [tl, bl, m[3]]];
}
function edHandleMid(r, i) {                           // where the i-th edge handle sits (chord mid + offset)
  const [a, b, off] = edEdges(r)[i], c = edMid(a, b);
  return [c[0] + off[0], c[1] + off[1]];
}
function edOutline(r, steps = 20) {                    // closed polygon following the (possibly curved) edges
  const [top, right, bottom, left] = edEdges(r), pts = [];
  const walk = (e, rev) => { for (let k = 0; k <= steps; k++) pts.push(edBez(e[0], e[1], e[2], rev ? 1 - k / steps : k / steps)); };
  walk(top, false); walk(right, false); walk(bottom, true); walk(left, true);
  return pts;
}
function edInside(poly, p) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
const edDist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
const edClone = (o) => JSON.parse(JSON.stringify(o));
const edFullQuad = (w, h) => [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]];
function edNewRegion(quad, mode = "simple") { return { quad, mids: null, enhance: { ...ED_NEUTRAL }, dewarp: "none", mode, fineAngle: 0 }; }
const AXIS_TOL = 3; // px: how far off horizontal/vertical a quad's edges may be and still count as "axis-aligned"
function edIsAxisAligned(quad) {
  const [tl, tr, br, bl] = quad;
  return Math.abs(tl[0] - bl[0]) < AXIS_TOL && Math.abs(tr[0] - br[0]) < AXIS_TOL
      && Math.abs(tl[1] - tr[1]) < AXIS_TOL && Math.abs(bl[1] - br[1]) < AXIS_TOL;
}
function edStraighten(quad) {                           // any quad -> its axis-aligned bounding rectangle
  const xs = quad.map((p) => p[0]), ys = quad.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
}
// For regions the server sent us (saved/auto-suggested/auto-cropped): fill in whatever a plain
// edNewRegion() would have, and guess a mode from whether it already has a curve or is tilted (a
// deskewed auto-suggestion is a rotated rectangle, not axis-aligned - that needs free-form's
// independent corners, or Simple's linked-corner drag would snap it straight and undo the deskew).
function edNormalizeRegion(r) {
  r.enhance = { ...ED_NEUTRAL, ...(r.enhance || {}) };
  r.dewarp = r.dewarp || "none";
  r.fineAngle = r.fineAngle || 0;                        // display-only running total; not sent to the server
  if (!r.mode) {
    const curved = (r.mids || []).some((o) => Math.abs(o[0]) > 0.5 || Math.abs(o[1]) > 0.5);
    r.mode = curved || !edIsAxisAligned(r.quad) ? "freeform" : "simple";
  }
  return r;
}
function edRotateCropBy(deg) {                          // nudge the active crop's angle by a small amount, in place, around its own center
  const a = ED.regions[ED.active];
  const rad = (deg * Math.PI) / 180, cos = Math.cos(rad), sin = Math.sin(rad);
  const cx = a.quad.reduce((s, p) => s + p[0], 0) / 4, cy = a.quad.reduce((s, p) => s + p[1], 0) / 4;
  a.quad = a.quad.map(([x, y]) => {
    const dx = x - cx, dy = y - cy;
    return [cx + dx * cos - dy * sin, cy + dx * sin + dy * cos];
  });
  a.mids = null;                                        // old curve offsets were relative to the un-rotated edges
  a.fineAngle = Math.round((a.fineAngle + deg) * 10) / 10;
  if (a.mode === "simple" && !edIsAxisAligned(a.quad)) a.mode = "freeform"; // a rotated rectangle needs free-form's independent corners
  edChanged(a); edSyncMode();
}
$e("rot-fine-up").onclick = () => edRotateCropBy(-0.5);
$e("rot-fine-down").onclick = () => edRotateCropBy(0.5);
function edRectFromTemp(temp) {                        // [x0,y0,x1,y1] canvas px -> clamped natural-px axis-aligned quad, or null if too small
  const [x0, y0, x1, y1] = temp;
  if (Math.abs(x1 - x0) < 25 || Math.abs(y1 - y0) < 25) return null;
  const p = edNatPt([Math.min(x0, x1), Math.min(y0, y1)]), q = edNatPt([Math.max(x0, x1), Math.max(y0, y1)]);
  return [p, [q[0], p[1]], q, [p[0], q[1]]].map(edClamp);
}
const edCanvasPt = (n) => [n[0] * ED.scale, n[1] * ED.scale];
const edNatPt = (c) => [c[0] / ED.scale, c[1] / ED.scale];
const edClamp = (p) => [Math.min(Math.max(p[0], 0), ED.W - 1), Math.min(Math.max(p[1], 0), ED.H - 1)];

/* ------------------------------------------------------------------ opening / loading pages */
// The editor is a real page (#/edit/<item>/<page index>), not a popup, so it survives a refresh
// and back/forward - see edParseHash/edHashFor below.
function edHashFor(itemId, idx) { return `#/edit/${itemId}/${idx}`; }
function edParseHash() {
  const m = location.hash.match(/^#\/edit\/(\d+)\/(\d+)$/);
  return m ? { itemId: +m[1], idx: +m[2] } : null;
}
let edSuppressHash = false;
function edSetHash(itemId, idx) {
  const target = edHashFor(itemId, idx);
  if (location.hash !== target) { edSuppressHash = true; location.hash = target; }
}

// reocr=true: the book has already been through OCR; saving a page re-reads just that page
async function openReview(itemId, pdfIndex = null, reocr = false, fromHash = false) {
  ED.itemId = itemId; ED.reocr = reocr;
  const data = await (await fetch(`/api/queue/${itemId}`)).json();
  ED.pages = data.pages;
  $e("review-title").textContent = `${data.item.title} — ${reocr ? "re-crop a page and read it again" : "check the pages"}`;
  $e("accept-next").innerHTML = reocr ? "Save &amp; re-OCR this page" : "Accept &amp; next";
  $e("accept-all").hidden = reocr;
  document.querySelector("header").hidden = true;
  document.querySelector("main").hidden = true;
  $e("view-editor").hidden = false;
  const first = pdfIndex !== null ? ED.pages.findIndex((p) => p.pdf_page_index === pdfIndex) : ED.pages.findIndex((p) => !p.approved);
  const idx = first === -1 ? 0 : first;
  if (!fromHash) edSetHash(itemId, idx);
  loadEdPage(idx);
}
function edHideEditor() {
  clearTimeout(ED.previewTimer);
  $e("view-editor").hidden = true;
  document.querySelector("header").hidden = false;
  document.querySelector("main").hidden = false;
  if (edParseHash()) { edSuppressHash = true; history.replaceState(null, "", location.pathname + location.search); }
}
$e("review-close").onclick = async () => { if (await edConfirmLeave()) { edHideEditor(); loadQueue(); } };
$e("ed-prev").onclick = async () => { if (await edConfirmLeave()) edGoAdjacent(-1); };
$e("ed-next").onclick = async () => { if (await edConfirmLeave()) edGoAdjacent(1); };
function edGoAdjacent(delta) {
  const n = ED.idx + delta;
  if (n < 0 || n >= ED.pages.length) { toast(delta < 0 ? "Already at the first page" : "Already at the last page"); return; }
  loadEdPage(n);
}
window.addEventListener("hashchange", () => {
  if (edSuppressHash) { edSuppressHash = false; return; }
  const r = edParseHash();
  const revert = () => { edSuppressHash = true; location.hash = edHashFor(ED.itemId, ED.idx); };
  if (r && ED.itemId != null && r.itemId === ED.itemId) {
    if (r.idx === ED.idx) return;
    edConfirmLeave().then((ok) => (ok ? loadEdPage(r.idx) : revert()));
  } else if (r) {
    openReview(r.itemId, r.idx, false, true);
  } else if (!$e("view-editor").hidden) {
    edConfirmLeave().then((ok) => (ok ? (edHideEditor(), loadQueue()) : revert()));
  }
});

/* ------------------------------------------------------------------ unsaved-edit tracking */
function edSnapshot() { return JSON.stringify({ rot: ED.rot, regions: ED.regions.map(edForServer) }); }
function edIsDirty() { return ED.savedSnapshot !== undefined && edSnapshot() !== ED.savedSnapshot; }
function edMarkSaved() { ED.savedSnapshot = edSnapshot(); }
// Resolves true once it's OK to navigate away from the current page (nothing unsaved, or the
// person chose to save or explicitly discard); false means stay put.
async function edConfirmLeave() {
  if (!ED.page || !edIsDirty()) return true;
  if (confirm("Save your changes to this page before moving on?\n\nOK = save and continue\nCancel = don't save yet")) {
    try {
      const r = await edPost(`/api/queue/${ED.itemId}/pages/${ED.page.id}/approve`, { rot90: ED.rot, regions: ED.regions.map(edForServer) });
      ED.page.approved = 1; ED.page.regions = JSON.stringify(ED.regions); ED.page.rot90 = ED.rot;
      edMarkSaved();
      if (r.all_pages_approved) toast("All pages approved - OCR will start automatically");
      return true;
    } catch (e) { toast(e.message); return false; }
  }
  return confirm("Discard your changes to this page and continue without saving?");
}

function edPageLabels() {                              // e.g. "12" for one crop, "12L / 12R", "12A / 12B / 12C"
  const n = ED.regions.length, base = ED.page.pdf_page_index + 1;
  if (n === 1) return [`${base}`];
  const sides = n === 2 ? ["L", "R"] : Array.from({ length: n }, (_, i) => String.fromCharCode(65 + i));
  return sides.map((s) => `${base}${s}`);
}

function edRenderStrip() {
  const strip = $e("review-pages-strip");
  // The number is always shown (not swapped for a checkmark) so it still works as a page index at
  // a glance; approved/current status is carried by the border/background colour instead.
  strip.innerHTML = ED.pages.map((p, i) =>
    `<div class="page-thumb ${p.approved ? "approved" : ""} ${i === ED.idx ? "current" : ""}" data-idx="${i}" title="Page ${i + 1}${p.approved ? " - approved" : ""}">${i + 1}</div>`).join("");
  strip.querySelectorAll(".page-thumb").forEach((el) => (el.onclick = async () => {
    const i = parseInt(el.dataset.idx, 10);
    if (i !== ED.idx && (await edConfirmLeave())) loadEdPage(i);
  }));
  $e("ed-prev").disabled = ED.idx <= 0;
  $e("ed-next").disabled = ED.idx >= ED.pages.length - 1;
}

function loadEdPage(idx) {
  if (idx < 0 || idx >= ED.pages.length) return;
  ED.idx = idx; ED.page = ED.pages[idx]; ED.active = 0;
  ED.rot = ED.page.rot90 || 0;
  edSetHash(ED.itemId, idx);
  const saved = ED.page.regions ? JSON.parse(ED.page.regions) : (ED.page.auto_regions ? JSON.parse(ED.page.auto_regions) : null);
  edRenderStrip();
  const img = new Image();
  img.onload = () => {
    ED.img = img;
    edBuildRotated();
    ED.regions = saved && saved.length ? saved : [edNewRegion(edFullQuad(ED.W, ED.H))];
    ED.regions.forEach(edNormalizeRegion);
    edFit(); edRefreshAll();
    edMarkSaved();
  };
  img.src = `/api/queue/${ED.itemId}/pages/${ED.page.id}/image?t=${Date.now()}`;
}

function edBuildRotated() {                            // the original, turned by ED.rot degrees, on an offscreen canvas
  const w = ED.img.naturalWidth, h = ED.img.naturalHeight, turn = (ED.rot / 90) % 2 === 1;
  const c = document.createElement("canvas");
  c.width = turn ? h : w; c.height = turn ? w : h;
  const g = c.getContext("2d");
  g.translate(c.width / 2, c.height / 2); g.rotate((ED.rot * Math.PI) / 180);
  g.drawImage(ED.img, -w / 2, -h / 2);
  ED.rotCanvas = c; ED.W = c.width; ED.H = c.height;
}
function edFit() {                                      // fits the canvas to whatever room the surrounding flex layout actually leaves it
  const cv = $e("page-canvas"), col = $e("ed-canvas-col"), hint = $e("ed-hint");
  // col.clientHeight already excludes the top bar, toolbar and bottom page strip - flexbox gave
  // .ed-body (and so #ed-canvas-col) only what's left over, whatever that currently is.
  const availW = Math.max(240, col.clientWidth - 8);
  const availH = Math.max(240, col.clientHeight - hint.getBoundingClientRect().height - 12);
  ED.scale = Math.min(availW / ED.W, availH / ED.H, 1);
  cv.width = Math.round(ED.W * ED.scale); cv.height = Math.round(ED.H * ED.scale);
}

/* ------------------------------------------------------------------ splitter: drag the boundary
   between the canvas and the right panel - an "anchor" you can grab anywhere along its height,
   not just the small corner-resize grip on the panel itself. */
(() => {
  const bar = $e("ed-splitter"), panel = $e("ed-right-resize");
  let drag = null;
  bar.addEventListener("pointerdown", (e) => {
    drag = { startX: e.clientX, startW: panel.getBoundingClientRect().width };
    bar.classList.add("dragging");
    bar.setPointerCapture(e.pointerId);
  });
  bar.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const min = parseFloat(getComputedStyle(panel).minWidth) || 280;
    const max = parseFloat(getComputedStyle(panel).maxWidth) || 1000;
    const w = Math.min(max, Math.max(min, drag.startW - (e.clientX - drag.startX)));
    panel.style.width = w + "px";
    if (ED.rotCanvas) { edFit(); edDraw(); }
  });
  const endDrag = () => { drag = null; bar.classList.remove("dragging"); };
  bar.addEventListener("pointerup", endDrag);
  bar.addEventListener("pointercancel", endDrag);
})();
// the panel's own corner-resize grip changes size natively (no JS) - keep the canvas in sync with it too
$e("ed-right-resize").addEventListener("mouseup", () => { if (ED.rotCanvas) { edFit(); edDraw(); } });
window.addEventListener("resize", () => { if (ED.rotCanvas && !$e("view-editor").hidden) { edFit(); edDraw(); } });

/* ------------------------------------------------------------------ drawing */
function edDraw() {
  const cv = $e("page-canvas"), g = cv.getContext("2d");
  g.clearRect(0, 0, cv.width, cv.height);
  g.drawImage(ED.rotCanvas, 0, 0, cv.width, cv.height);
  ED.regions.forEach((r, i) => {
    const poly = edOutline(r).map(edCanvasPt), on = i === ED.active;
    g.beginPath(); poly.forEach((p, k) => (k ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]))); g.closePath();
    g.fillStyle = on ? "rgba(76,139,245,0.16)" : "rgba(160,170,200,0.10)"; g.fill();
    g.strokeStyle = on ? "#4c8bf5" : "#8fa0c8"; g.lineWidth = on ? 2.5 : 1.5; g.stroke();
    const cx = poly.reduce((s, p) => s + p[0], 0) / poly.length, cy = poly.reduce((s, p) => s + p[1], 0) / poly.length;
    g.font = "bold 22px sans-serif"; g.fillStyle = on ? "#4c8bf5" : "#8fa0c8"; g.fillText(String(i + 1), cx - 6, cy + 8);
  });
  const a = ED.regions[ED.active];
  if (a) {
    g.fillStyle = "#4c8bf5";
    a.quad.map(edCanvasPt).forEach((p) => g.fillRect(p[0] - 7, p[1] - 7, 14, 14));
    if (a.mode !== "simple") {                          // curve handles only make sense in free-form mode
      for (let i = 0; i < 4; i++) {
        const p = edCanvasPt(edHandleMid(a, i));
        g.beginPath(); g.arc(p[0], p[1], 6, 0, 7); g.fillStyle = "#ffd166"; g.fill(); g.strokeStyle = "#7a5a10"; g.lineWidth = 1.5; g.stroke();
      }
    }
  }
  if (ED.temp) {
    const [x0, y0, x1, y1] = ED.temp;
    g.setLineDash([6, 4]); g.strokeStyle = "#7ee08a"; g.lineWidth = 2; g.strokeRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0)); g.setLineDash([]);
  }
}

/* ------------------------------------------------------------------ pointer interaction */
function edPointer(e) {
  const r = $e("page-canvas").getBoundingClientRect();
  return [(e.clientX - r.left) * ($e("page-canvas").width / r.width), (e.clientY - r.top) * ($e("page-canvas").height / r.height)];
}
function edHit(c) {
  const a = ED.regions[ED.active];
  if (a) {
    for (let i = 0; i < 4; i++) if (edDist(c, edCanvasPt(a.quad[i])) < 12) return { kind: "corner", i };
    if (a.mode !== "simple") for (let i = 0; i < 4; i++) if (edDist(c, edCanvasPt(edHandleMid(a, i))) < 11) return { kind: "mid", i };
    if (edInside(edOutline(a).map(edCanvasPt), c)) return { kind: "move", region: ED.active };
  }
  for (let j = ED.regions.length - 1; j >= 0; j--)
    if (j !== ED.active && edInside(edOutline(ED.regions[j]).map(edCanvasPt), c)) return { kind: "select", region: j };
  return { kind: "draw" };
}

const edCanvas = $e("page-canvas");
edCanvas.addEventListener("pointerdown", (e) => {
  if (!ED.rotCanvas) return;
  const c = edPointer(e);
  edCanvas.setPointerCapture(e.pointerId);
  if (ED.forceDraw) {                                   // "Draw new rectangle" was clicked: this drag redefines the active crop, whatever it lands on
    ED.forceDraw = false;
    ED.drag = { kind: "draw-replace", start: c }; ED.temp = [c[0], c[1], c[0], c[1]];
    return;
  }
  const hit = edHit(c);
  if (hit.kind === "select") { ED.active = hit.region; edRefreshAll(); ED.drag = { kind: "move", start: c, base: edClone(ED.regions[ED.active].quad), moved: false }; return; }
  if (hit.kind === "move") ED.drag = { kind: "move", start: c, base: edClone(ED.regions[ED.active].quad), moved: false };
  else if (hit.kind === "draw") { ED.drag = { kind: "draw", start: c }; ED.temp = [c[0], c[1], c[0], c[1]]; }
  else ED.drag = { ...hit, moved: false };
});
edCanvas.addEventListener("pointermove", (e) => {
  const c = edPointer(e), d = ED.drag;
  if (!d) {
    const h = edHit(c);
    edCanvas.style.cursor = h.kind === "corner" || h.kind === "mid" ? "pointer" : h.kind === "move" || h.kind === "select" ? "move" : "crosshair";
    return;
  }
  const a = ED.regions[ED.active], n = edNatPt(c);
  if (d.kind === "draw" || d.kind === "draw-replace") { ED.temp = [d.start[0], d.start[1], c[0], c[1]]; edDraw(); return; }
  d.moved = true;
  if (d.kind === "corner") {
    const p = edClamp(n);
    a.quad[d.i] = p;
    if (a.mode === "simple") {                         // move the two connected edges together, like an ordinary rectangle-crop handle
      const xP = d.i ^ 3, yP = d.i ^ 1;                  // corner sharing this one's vertical / horizontal edge (TL=0,TR=1,BR=2,BL=3)
      a.quad[xP] = [p[0], a.quad[xP][1]];
      a.quad[yP] = [a.quad[yP][0], p[1]];
    }
  }
  else if (d.kind === "mid") {                         // edge handle: offset from the straight chord, so the edge follows the pointer
    const [p, q] = edEdges(a)[d.i], ch = edMid(p, q);
    a.mids = edMids(a).map((m) => m.slice()); a.mids[d.i] = [n[0] - ch[0], n[1] - ch[1]];
  } else if (d.kind === "move") {
    let dx = (c[0] - d.start[0]) / ED.scale, dy = (c[1] - d.start[1]) / ED.scale;
    const xs = d.base.map((p) => p[0]), ys = d.base.map((p) => p[1]);
    dx = Math.min(Math.max(dx, -Math.min(...xs)), ED.W - 1 - Math.max(...xs));
    dy = Math.min(Math.max(dy, -Math.min(...ys)), ED.H - 1 - Math.max(...ys));
    a.quad = d.base.map((p) => [p[0] + dx, p[1] + dy]);
  }
  edChanged(a);
});
function edEndDrag() {
  const d = ED.drag; ED.drag = null;
  if (d && d.kind === "draw" && ED.temp) {
    const quad = edRectFromTemp(ED.temp); ED.temp = null;
    if (quad) {
      if (ED.regions.length >= ED_MAX_REGIONS) toast(`At most ${ED_MAX_REGIONS} crops per page`);
      else {
        ED.regions.push(edNewRegion(quad)); ED.active = ED.regions.length - 1;
        edRefreshAll(); return;
      }
    }
    edDraw();
  } else if (d && d.kind === "draw-replace" && ED.temp) {
    const quad = edRectFromTemp(ED.temp); ED.temp = null;
    const a = ED.regions[ED.active];
    if (quad) { a.quad = quad; a.mids = null; edChanged(a); }
    else edDraw();
  }
}
edCanvas.addEventListener("pointerup", edEndDrag);
edCanvas.addEventListener("pointercancel", edEndDrag);
document.addEventListener("keydown", (e) => {
  if (!$e("view-editor").hidden && e.key === "Delete" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) edDeleteCrop();
});

/* ------------------------------------------------------------------ region editing */
function edChanged(region) {                           // an edit invalidates any auto-dewarp done for the old shape
  if (region && region.dewarp === "auto") { region.dewarp = "none"; $e("preview-note").textContent = "shape changed - run Auto-dewarp again if you want it"; }
  edDraw(); edSchedulePreview();
}
function edRefreshAll() { edDraw(); edRenderList(); edSyncControls(); edSyncMode(); edSchedulePreview(); }

/* ------------------------------------------------------------------ crop mode: simple rectangle vs free-form/curved */
function edSyncMode() {
  const a = ED.regions[ED.active], mode = a.mode;
  $e("mode-simple").classList.toggle("active", mode === "simple");
  $e("mode-freeform").classList.toggle("active", mode !== "simple");
  $e("ed-hint").textContent = mode === "simple"
    ? "Click “Draw new rectangle”, then drag corner to corner on the page · drag a corner to nudge · drag inside to move"
    : "Drag a blue corner to slant an edge · drag a yellow dot to curve an edge · drag inside to move · drag on empty space to draw another crop";
  $e("crop-angle-label").textContent = `${a.fineAngle > 0 ? "+" : ""}${a.fineAngle.toFixed(1)}°`;
}
function edSetMode(mode) {
  const a = ED.regions[ED.active];
  if (a.mode === mode) return;
  a.mode = mode;
  if (mode === "simple") {                             // a simple crop has no curve, and must be axis-aligned for its linked-corner drag to make sense
    a.mids = null;
    if (!edIsAxisAligned(a.quad)) a.quad = edStraighten(a.quad);
  }
  edChanged(a); edSyncMode();
}
$e("mode-simple").onclick = () => edSetMode("simple");
$e("mode-freeform").onclick = () => edSetMode("freeform");
$e("draw-rect").onclick = () => { ED.forceDraw = true; toast("Now drag on the page, corner to corner"); };

function edRenderList() {
  const names = edPageLabels();
  $e("region-list").innerHTML = ED.regions.map((r, i) =>
    `<button class="chip-btn ${i === ED.active ? "active" : ""}" data-i="${i}">Crop ${i + 1} <small>→ page ${names[i]}</small>${r.dewarp === "auto" ? " <small>· dewarped</small>" : ""}</button>`).join("");
  $e("region-list").querySelectorAll("button").forEach((b) => (b.onclick = () => { ED.active = +b.dataset.i; edRefreshAll(); }));
  $e("crop-delete").disabled = ED.regions.length <= 1;
  $e("crop-up").disabled = ED.active === 0; $e("crop-down").disabled = ED.active >= ED.regions.length - 1;
  $e("add-crop").disabled = ED.regions.length >= ED_MAX_REGIONS;
}
function edDeleteCrop() {
  if (ED.regions.length <= 1) return;
  ED.regions.splice(ED.active, 1); ED.active = Math.min(ED.active, ED.regions.length - 1); edRefreshAll();
}
$e("crop-delete").onclick = edDeleteCrop;
$e("add-crop").onclick = () => {
  if (ED.regions.length >= ED_MAX_REGIONS) return;
  const w = ED.W * 0.4, h = ED.H * 0.4, x = (ED.W - w) / 2, y = (ED.H - h) / 2;
  ED.regions.push(edNewRegion([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])); ED.active = ED.regions.length - 1; edRefreshAll();
};
$e("split-two").onclick = () => {
  if (ED.regions.length !== 1) { toast("Split works on a single crop - delete the extra crops first"); return; }
  const r = ED.regions[0], [tl, tr, br, bl] = r.quad, mt = edMid(tl, tr), mb = edMid(bl, br);
  const left = { ...edClone(r), quad: [tl, mt, mb, bl], mids: null, dewarp: "none" };
  const right = { ...edClone(r), quad: [mt, tr, br, mb], mids: null, dewarp: "none" };
  ED.regions = [left, right]; ED.active = 0; edRefreshAll();
};
$e("crop-up").onclick = () => { const i = ED.active; if (i > 0) { [ED.regions[i - 1], ED.regions[i]] = [ED.regions[i], ED.regions[i - 1]]; ED.active--; edRefreshAll(); } };
$e("crop-down").onclick = () => { const i = ED.active; if (i < ED.regions.length - 1) { [ED.regions[i + 1], ED.regions[i]] = [ED.regions[i], ED.regions[i + 1]]; ED.active++; edRefreshAll(); } };
$e("reset-curve").onclick = () => { const a = ED.regions[ED.active]; a.mids = null; edChanged(a); };

function edRotate(step) {                              // turn the page 90 degrees and carry every crop with it
  const oldW = ED.W, oldH = ED.H;
  const mapPt = step > 0 ? (p) => [oldH - 1 - p[1], p[0]] : (p) => [p[1], oldW - 1 - p[0]];
  const mapOff = step > 0 ? (o) => [-o[1], o[0]] : (o) => [o[1], -o[0]];
  ED.regions.forEach((r) => {
    const q = r.quad.map(mapPt), m = edMids(r).map(mapOff);
    // clockwise: old BL,TL,TR,BR become new TL,TR,BR,BL and old left,top,right,bottom edges become top,right,bottom,left
    if (step > 0) { r.quad = [q[3], q[0], q[1], q[2]]; r.mids = [m[3], m[0], m[1], m[2]]; }
    else { r.quad = [q[1], q[2], q[3], q[0]]; r.mids = [m[1], m[2], m[3], m[0]]; }
    if (!r.mids.some((o) => Math.abs(o[0]) > 0.5 || Math.abs(o[1]) > 0.5)) r.mids = null;
    r.dewarp = "none";
  });
  ED.rot = (ED.rot + (step > 0 ? 90 : 270)) % 360;
  edBuildRotated(); edFit(); edRefreshAll();
}
$e("rot-right").onclick = () => edRotate(1);
$e("rot-left").onclick = () => edRotate(-1);

$e("auto-crop").onclick = async () => {
  try {
    const r = await fetch(`/api/queue/${ED.itemId}/pages/${ED.page.id}/auto-crop`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rot90: ED.rot }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "auto-crop failed");
    ED.regions = data.regions.map(edNormalizeRegion); ED.active = 0; edRefreshAll(); toast("Auto-crop applied - drag the corners to adjust");
  } catch (e) { toast(e.message); }
};
$e("auto-dewarp").onclick = async () => {
  const a = ED.regions[ED.active], btn = $e("auto-dewarp");
  btn.disabled = true; $e("preview-note").textContent = "auto-dewarping… this can take 10 seconds";
  try {
    const r = await fetch(`/api/queue/${ED.itemId}/pages/${ED.page.id}/auto-dewarp`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rot90: ED.rot, regions: ED.regions.map(edForServer), index: ED.active }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "auto-dewarp failed");
    a.dewarp = "auto"; edRenderList(); edSchedulePreview(); toast("Auto-dewarp done - compare the result, or Reset curves / edit the crop to undo it");
  } catch (e) { toast(e.message); $e("preview-note").textContent = ""; }
  btn.disabled = false;
};

/* ------------------------------------------------------------------ enhancement controls */
const EDC = { brightness: $e("enh-brightness"), contrast: $e("enh-contrast"), binarize: $e("enh-binarize"), threshold: $e("enh-threshold"), block: $e("enh-block"), c: $e("enh-c") };
function edSyncControls() {
  const e = ED.regions[ED.active].enhance;
  EDC.brightness.value = e.brightness; EDC.contrast.value = Math.round(e.contrast * 100); EDC.binarize.value = e.binarize;
  EDC.threshold.value = e.threshold; EDC.block.value = e.block; EDC.c.value = e.c;
  edShowEnhanceRows(); edEnhanceLabels();
}
function edShowEnhanceRows() {
  const m = EDC.binarize.value;
  $e("row-threshold").hidden = m !== "global"; $e("row-adaptive").hidden = m !== "adaptive";
}
function edEnhanceLabels() {
  $e("lbl-brightness").textContent = EDC.brightness.value; $e("lbl-contrast").textContent = (EDC.contrast.value / 100).toFixed(2);
  $e("lbl-threshold").textContent = EDC.threshold.value; $e("lbl-block").textContent = EDC.block.value; $e("lbl-c").textContent = EDC.c.value;
}
function edReadControls() {
  const e = ED.regions[ED.active].enhance;
  e.brightness = +EDC.brightness.value; e.contrast = EDC.contrast.value / 100; e.binarize = EDC.binarize.value;
  e.threshold = +EDC.threshold.value; e.block = +EDC.block.value; e.c = +EDC.c.value;
  edShowEnhanceRows(); edEnhanceLabels(); edSchedulePreview();
}
Object.values(EDC).forEach((el) => el.addEventListener("input", edReadControls));
$e("reset-enhance").onclick = () => { ED.regions[ED.active].enhance = { ...ED_NEUTRAL }; edSyncControls(); edSchedulePreview(); };

/* ------------------------------------------------------------------ live preview (server renders the active crop) */
function edForServer(r) { const { kind, mode, fineAngle, ...rest } = r; return rest; }
function edSchedulePreview() { clearTimeout(ED.previewTimer); ED.previewTimer = setTimeout(edRunPreview, 180); }
// Re-render at a size matching the box, not a fixed 900px, so dragging the box bigger actually
// shows more detail instead of just more empty space around the same image.
new ResizeObserver(() => { if (ED.rotCanvas) edSchedulePreview(); }).observe($e("preview-box"));
async function edRunPreview() {
  if (!ED.page || $e("view-editor").hidden) return;
  if (ED.previewCtl) ED.previewCtl.abort();
  const ctl = (ED.previewCtl = new AbortController()), seq = ++ED.previewSeq;
  $e("preview-box").classList.add("loading");
  const maxWidth = Math.max(400, Math.min(2200, Math.round($e("preview-box").clientWidth * (window.devicePixelRatio || 1))));
  try {
    const r = await fetch(`/api/queue/${ED.itemId}/pages/${ED.page.id}/preview`, { method: "POST", signal: ctl.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rot90: ED.rot, regions: ED.regions.map(edForServer), active: ED.active, max_width: maxWidth }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || "preview failed"); }
    const blob = await r.blob();
    if (seq !== ED.previewSeq) return;
    const img = $e("preview-img"), old = img.src;
    img.onload = () => { $e("preview-size").textContent = `${img.naturalWidth}×${img.naturalHeight}px preview`; };
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    if (ED.regions[ED.active].dewarp !== "auto") $e("preview-note").textContent = "";
    else $e("preview-note").textContent = r.headers.get("X-Dewarp") === "cached" ? "auto-dewarp applied" : "";
  } catch (e) {
    if (e.name !== "AbortError") { $e("preview-note").textContent = e.message; }
  } finally {
    if (seq === ED.previewSeq) $e("preview-box").classList.remove("loading");
  }
}

/* ------------------------------------------------------------------ accepting */
async function edPost(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || "request failed");
  return data;
}
function edAfterApprovals(allDone) {
  if (allDone) { edHideEditor(); toast("All pages approved - OCR will start automatically"); loadQueue(); return; }
  const next = ED.pages.findIndex((p, i) => !p.approved && i !== ED.idx);
  loadEdPage(next === -1 ? ED.idx : next);
}
$e("accept-next").onclick = async () => {
  if (ED.busy) return;
  ED.busy = true;
  try {
    const r = await edPost(`/api/queue/${ED.itemId}/pages/${ED.page.id}/approve`, { rot90: ED.rot, regions: ED.regions.map(edForServer) });
    ED.page.approved = 1; ED.page.regions = JSON.stringify(ED.regions); ED.page.rot90 = ED.rot;
    edMarkSaved();
    if (ED.reocr) {
      const re = await edPost(`/api/queue/${ED.itemId}/pages/${ED.page.id}/reocr`);
      edHideEditor();
      toast(re.was_published ? "Re-reading the page; the new text goes to the review app automatically afterwards." : "Re-reading the page…");
      openProgress(ED.itemId);
    } else {
      toast(`Page ${ED.page.pdf_page_index + 1} accepted`);
      edAfterApprovals(r.all_pages_approved);
    }
  } catch (e) { toast(e.message); }
  ED.busy = false;
};
$e("accept-all").onclick = async () => {
  const left = ED.pages.filter((p) => !p.approved).length;
  if (!left) return;
  if (!confirm(`Accept the automatic crop for the ${left} page(s) not yet checked? You won't see them first (you can re-open any page afterwards).`)) return;
  if (ED.busy) return;
  ED.busy = true; $e("accept-all").disabled = true;
  try {
    const r = await edPost(`/api/queue/${ED.itemId}/accept-auto`);
    const data = await (await fetch(`/api/queue/${ED.itemId}`)).json();
    ED.pages = data.pages; toast(`Accepted ${r.approved} page(s)`);
    edRenderStrip(); edAfterApprovals(r.all_pages_approved);
  } catch (e) { toast(e.message); }
  ED.busy = false; $e("accept-all").disabled = false;
};
