"use strict";
const CFG = window.LIPI_CONFIG;
if (location.hostname === "localhost" || location.hostname === "127.0.0.1") {
  // this same web/ folder is served both locally (app 1's "local" publish stage feeds app 2 running
  // here) and deployed to Firebase Hosting - config.js's API_BASE is the *production* target either
  // way, so when we're clearly running locally, always talk to the local app 2 instead.
  CFG.API_BASE = CFG.LOCAL_API_BASE || "http://127.0.0.1:8200";
}
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STRIP = 20; // page numbers per pagination strip

let me = null;            // {uid,email,name,role} or null (anonymous browsing / guest)
let perms = ["read"];     // what this person may do - decided by the server (reviewapi/permissions.py)
const bundles = {};       // book id -> {snippet id -> raw readings} (or null)

/* ---------------- helpers ---------------- */
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2200);
}
function showModal(html) { $("modal-body").innerHTML = html; $("modal").hidden = false; }
function closeModal() { $("modal").hidden = true; }
$("modal-x").onclick = closeModal;
$("modal").addEventListener("mousedown", (e) => { if (e.target === $("modal")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function guestId() {
  let g = localStorage.getItem("lipi_guest");
  if (!g) { g = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()); localStorage.setItem("lipi_guest", g); }
  return g;
}
async function authHeaders() {
  if (CFG.AUTH_MODE === "dev") {
    const t = localStorage.getItem("lipi_token");
    return t ? { Authorization: "Bearer " + t } : { "X-Guest-Id": guestId() };
  }
  // firebase mode: a Google ID token expires after 1 hour, so it's never cached here - fetched fresh
  // from the SDK on every call instead. currentUser.getIdToken() returns its own cached copy and only
  // does a network round-trip to refresh when that's actually close to/past expiry, so this is cheap.
  if (firebaseUser) {
    try { return { Authorization: "Bearer " + (await firebaseUser.getIdToken()) }; }
    catch (e) { /* refresh failed (e.g. the session was revoked) - fall through to guest */ }
  }
  return { "X-Guest-Id": guestId() };
}
async function api(path, { method = "GET", body } = {}) {
  const r = await fetch(CFG.API_BASE + path, {
    method,
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(await authHeaders()) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const isJson = (r.headers.get("content-type") || "").includes("json");
  const data = isJson ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
// For an admin-gated binary download (e.g. the backup zip): a plain <a href target="_blank"> would
// navigate with no Authorization header at all (that's only ever attached by fetch calls, never by a
// browser-driven page navigation), so an admin-only endpoint would just 403 with nothing downloaded.
// Fetches authenticated instead, then hands the browser a real file via a throwaway object URL.
// `filename` is only the fallback - the server's Content-Disposition name (e.g. built from the book's
// title/kavi) wins when present, which needs CORS's expose_headers (reviewapi/app.py) to be readable
// here at all, since it isn't one of the default CORS-safelisted response headers.
async function downloadFile(path, filename) {
  const r = await fetch(CFG.API_BASE + path, { headers: await authHeaders() });
  if (!r.ok) {
    const data = await r.json().catch(() => null);
    toast((data && data.error) || `Could not download (HTTP ${r.status})`);
    return;
  }
  const cd = r.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="?([^";]+)"?/);
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a");
  a.href = url; a.download = (m && m[1]) || filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}
const can = (p) => perms.includes(p);
const isEditor = () => can("approve_text");
const isAdmin = () => can("reopen");
const pageLabel = (p) => `${p.page_number}${p.side && p.side !== "P" ? p.side : ""}`;

/* ---------------- auth ---------------- */
let onboardingShown = false;
let firebaseUser = null;  // live firebase.User, kept in sync via onAuthStateChanged below - unused in dev mode
function isSignedIn() {
  return CFG.AUTH_MODE === "dev" ? !!localStorage.getItem("lipi_token") : !!firebaseUser;
}
async function loadMe() {
  try { const r = await api("/api/me"); me = r.user; perms = r.permissions || ["read"]; }
  catch (e) { me = null; perms = ["read"]; if (CFG.AUTH_MODE === "dev") localStorage.removeItem("lipi_token"); }
  renderWho();
  refreshAdminBadge();
  if (me && me.application_status === "new" && !onboardingShown) { onboardingShown = true; openOnboardingForm(); }
}
function renderWho() {
  const signedIn = isSignedIn() && me;
  if (signedIn && me.banned) {
    $("who").innerHTML = `<span class="pill warn">Banned${me.ban_reason ? ": " + esc(me.ban_reason) : ""}</span><button class="btn" id="signout">Sign out</button>`;
  } else if (signedIn) {
    const pending = me.application_status === "pending" ? '<span class="pill sug">application pending</span>' : "";
    $("who").innerHTML = `<span>${esc(me.name || me.email)}</span><span class="role">${esc(me.role)}</span>${pending}<button class="btn" id="signout">Sign out</button>`;
  } else {
    $("who").innerHTML = `<span>Browsing as guest</span><button class="btn primary" id="signin">Sign in</button>`;
  }
  $("nav-admin").hidden = !can("manage_users");
  const signout = $("signout");
  if (signout) signout.onclick = () => {
    onboardingShown = false;
    if (CFG.AUTH_MODE === "dev") {
      localStorage.removeItem("lipi_token"); me = null; perms = ["read"];
      loadMe().then(route);
    } else if (firebaseApp) {
      firebase.auth().signOut().catch(() => {});  // onAuthStateChanged (below) picks this up
    }
  };
  const signin = $("signin");
  if (signin) signin.onclick = signIn;
}
async function refreshAdminBadge() {
  const badge = $("admin-badge");
  if (!can("manage_users")) { badge.hidden = true; return; }
  try {
    const { applications } = await api("/api/admin/applications");
    badge.hidden = applications.length === 0;
    badge.textContent = applications.length;
  } catch (e) { /* not critical */ }
}

let firebaseApp = null;
function firebaseAuth() {
  if (!firebaseApp) firebaseApp = firebase.initializeApp(CFG.FIREBASE_CONFIG);
  return firebase.auth();
}
async function signIn() {
  if (CFG.AUTH_MODE === "dev") {
    const email = (prompt("Dev sign-in - enter an email address (local development only):") || "").trim().toLowerCase();
    if (!email) return;
    localStorage.setItem("lipi_token", "dev:" + email);
    await loadMe(); route();
    return;
  }
  try {
    await firebaseAuth().signInWithPopup(new firebase.auth.GoogleAuthProvider());
    // no token cached here - onAuthStateChanged (below) picks up the new firebaseUser and calls
    // loadMe()/route() itself; authHeaders() fetches a fresh ID token per call from then on
  } catch (e) {
    if (e && e.code !== "auth/popup-closed-by-user") toast("Sign-in failed: " + e.message);
  }
}

/* ---------------- onboarding: shown once right after a brand-new sign-in ---------------- */
function openOnboardingForm() {
  showModal(`
    <h2>Welcome to Yaksha - PratiNidhi (ಯಕ್ಷ-ಪ್ರತಿ-ನಿಧಿ)!</h2>
    <p class="dim small">A couple of quick questions before you get started.</p>
    <div class="field"><label>Name</label><input type="text" id="ob-name" value="${esc((me && me.name) || "")}"></div>
    <div class="field"><label>Place</label><input type="text" id="ob-place" placeholder="City / town"></div>
    <div class="field"><label>Would you be reviewing books / volunteering?</label>
      <div class="radio-row"><label><input type="radio" name="ob-vol" value="yes" checked> Yes, I'd like to help review</label>
      <label><input type="radio" name="ob-vol" value="no"> No, just browsing</label></div>
    </div>
    <div class="field"><label>Anything else you'd like to share? (optional)</label><textarea id="ob-extra" rows="3"></textarea></div>
    <div class="row" style="margin-top:1rem"><button class="btn primary" id="ob-submit">Submit</button></div>
  `);
  $("ob-submit").onclick = async () => {
    const wants = document.querySelector('input[name="ob-vol"]:checked').value === "yes";
    const btn = $("ob-submit"); btn.disabled = true;
    try {
      const r = await api("/api/apply", { method: "POST", body: {
        name: $("ob-name").value, place: $("ob-place").value, wants_to_volunteer: wants, extra_info: $("ob-extra").value,
      } });
      me = { ...me, ...r.user }; perms = r.permissions;
      closeModal();
      if (wants) {
        toast("Thanks! An admin will review your application.");
        renderWho();
      } else {
        toast("No problem — you're browsing as a guest.");
        if (CFG.AUTH_MODE === "dev") {
          localStorage.removeItem("lipi_token"); me = null; perms = ["read"];
          renderWho(); route();
        } else if (firebaseApp) {
          firebase.auth().signOut().catch(() => {});  // onAuthStateChanged (below) picks this up
        }
      }
    } catch (e) { toast(e.message); btn.disabled = false; }
  };
}

/* ---------------- router ---------------- */
async function route() {
  const [path, query = ""] = (location.hash.slice(1) || "/").split("?");
  const q = new URLSearchParams(query);
  const seg = path.split("/").filter(Boolean);
  const view = $("view");
  if (me && me.banned) {
    view.innerHTML = `<div class="center err">Your account has been banned${me.ban_reason ? ": " + esc(me.ban_reason) : ""}.<br>Contact an admin if you think this is a mistake.</div>`;
    return;
  }
  view.innerHTML = '<div class="center">Loading…</div>';
  try {
    if (seg.length === 0 || seg[0] === "dashboard") await dashboardView(view);  // dashboard is home now
    else if (seg[0] === "library") await libraryView(view);
    else if (seg[0] === "book" && seg[2] === "page") await pageView(view, seg[1], parseInt(seg[3] || "1", 10), q.get("focus"));
    else if (seg[0] === "book" && seg[2] === "read") await readView(view, seg[1], parseInt(seg[3] || "1", 10));
    else if (seg[0] === "admin") await adminView(view, seg[1]);
    else view.innerHTML = '<div class="center">Not found</div>';
  } catch (e) {
    view.innerHTML = `<div class="center err">${esc(e.message)}<br><br><a href="#/">Back to the dashboard</a></div>`;
  }
}
/* Unsent edits: cards whose text differs from what was loaded. Leaving the page (a link, the Go box,
   Back, closing the tab) first asks the person to click "Suggest this edit". */
const unsentCount = () => document.querySelectorAll("#view .snip.unsent").length;
let lastHash = location.hash, revertingHash = false;
window.addEventListener("hashchange", () => {
  if (revertingHash) { revertingHash = false; return; }
  const n = unsentCount();
  if (n && !confirm(`You changed the text of ${n} snippet${n === 1 ? "" : "s"} but haven't clicked "Suggest this edit".\n\nPress Cancel to stay on this page and click "Suggest this edit" first, or OK to leave and lose the changes.`)) {
    revertingHash = true; location.hash = lastHash; return;
  }
  lastHash = location.hash;
  route();
});
window.addEventListener("beforeunload", (e) => { if (unsentCount()) { e.preventDefault(); e.returnValue = ""; } });

/* ---------------- library ---------------- */
async function libraryView(view) {
  const { books } = await api("/api/library");
  if (!books.length) { view.innerHTML = '<div class="center">No books published yet. Finish an OCR run in the intake app and it appears here.</div>'; return; }
  view.innerHTML = `<h1>Library</h1><div class="grid">${books.map(bookCard).join("")}</div>`;
}
function bookCard(b) {
  const attn = b.needs_attention ? `<div class="attn"><b>${b.needs_attention}</b>need attention</div>` : `<div><b>0</b>need attention</div>`;
  return `<div class="card">
    <h2>${esc(b.title || b.id)}</h2>
    <div class="kavi">${esc(b.kavi || "")} <span class="dim">· ${esc(b.id)} · ${b.page_count || "?"} pages</span></div>
    <div class="small dim">${b.completion_pct}% finalized</div>
    <div class="bar"><i style="width:${b.completion_pct}%"></i></div>
    <div class="stats">
      <div><b>${b.total}</b>snippets</div><div><b>${b.finalized}</b>finalized</div><div><b>${b.pending}</b>pending</div>
      <div><b>${b.awaiting_approval}</b>awaiting approval</div><div><b>${b.untouched}</b>untouched</div>${attn}
    </div>
    ${b.finalized_challenged ? `<div class="small" style="color:var(--amber)">${b.finalized_challenged} finalized snippet(s) challenged by reviewers</div>` : ""}
    <div class="row"><a class="btn primary" href="#/book/${esc(b.id)}/page/1">Review pages</a><a class="btn" href="#/book/${esc(b.id)}/read/1">Read full text</a></div>
  </div>`;
}

/* ---------------- dashboard ---------------- */
async function dashboardView(view) {
  const d = await api("/api/dashboard");
  const leaderboard = (rows, key, emptyMsg) => rows.length
    ? `<ol class="leaderboard">${rows.map((r) => `<li><span>${esc(r.name || "(no name)")} <span class="dim small">${esc(r.role)}</span></span><b>${r[key]}</b></li>`).join("")}</ol>`
    : `<p class="dim small">${emptyMsg}</p>`;
  const pendingList = d.pending_admin_review.length
    ? `<ul class="plain-list">${d.pending_admin_review.map((b) => `<li><a href="#/book/${esc(b.id)}/page/1">${esc(b.title || b.id)}</a> <span class="dim small">${b.needs_attention} snippet(s) awaiting approval</span></li>`).join("")}</ul>`
    : `<p class="dim small">Nothing pending - every reviewer-agreed change has already been approved.</p>`;
  const participationList = d.participation.length
    ? `<ol class="leaderboard">${d.participation.slice(0, 10).map((p) => `<li><span>${esc(p.title || p.book_id)}</span><b>${p.participants}</b></li>`).join("")}</ol>`
    : `<p class="dim small">No reviewer activity yet.</p>`;

  view.innerHTML = `<h1>Dashboard</h1>
    <div class="grid">
      <div class="card"><h2>Top reviewers - most suggestions</h2>${leaderboard(d.top_suggesters, "suggestions", "No suggestions yet.")}</div>
      <div class="card"><h2>Top reviewers - most adopted</h2>${leaderboard(d.top_approved, "approved", "No suggestions have been adopted into finalized text yet.")}</div>
      <div class="card">
        <h2>Review trends</h2>
        <div class="stats">
          <div><b>${d.trends.books_reviewed}</b>books reviewed</div>
          <div><b>${d.trends.pages_reviewed}</b>pages reviewed</div>
          <div><b>${d.trends.words_reviewed}</b>words reviewed</div>
        </div>
      </div>
      <div class="card"><h2>Pending admin review <span class="dim small">(${d.pending_admin_review.length})</span></h2>${pendingList}</div>
      <div class="card"><h2>Books by participation</h2>${participationList}</div>
    </div>
    <h2 style="margin-top:1.2rem">Books completed</h2>
    <div id="dash-books-table"></div>
    ${can("manage_books") ? `<div class="row" style="margin-top:1rem"><button class="btn primary" id="dash-backup">Download all books (backup)</button></div>` : ""}`;

  const bookRows = d.books.map((b) => ({ ...b, title: b.title || b.id }));
  let sort = { key: "completion_pct", dir: -1 };
  const drawBooks = () => {
    const rows = [...bookRows].sort((a, b) => (typeof a[sort.key] === "string" ? String(a[sort.key]).localeCompare(String(b[sort.key])) : a[sort.key] - b[sort.key]) * sort.dir);
    const cols = [["title", "Title"], ["completion_pct", "Completion %"], ["finalized", "Finalized"], ["total", "Total"]];
    $("dash-books-table").innerHTML = `<table><thead><tr>${cols.map(([k, l]) => `<th data-sort="${k}" class="sortable">${l}${sort.key === k ? (sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead><tbody>${rows.map((b) =>
      `<tr><td><a href="#/book/${esc(b.id)}/page/1">${esc(b.title)}</a></td><td>${b.completion_pct}%</td><td>${b.finalized}</td><td>${b.total}</td></tr>`).join("")}</tbody></table>`;
    document.querySelectorAll("#dash-books-table [data-sort]").forEach((th) => (th.onclick = () => { sort = { key: th.dataset.sort, dir: sort.key === th.dataset.sort ? -sort.dir : -1 }; drawBooks(); }));
  };
  drawBooks();
  const backupBtn = $("dash-backup");
  if (backupBtn) backupBtn.onclick = async () => {
    backupBtn.disabled = true; const was = backupBtn.textContent; backupBtn.textContent = "Preparing…";
    try { await downloadFile("/api/admin/books/backup", `lipi-sampada-backup-${new Date().toISOString().slice(0, 10)}.zip`); }
    finally { backupBtn.disabled = false; backupBtn.textContent = was; }
  };
}

/* ---------------- per-word rendering ---------------- */
function renderText(s) {
  const text = s.current_text || "";
  const spans = tokenize(text);  // [{text, start, end}, ...] - same \S+ tokenization the backend's
                                  // word_changes indices are computed against (reviewapi/tally.py)
  const mark = new Array(spans.length).fill(null);
  const ins = {};
  for (const g of s.tally.word_changes) {
    const who = `${g.count} reviewer${g.count === 1 ? "" : "s"}${g.editors ? `, ${g.editors} editor` : ""}${g.guests ? `, ${g.guests} guest` : ""}`;
    const tip = `${g.original || "(nothing)"} → ${g.replacement || "(delete)"} : ${who}`;
    if (g.start === g.end) ins[g.start] = (ins[g.start] ? ins[g.start] + "\n" : "") + tip;
    else for (let i = g.start; i < g.end; i++) if (!mark[i] || g.count > mark[i].count) mark[i] = { count: g.count, tip };
  }
  const insertion = (i) => (ins[i] ? `<span class="ins" title="${esc(ins[i])}">‸</span> ` : "");
  // Emits each word plus whatever whitespace originally separated it from the previous one (verbatim,
  // via esc() so a literal newline survives as-is - see the paired white-space:pre-wrap on .text/.read
  // p in style.css) instead of always joining with a hardcoded " ", which used to flatten any line
  // break in current_text regardless of whether the backend now preserves it.
  let html = "";
  let lastEnd = 0;
  spans.forEach((sp, i) => {
    html += esc(text.slice(lastEnd, sp.start)) + insertion(i)
      + (mark[i] ? `<span class="w chg ${mark[i].count >= 2 ? "hot" : ""}" title="${esc(mark[i].tip)}">${esc(sp.text)}</span>` : esc(sp.text));
    lastEnd = sp.end;
  });
  html += esc(text.slice(lastEnd));
  return html + (ins[spans.length] ? ` <span class="ins" title="${esc(ins[spans.length])}">‸</span>` : "");
}

/* ---------------- word-replacement popup: select text to see what the other engines read there ---------------- */
function tokenize(str) {
  const tokens = []; const re = /\S+/g; let m;
  while ((m = re.exec(str || "")) !== null) tokens.push({ text: m[0], start: m.index, end: m.index + m[0].length });
  return tokens;
}
// LCS word-alignment via O(n*m) DP; returns monotonic matched (aIdx,bIdx) anchor pairs.
function alignTokens(aTokens, bTokens, bText) {
  const n = aTokens.length, m = bTokens.length;
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = 1; i <= n; i++) for (let j = 1; j <= m; j++)
    dp[i][j] = aTokens[i - 1].text === bTokens[j - 1].text ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
  const matches = []; let i = n, j = m;
  while (i > 0 && j > 0) {
    if (aTokens[i - 1].text === bTokens[j - 1].text) { matches.push({ aIdx: i - 1, bIdx: j - 1 }); i--; j--; }
    else if (dp[i - 1][j] >= dp[i][j - 1]) i--; else j--;
  }
  matches.reverse();
  return { aTokens, bTokens, bText: bText || "", matches };
}
function alignmentScore(a) {
  if (a.aTokens.length === 0 && a.bTokens.length === 0) return 100;
  return Math.round((2 * a.matches.length) / (a.aTokens.length + a.bTokens.length) * 100);
}
// Projects a [selA, selB) current-token range through the alignment onto the engine token range,
// by bracketing with the nearest matched anchors and taking the gap on the engine side.
function projectSelection(a, selA, selB) {
  const { bTokens, matches } = a;
  if (bTokens.length === 0 || alignmentScore(a) < 15) return null;
  let loB = -1, hiB = bTokens.length;
  for (const mt of matches) { if (mt.aIdx < selA) loB = Math.max(loB, mt.bIdx); if (mt.aIdx >= selB) { hiB = mt.bIdx; break; } }
  const gapLen = hiB - loB - 1;
  if (gapLen > Math.max(6, (selB - selA) * 4)) return null; // likely alignment noise, not a real correspondence
  if (gapLen <= 0) return "";
  return a.bText.slice(bTokens[loB + 1].start, bTokens[hiB - 1].end);
}
function setScoreBadge(el, pct) {
  if (!el) return;
  el.textContent = pct + "%";
  el.className = "engine-score " + (pct >= 90 ? "high" : pct >= 70 ? "mid" : "low");
}
function getCaretCoords(textarea, position) {
  const mirror = document.createElement("div");
  const style = getComputedStyle(textarea);
  ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "padding", "border", "boxSizing", "whiteSpace", "wordWrap"]
    .forEach((p) => (mirror.style[p] = style[p]));
  Object.assign(mirror.style, { position: "absolute", visibility: "hidden", width: textarea.clientWidth + "px", whiteSpace: "pre-wrap", wordWrap: "break-word" });
  mirror.textContent = textarea.value.substring(0, position);
  const marker = document.createElement("span"); marker.textContent = "​"; mirror.appendChild(marker);
  document.body.appendChild(mirror);
  const rect = textarea.getBoundingClientRect(), markerRect = marker.getBoundingClientRect(), mirrorRect = mirror.getBoundingClientRect();
  const coords = {
    x: rect.left + (markerRect.left - mirrorRect.left) - textarea.scrollLeft,
    y: rect.top + (markerRect.top - mirrorRect.top) - textarea.scrollTop + parseFloat(style.lineHeight || "20"),
  };
  document.body.removeChild(mirror);
  return coords;
}

let suggestActiveTa = null, suggestSnap = null;
function closeSuggestPopup() { $("suggest-popup").hidden = true; suggestActiveTa = null; suggestSnap = null; }
function applySuggestion(text) {
  if (!suggestActiveTa || !suggestSnap) return;
  const ta = suggestActiveTa, scrollTop = ta.scrollTop;
  ta.focus();
  ta.setRangeText(text, suggestSnap.start, suggestSnap.end, "end");
  ta.scrollTop = scrollTop;
  ta.dispatchEvent(new Event("input", { bubbles: true })); // reruns the unsent-edit / dirty-tracking logic
  closeSuggestPopup();
}
function showSuggestPopup(ta, raw) {
  if (ta.readOnly || !raw) return;
  const selStart = ta.selectionStart, selEnd = ta.selectionEnd;
  if (selStart === selEnd) { closeSuggestPopup(); return; }
  const curTokens = tokenize(ta.value);
  if (curTokens.length === 0) return;
  let selA = curTokens.findIndex((t) => t.end > selStart); if (selA === -1) selA = curTokens.length - 1;
  let selB = selA;
  for (let k = curTokens.length - 1; k >= 0; k--) if (curTokens[k].start < selEnd) { selB = k; break; }
  selB += 1; if (selB <= selA) selB = selA + 1;
  suggestActiveTa = ta;
  suggestSnap = { start: curTokens[selA].start, end: curTokens[selB - 1].end };
  ta.selectionStart = suggestSnap.start; ta.selectionEnd = suggestSnap.end; // reflect the word-boundary snap

  const candidatesEl = $("suggest-candidates");
  candidatesEl.innerHTML = "";
  for (const [label, key] of [["EasyOCR", "easyocr"], ["Tesseract", "tesseract"], ["Surya", "surya"]]) {
    const alignment = alignTokens(curTokens, tokenize(raw[key]), raw[key]);
    const proj = projectSelection(alignment, selA, selB);
    const row = document.createElement("div");
    if (proj === null) {
      row.className = "suggest-cand no-match";
      row.innerHTML = `<span class="suggest-cand-label">${esc(label)}</span><span class="suggest-cand-text">no clear match</span>`;
    } else {
      row.className = "suggest-cand";
      row.innerHTML = `<span class="suggest-cand-label">${esc(label)}</span><span class="suggest-cand-text">${proj ? esc(proj) : "<em>(delete)</em>"}</span>`;
      row.onclick = () => applySuggestion(proj);
    }
    candidatesEl.appendChild(row);
  }
  const coords = getCaretCoords(ta, selStart);
  const popup = $("suggest-popup");
  popup.hidden = false;
  const maxLeft = window.innerWidth - popup.offsetWidth - 12, maxTop = window.innerHeight - popup.offsetHeight - 12;
  popup.style.left = Math.max(8, Math.min(coords.x, maxLeft)) + "px";
  popup.style.top = Math.max(8, Math.min(coords.y, maxTop)) + "px";
  $("suggest-custom-input").value = ta.value.slice(suggestSnap.start, suggestSnap.end);
}
$("suggest-custom-apply").onclick = () => applySuggestion($("suggest-custom-input").value);
$("suggest-custom-input").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("suggest-custom-apply").click(); } });
document.addEventListener("mousedown", (e) => {
  const popup = $("suggest-popup");
  if (!popup.hidden && !popup.contains(e.target) && e.target !== suggestActiveTa) closeSuggestPopup();
});
window.addEventListener("hashchange", closeSuggestPopup);

function updateScoreBadges(card) {
  if (!card._raw) return;
  const ta = card.querySelector("[data-edit]");
  const curTokens = tokenize(ta.value);
  for (const key of ["easyocr", "tesseract", "surya"]) {
    const el = card.querySelector(`.engine-score[data-eng="${key}"]`);
    if (el) setScoreBadge(el, alignmentScore(alignTokens(curTokens, tokenize(card._raw[key]), card._raw[key])));
  }
}
function wireWordSuggestions(card, s, book) {
  const ta = card.querySelector("[data-edit]");
  let scoreDebounce = null, selectDebounce = null;
  getBundle(book).then((bundle) => { card._raw = bundle && bundle[s.id]; updateScoreBadges(card); });
  const select = () => showSuggestPopup(ta, card._raw);
  ta.addEventListener("mouseup", () => { clearTimeout(selectDebounce); selectDebounce = setTimeout(select, 120); });
  ta.addEventListener("keyup", (e) => {
    if (["Shift", "Control", "Alt", "Meta"].includes(e.key) || e.shiftKey || e.key.startsWith("Arrow")) {
      clearTimeout(selectDebounce); selectDebounce = setTimeout(select, 120);
    }
  });
  ta.addEventListener("input", () => { clearTimeout(scoreDebounce); scoreDebounce = setTimeout(() => updateScoreBadges(card), 150); });
}

/* ---------------- snippet card ---------------- */
// One line telling a reviewer/editor what, if anything, they should actually do with this snippet -
// the amber styling flags *that* something needs eyes on it; this says *what*.
function hintFor(s, t) {
  if (s.finalized) {
    if (!t.needs_attention) return "";
    return isAdmin()
      ? "<b>Challenged.</b> Reviewers disagree with this finalized text - Re-open it below to reconsider."
      : "<b>Challenged.</b> Reviewers have suggested this finalized text may be wrong - an admin can re-open it.";
  }
  if (t.needs_attention) {
    return isEditor()
      ? "<b>Reviewers agree on a change.</b> Accept the highlighted word change below, or Approve as is / with your own edit."
      : "<b>Reviewers agree on a change.</b> An editor will review and approve it soon.";
  }
  if (s.suggestion_count) {
    return isEditor()
      ? "<b>A suggestion is awaiting more input.</b> You can Accept a word change below, or Approve directly if you're confident."
      : "<b>A suggestion is here.</b> Read it, then click \"Looks right\" if you agree, or suggest your own edit.";
  }
  return "";
}

function snipHtml(s, book) {
  const t = s.tally;
  const pills = [
    s.finalized ? '<span class="pill final">finalized</span>' : (s.suggestion_count ? '<span class="pill sug">awaiting approval</span>' : '<span class="pill">untouched</span>'),
    t.needs_attention ? `<span class="pill attn">${s.finalized ? "challenged" : "needs attention"}</span>` : "",
    s.refine_failed ? '<span class="pill warn" title="the AI step timed out; this is the best non-AI reading">AI step skipped</span>' : "",
  ].join(" ");
  const hint = hintFor(s, t);
  const chips = t.word_changes.map((g, i) => `
    <div class="chip"><span class="diff-old">${esc(g.original || "∅")}</span><span class="arrow">→</span><span class="diff-new">${esc(g.replacement || "(delete)")}</span>
      <span class="cnt ${g.count >= 2 ? "hot" : ""}" title="${esc(g.reviewers.map((r) => r.name + " (" + r.role + ")").join(", "))}">×${g.count}</span>
      ${g.editors ? `<span class="small dim">${g.editors} editor</span>` : ""}${g.guests ? `<span class="small dim">+${g.guests} guest</span>` : ""}
      ${isEditor() && !s.finalized ? `<button class="btn good" data-accept="${i}">Accept</button>` : ""}
    </div>`).join("");
  const votes = `<div class="votes">👥 ${s.reviewer_count} reviewer${s.reviewer_count === 1 ? "" : "s"} have looked at this · ✓ ${t.confirms.reviewers} confirmed as-is${t.confirms.guests ? ` (+${t.confirms.guests} guest)` : ""} · ${t.edit_suggestions} suggested edit${t.edit_suggestions === 1 ? "" : "s"}${s.my_suggestion ? " · <b>you've weighed in</b>" : ""}</div>`;
  const suggestionsList = (s.suggestions || []).length ? `<details class="suggestions-list"><summary>Suggested edits, in full (${s.suggestions.length})</summary>
    ${s.suggestions.map((sug) => `<div class="suggestion-item"><div class="small dim">${esc(sug.name || "someone")} (${esc(sug.role)}) · ${esc(new Date(sug.created_at).toLocaleString())}</div><div class="suggestion-text">${esc(sug.text)}</div></div>`).join("")}
  </details>` : "";
  const mine = s.my_suggestion && s.my_suggestion.text ? s.my_suggestion.text : s.current_text;
  const flagged = t.needs_attention || (s.suggestion_count > 0 && !s.finalized);
  return `<div class="snip ${s.finalized ? "final" : ""} ${flagged ? "attn" : ""}" data-id="${esc(s.id)}">
    <div class="snip-head"><b>#${s.seq + 1}</b>${pills}</div>
    ${hint ? `<div class="hint">${hint}</div>` : ""}
    ${s.snippet_image_url ? `<div class="snip-img"><img src="${esc(s.snippet_image_url)}" loading="lazy" alt="snippet"></div>` : ""}
    <div class="text">${renderText(s)}</div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}${votes}${suggestionsList}
    <textarea lang="kn" data-edit>${esc(mine)}</textarea>
    <div class="actions">
      <button class="btn good" data-confirm>Looks right</button>
      <button class="btn primary" data-suggest hidden>Suggest this edit</button>
      ${isEditor() ? `<button class="btn warn" data-final-as-is>${s.finalized ? "Re-approve current" : "Approve as is"}</button><button class="btn warn" data-final-edit hidden>Approve my text</button>` : ""}
      ${isAdmin() && s.finalized ? '<button class="btn danger" data-reopen>Re-open</button>' : ""}
      ${isAdmin() ? '<button class="btn danger" data-delete>Delete snippet</button>' : ""}
    </div>
    <details data-raw><summary>Raw engine readings</summary><div class="raw-body dim small">Loading…</div></details>
  </div>`;
}

function bindSnip(card, s, book) {
  const id = s.id;
  const text = () => card.querySelector("[data-edit]").value.trim();
  const act = async (fn, okMsg) => {
    try { const r = await fn(); if (okMsg) toast(okMsg); await refreshSnip(card, book); return r; }
    catch (e) { toast(e.message); }
  };
  // Editing the text swaps which buttons apply (confirm/approve-as-is only make sense
  // unedited; suggest/approve-my-text only make sense once something's changed), and
  // marks the card as having unsent changes, which the leave-page guard below watches.
  const ta = card.querySelector("[data-edit]");
  const sync = () => {
    const dirty = ta.value.trim() !== ta.defaultValue.trim();
    card.classList.toggle("unsent", dirty);
    const confirmBtn = card.querySelector("[data-confirm]");
    const suggestBtn = card.querySelector("[data-suggest]");
    const asIsBtn = card.querySelector("[data-final-as-is]");
    const editBtn = card.querySelector("[data-final-edit]");
    if (confirmBtn) confirmBtn.hidden = dirty;
    if (suggestBtn) suggestBtn.hidden = !dirty;
    if (asIsBtn) asIsBtn.hidden = dirty;
    if (editBtn) editBtn.hidden = !dirty;
  };
  ta.addEventListener("input", sync);
  sync();
  card.querySelector("[data-confirm]").onclick = () => act(() => api("/api/suggest", { method: "POST", body: { snippet_id: id, kind: "confirm" } }), "Recorded: looks right");
  card.querySelector("[data-suggest]").onclick = () => act(() => api("/api/suggest", { method: "POST", body: { snippet_id: id, kind: "edit", text: text() } }), "Suggestion saved");
  card.querySelectorAll("[data-accept]").forEach((b) => (b.onclick = () => {
    const g = s.tally.word_changes[+b.dataset.accept];
    act(() => api("/api/accept-word", { method: "POST", body: { snippet_id: id, start: g.start, end: g.end, replacement: g.replacement } }), "Word change accepted");
  }));
  const fa = card.querySelector("[data-final-as-is]"); if (fa) fa.onclick = () => act(() => api("/api/finalize", { method: "POST", body: { snippet_id: id } }), "Finalized");
  const fe = card.querySelector("[data-final-edit]"); if (fe) fe.onclick = () => act(() => api("/api/finalize", { method: "POST", body: { snippet_id: id, text: text() } }), "Finalized with your text");
  const ro = card.querySelector("[data-reopen]"); if (ro) ro.onclick = () => act(() => api("/api/unfinalize", { method: "POST", body: { snippet_id: id } }), "Re-opened");
  const del = card.querySelector("[data-delete]");
  if (del) del.onclick = async () => {
    if (!confirm("Delete this snippet permanently? Its image and all review data (suggestions, finalization) will be removed. This cannot be undone.")) return;
    try {
      const r = await api(`/api/admin/snippets/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(r.storage_warning ? `Deleted, but image cleanup failed: ${r.storage_warning}` : "Snippet deleted");
      card.remove();
    } catch (e) { toast(e.message); }
  };
  card.querySelector("[data-raw]").addEventListener("toggle", async (e) => {
    if (!e.target.open) return;
    const body = card.querySelector(".raw-body");
    const bundle = await getBundle(book);
    const r = bundle && bundle[id];
    body.innerHTML = r
      ? [["EasyOCR", "easyocr"], ["Tesseract", "tesseract"], ["Surya", "surya"]].map(([label, key]) =>
          `<div class="raw"><b>${label}</b><span class="engine-score" data-eng="${key}"></span>${esc(r[key] || "(empty)")}</div>`).join("")
      : "Raw readings are not available for this book.";
    updateScoreBadges(card); // in case the bundle finished loading before this was ever opened
  }, { once: true });
  wireWordSuggestions(card, s, book);
}
async function refreshSnip(card, book) {
  const s = await api("/api/snippet?id=" + encodeURIComponent(card.dataset.id));
  const wasOpen = card.querySelector("[data-raw]").open;
  const tmp = document.createElement("div"); tmp.innerHTML = snipHtml(s, book);
  const fresh = tmp.firstElementChild; card.replaceWith(fresh); bindSnip(fresh, s, book);
  if (wasOpen) fresh.querySelector("[data-raw]").open = true;
}
async function getBundle(book) {
  if (book.id in bundles) return bundles[book.id];
  bundles[book.id] = null;
  try {
    const r = await fetch(book.bundle_url);
    bundles[book.id] = await new Response(r.body.pipeThrough(new DecompressionStream("gzip"))).json();
  } catch (e) { /* older browsers: raw readings simply unavailable */ }
  return bundles[book.id];
}

/* ---------------- page view (per-page text + full image + pagination) ---------------- */
async function pageView(view, bookId, pageIndex, focusId) {
  const stripPage = Math.max(1, Math.ceil(pageIndex / STRIP));
  const [{ book }, page, strip] = await Promise.all([
    api(`/api/books/${bookId}`), api(`/api/books/${bookId}/pages/${pageIndex}`), api(`/api/books/${bookId}/pages?page=${stripPage}&per_page=${STRIP}`),
  ]);
  const N = page.total_pages;
  const go = (n) => `#/book/${bookId}/page/${Math.min(Math.max(1, n), N)}`;
  const stripHtml = strip.pages.map((p) => {
    const cls = p.finalized === p.total ? "done" : (p.needs_attention ? "attn" : "");
    return `<a class="pg ${cls} ${p.page_index === pageIndex ? "cur" : ""}" href="${go(p.page_index)}" title="${p.finalized}/${p.total} finalized">${p.page_index}</a>`;
  }).join("");
  const prevStrip = stripPage > 1 ? `<a class="pg" href="${go((stripPage - 2) * STRIP + 1)}">«</a>` : "";
  const nextStrip = stripPage * STRIP < N ? `<a class="pg" href="${go(stripPage * STRIP + 1)}">»</a>` : "";
  const pageDone = page.snippets.every((s) => s.finalized);

  view.innerHTML = `
    <div class="toolbar"><a href="#/library">← Library</a><b>${esc(book.title || book.id)}</b><span class="dim small">${book.completion_pct}% finalized</span>
      <span style="flex:1"></span><a class="btn" href="#/book/${bookId}/read/${pageIndex}">Read as full text</a></div>
    <div class="toolbar">
      <a class="btn" href="${go(1)}">⏮ First</a><a class="btn" href="${go(pageIndex - 1)}" ${page.prev ? "" : 'style="visibility:hidden"'}>◀ Prev</a>
      <span>Page <b>${pageIndex}</b> of ${N}${pageLabel(page) !== String(pageIndex) ? ` <span class="dim small">(printed ${esc(pageLabel(page))})</span>` : ""}</span>
      <a class="btn" href="${go(pageIndex + 1)}" ${page.next ? "" : 'style="visibility:hidden"'}>Next ▶</a><a class="btn" href="${go(N)}">Last ⏭</a>
      <input type="number" id="goto" min="1" max="${N}" placeholder="page #"><button class="btn" id="goto-btn">Go</button>
      <span style="flex:1"></span>
      ${isEditor() ? `<button class="btn warn" id="approve-page" ${pageDone ? "disabled" : ""}>Approve page…</button>` : ""}
    </div>
    <div class="strip">${prevStrip}${stripHtml}${nextStrip}</div>
    <div class="page-layout">
      <div class="page-img">${page.page_image_url ? `<img src="${esc(page.page_image_url)}" alt="full page">` : ""}</div>
      <div id="snips">${page.snippets.map((s) => snipHtml(s, book)).join("")}</div>
    </div>`;

  $("goto-btn").onclick = () => { const n = parseInt($("goto").value, 10); if (n) location.hash = go(n); };
  $("goto").addEventListener("keydown", (e) => { if (e.key === "Enter") $("goto-btn").click(); });
  view.querySelectorAll(".snip").forEach((card, i) => bindSnip(card, page.snippets[i], book));
  if (focusId) {
    const el = [...view.querySelectorAll(".snip")].find((c) => c.dataset.id === focusId);
    if (el) { el.classList.add("focus"); el.scrollIntoView({ block: "center" }); }
  }
  const ap = $("approve-page");
  if (ap) ap.onclick = () => approvePageDialog(bookId, pageIndex);
}

async function approvePageDialog(bookId, pageIndex) {
  let plan;
  try { plan = await api(`/api/books/${bookId}/pages/${pageIndex}/approve`, { method: "POST", body: { preview: true } }); }
  catch (e) { toast(e.message); return; }
  const changed = plan.snippets.filter((p) => p.changes_applied).length;
  showModal(`<h2>Approve page ${pageIndex}</h2>
    <p class="dim small">Finalizes the ${plan.count} snippet(s) not yet finalized. Only word changes that at least two signed-in reviewers agreed on <i>and</i> that outnumber the "looks right" votes are applied; everything else stays as it is.
    <b>${changed}</b> snippet(s) will change.</p>
    <table><thead><tr><th>#</th><th>Before</th><th>After</th></tr></thead><tbody>${plan.snippets.map((p) =>
      `<tr><td>${p.seq + 1}</td><td>${esc(p.before)}</td><td class="${p.changes_applied ? "diff-new" : ""}">${esc(p.after)}</td></tr>`).join("")}</tbody></table>
    <div class="actions" style="margin-top:1rem"><button class="btn warn" id="confirm-approve">Approve ${plan.count} snippet(s)</button><button class="btn" id="cancel-approve">Cancel</button></div>`);
  $("cancel-approve").onclick = closeModal;
  $("confirm-approve").onclick = async () => {
    try { await api(`/api/books/${bookId}/pages/${pageIndex}/approve`, { method: "POST", body: { preview: false } }); toast("Page approved"); closeModal(); route(); }
    catch (e) { toast(e.message); }
  };
}

/* ---------------- full-text reading view ---------------- */
async function readView(view, bookId, start) {
  const COUNT = 5;
  const [{ book }, data] = await Promise.all([api(`/api/books/${bookId}`), api(`/api/books/${bookId}/read?start=${start}&count=${COUNT}`)]);
  const N = data.total_pages;
  const to = (n) => `#/book/${bookId}/read/${Math.min(Math.max(1, n), Math.max(1, N - COUNT + 1))}`;
  view.innerHTML = `
    <div class="toolbar"><a href="#/library">← Library</a><b>${esc(book.title || book.id)}</b><span class="dim small">${book.completion_pct}% finalized · pages ${start}-${Math.min(N, start + COUNT - 1)} of ${N}</span>
      <span style="flex:1"></span>
      <a class="btn" target="_blank" href="${CFG.API_BASE}/api/books/${bookId}/text?use=current">Text</a>
      <a class="btn" target="_blank" href="${CFG.API_BASE}/api/books/${bookId}/text?use=final">Finalized only</a></div>
    <div class="toolbar"><a class="btn" href="${to(1)}">⏮ First</a><a class="btn" href="${to(start - COUNT)}">◀ Previous ${COUNT}</a>
      <a class="btn" href="${to(start + COUNT)}">Next ${COUNT} ▶</a><a class="btn" href="${to(N)}">Last ⏭</a>
      <input type="number" id="goto" min="1" max="${N}" placeholder="page #"><button class="btn" id="goto-btn">Go</button></div>
    <div class="legend"><span><i style="background:#2f7d3f"></i>finalized</span><span><i style="background:var(--amber)"></i>has a suggestion - needs review</span><span>click any paragraph to open it for review</span></div>
    <div class="read">${data.pages.map((p) => `
      <div class="pagehead"><span>Page ${p.page_index}${pageLabel(p) !== String(p.page_index) ? ` (printed ${esc(pageLabel(p))})` : ""}</span><a href="#/book/${bookId}/page/${p.page_index}">open page →</a></div>
      ${p.snippets.map((s) => `<p class="${s.finalized ? "final" : ""} ${s.suggestion_count && !s.finalized ? "sug" : ""} ${s.attention ? "attn" : ""}" data-page="${p.page_index}" data-id="${esc(s.id)}">${esc(s.text)}</p>`).join("")}`).join("")}
    </div>`;
  view.querySelectorAll(".read p").forEach((p) => (p.onclick = () => { location.hash = `#/book/${bookId}/page/${p.dataset.page}?focus=${encodeURIComponent(p.dataset.id)}`; }));
  $("goto-btn").onclick = () => { const n = parseInt($("goto").value, 10); if (n) location.hash = to(n); };
  $("goto").addEventListener("keydown", (e) => { if (e.key === "Enter") $("goto-btn").click(); });
}

/* ---------------- admin: users, contributors, books, permissions ---------------- */
const ADMIN_TABS = [["users", "Users"], ["applicants", "Applicants"], ["contributors", "Contributors"], ["books", "Books"], ["permissions", "Permissions"]];
const APP_STATUS_LABEL = { new: "hasn't answered yet", pending: "application pending", declined: "declined to volunteer", rejected: "application rejected", approved: "approved", unset: "" };
const ago = (ts) => {
  if (!ts) return "never";
  const m = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 60000));
  return m < 2 ? "just now" : m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
};

async function adminView(view, tab = "users") {
  if (!can("manage_users")) throw new Error("This page is for admins.");
  view.innerHTML = `<div class="tabs">${ADMIN_TABS.map(([k, l]) => `<a href="#/admin/${k}" class="${k === tab ? "cur" : ""}">${l}</a>`).join("")}</div><div id="admin-body"></div>`;
  const fn = { users: adminUsers, applicants: adminApplicants, contributors: adminContributors, books: adminBooks, permissions: adminPermissions }[tab] || adminUsers;
  await fn($("admin-body"));
}

async function adminUsers(body) {
  const f = { q: "", role: "" };
  const assignable = ["reviewer", "editor", "admin", ...(can("grant_superadmin") ? ["superadmin"] : [])];
  body.innerHTML = `
    <h1>Users &amp; roles</h1>
    <div class="toolbar"><input type="text" id="u-q" placeholder="Search name or email" style="min-width:16rem">
      <select id="u-role"><option value="">All roles</option>${["reviewer", "editor", "admin", "superadmin"].map((r) => `<option>${r}</option>`).join("")}</select></div>
    <div id="u-table"></div>
    <h2>Invite someone</h2>
    <p class="dim small">Pick the role they should start with. It is applied the first time they sign in with that email; until then it waits below.</p>
    <div class="toolbar"><input type="text" id="i-email" placeholder="name@gmail.com" style="min-width:16rem">
      <select id="i-role">${assignable.map((r) => `<option ${r === "editor" ? "selected" : ""}>${r}</option>`).join("")}</select>
      <button class="btn primary" id="i-add">Invite</button></div>
    <div id="i-list"></div>`;
  const draw = async () => {
    const [{ users }, { invites }] = await Promise.all([api(`/api/admin/users?q=${encodeURIComponent(f.q)}&role=${encodeURIComponent(f.role)}`), api("/api/admin/invites")]);
    $("u-table").innerHTML = users.length ? `<table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last seen</th><th></th></tr></thead><tbody>${users.map((u) => {
      const mine = u.uid === me.uid, superOther = u.role === "superadmin" && !can("grant_superadmin");
      const locked = mine || superOther;
      const statusBits = [
        u.banned ? `<span class="pill warn" title="${esc(u.ban_reason || "")}">banned</span>` : (u.active ? "" : '<span class="pill warn">deactivated</span>'),
        APP_STATUS_LABEL[u.application_status] && !["approved", "unset"].includes(u.application_status)
          ? `<span class="pill sug">${esc(APP_STATUS_LABEL[u.application_status])}</span>` : "",
      ].filter(Boolean).join(" ") || "active";
      return `<tr class="${u.active && !u.banned ? "" : "off"}"><td>${esc(u.name || "")}${mine ? ' <span class="dim small">(you)</span>' : ""}</td><td>${esc(u.email || u.uid)}</td>
        <td><select data-role="${esc(u.uid)}" ${locked ? "disabled" : ""}>${(assignable.includes(u.role) ? assignable : [...assignable, u.role]).map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}</select></td>
        <td>${statusBits}</td><td class="dim small">${esc(ago(u.last_seen))}</td>
        <td class="nowrap">${locked ? "" : `<button class="btn ${u.active ? "danger" : "good"}" data-active="${esc(u.uid)}" data-to="${u.active ? 0 : 1}">${u.active ? "Deactivate" : "Reactivate"}</button>
          <button class="btn ${u.banned ? "good" : "danger"}" data-ban="${esc(u.uid)}" data-to="${u.banned ? 0 : 1}">${u.banned ? "Unban" : "Ban"}</button>`}</td></tr>`;
    }).join("")}</tbody></table>` : '<p class="dim">No one matches.</p>';
    $("i-list").innerHTML = invites.length ? `<table><thead><tr><th>Waiting for</th><th>Role</th><th>Invited</th><th></th></tr></thead><tbody>${invites.map((i) =>
      `<tr><td>${esc(i.email)}</td><td>${esc(i.role)}</td><td class="dim small">${esc(ago(i.created_at))}</td><td><button class="btn" data-uninvite="${esc(i.email)}">Remove</button></td></tr>`).join("")}</tbody></table>` : '<p class="dim small">No pending invites.</p>';
    body.querySelectorAll("[data-role]").forEach((sel) => (sel.onchange = async () => {
      try { await api(`/api/admin/users/${encodeURIComponent(sel.dataset.role)}/role`, { method: "POST", body: { role: sel.value } }); toast("Role updated"); }
      catch (e) { toast(e.message); }
      draw();
    }));
    body.querySelectorAll("[data-active]").forEach((b) => (b.onclick = async () => {
      if (b.dataset.to === "0" && !confirm("Deactivate this account? They can still read, but can no longer suggest or approve.")) return;
      try { await api(`/api/admin/users/${encodeURIComponent(b.dataset.active)}/active`, { method: "POST", body: { active: b.dataset.to === "1" } }); toast("Updated"); }
      catch (e) { toast(e.message); }
      draw();
    }));
    body.querySelectorAll("[data-ban]").forEach((b) => (b.onclick = async () => {
      const banning = b.dataset.to === "1";
      let reason = "";
      if (banning) {
        reason = prompt("Ban this account - why? (shown to them, visible to other admins)") || "";
        if (reason.trim() === "" && !confirm("Ban with no reason given?")) return;
      }
      try { await api(`/api/admin/users/${encodeURIComponent(b.dataset.ban)}/ban`, { method: "POST", body: { banned: banning, reason } }); toast(banning ? "Banned" : "Unbanned"); }
      catch (e) { toast(e.message); }
      draw();
    }));
    body.querySelectorAll("[data-uninvite]").forEach((b) => (b.onclick = async () => {
      try { await api(`/api/admin/invites?email=${encodeURIComponent(b.dataset.uninvite)}`, { method: "DELETE" }); } catch (e) { toast(e.message); }
      draw();
    }));
  };
  let t; $("u-q").oninput = (e) => { f.q = e.target.value; clearTimeout(t); t = setTimeout(draw, 250); };
  $("u-role").onchange = (e) => { f.role = e.target.value; draw(); };
  $("i-add").onclick = async () => {
    try { await api("/api/admin/invites", { method: "POST", body: { email: $("i-email").value, role: $("i-role").value } }); $("i-email").value = ""; toast("Invite saved"); }
    catch (e) { toast(e.message); }
    draw();
  };
  await draw();
}

async function adminApplicants(body) {
  const draw = async () => {
    const { applications } = await api("/api/admin/applications");
    body.innerHTML = `<h1>Volunteer applications</h1>${applications.length ? applications.map((a) => `
      <div class="card" style="margin-bottom:.7rem" data-uid="${esc(a.uid)}">
        <h2>${esc(a.name || a.email)}</h2>
        <div class="dim small">${esc(a.email)} · ${esc(a.place || "no place given")} · applied ${esc(ago(a.applied_at))}</div>
        ${a.extra_info ? `<p style="margin:.5rem 0">${esc(a.extra_info)}</p>` : '<p class="dim small">No extra info given.</p>'}
        <div class="row"><button class="btn good" data-approve>Approve</button><button class="btn danger" data-reject>Reject</button></div>
      </div>`).join("") : '<p class="dim">No applications waiting right now.</p>'}`;
    body.querySelectorAll("[data-uid]").forEach((card) => {
      const uid = card.dataset.uid;
      card.querySelector("[data-approve]").onclick = async () => {
        try { await api(`/api/admin/applications/${encodeURIComponent(uid)}/decide`, { method: "POST", body: { approve: true } }); toast("Approved"); }
        catch (e) { toast(e.message); }
        draw(); refreshAdminBadge();
      };
      card.querySelector("[data-reject]").onclick = async () => {
        if (!confirm("Reject this application? They'll stay at guest-level access.")) return;
        try { await api(`/api/admin/applications/${encodeURIComponent(uid)}/decide`, { method: "POST", body: { approve: false } }); toast("Rejected"); }
        catch (e) { toast(e.message); }
        draw(); refreshAdminBadge();
      };
    });
  };
  await draw();
}

async function adminContributors(body) {
  const { books } = can("manage_books") ? await api("/api/admin/books") : { books: [] };
  body.innerHTML = `<h1>Contributors</h1>
    <div class="toolbar"><label class="dim small">Book</label><select id="c-book"><option value="">All books</option>${books.map((b) => `<option value="${esc(b.id)}">${esc(b.title || b.id)}</option>`).join("")}</select></div>
    <div id="c-table"></div>`;
  let data = null, sort = { key: "edits", dir: -1 };
  const draw = () => {
    const rows = [...data.contributors].sort((a, b) => (typeof a[sort.key] === "string" ? String(a[sort.key]).localeCompare(String(b[sort.key])) : a[sort.key] - b[sort.key]) * sort.dir);
    const cols = [["name", "Name"], ["role", "Role"], ["edits", "Edits suggested"], ["confirms", "Looks right"], ["approvals", "Approved"], ["last_seen", "Last seen"]];
    $("c-table").innerHTML = `<table><thead><tr>${cols.map(([k, l]) => `<th data-sort="${k}" class="sortable">${l}${sort.key === k ? (sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead><tbody>${rows.map((c) =>
      `<tr class="${c.active ? "" : "off"}"><td>${esc(c.name || c.email)}<div class="dim small">${esc(c.email || "")}</div></td><td>${esc(c.role)}</td><td>${c.edits}</td><td>${c.confirms}</td><td>${c.approvals}</td><td class="dim small">${esc(ago(c.last_seen))}</td></tr>`).join("")}
      <tr><td>Guests <span class="dim small">(${data.guests.people} people)</span></td><td>guest</td><td>${data.guests.edits}</td><td>${data.guests.confirms}</td><td>0</td><td></td></tr></tbody></table>`;
    body.querySelectorAll("[data-sort]").forEach((th) => (th.onclick = () => { sort = { key: th.dataset.sort, dir: sort.key === th.dataset.sort ? -sort.dir : -1 }; draw(); }));
  };
  const load = async () => { data = await api("/api/admin/stats" + ($("c-book").value ? `?book=${encodeURIComponent($("c-book").value)}` : "")); draw(); };
  $("c-book").onchange = load;
  await load();
}

async function adminBooks(body) {
  if (!can("manage_books")) throw new Error("You don't have permission to manage books.");
  const draw = async () => {
    const { books } = await api("/api/admin/books");
    body.innerHTML = `<h1>Books</h1>${books.length ? `<table><thead><tr><th>Book</th><th>Progress</th><th>Snippets</th><th>Attention</th><th>Visibility</th><th></th></tr></thead><tbody>${books.map((b) => `
      <tr class="${b.hidden ? "off" : ""}"><td>${esc(b.title || b.id)}<div class="dim small">${esc(b.id)} · ${esc(b.kavi || "")}</div></td>
        <td style="min-width:8rem"><div class="small">${b.completion_pct}% finalized</div><div class="bar"><i style="width:${b.completion_pct}%"></i></div></td>
        <td>${b.finalized} / ${b.total}</td><td>${b.needs_attention}</td>
        <td>${b.hidden ? '<span class="pill warn">hidden</span>' : "visible"}</td>
        <td class="nowrap"><a class="btn" href="#/book/${esc(b.id)}/page/1">Open</a>
          <a class="btn" href="${esc(CFG.API_BASE)}/api/books/${esc(b.id)}/text?use=final" target="_blank" rel="noopener">Final text</a>
          <button class="btn" data-versions="${esc(b.id)}">Versions</button>
          <button class="btn ${b.hidden ? "good" : "warn"}" data-hide="${esc(b.id)}" data-to="${b.hidden ? 0 : 1}">${b.hidden ? "Show" : "Hide"}</button></td></tr>`).join("")}</tbody></table>
      <p class="dim small">A hidden book disappears from the library for everyone except admins. Reviews already made are kept.</p>` : '<p class="dim">No books yet.</p>'}`;
    body.querySelectorAll("[data-hide]").forEach((b) => (b.onclick = async () => {
      try { await api(`/api/admin/books/${encodeURIComponent(b.dataset.hide)}/hidden`, { method: "POST", body: { hidden: b.dataset.to === "1" } }); toast(b.dataset.to === "1" ? "Book hidden" : "Book visible"); }
      catch (e) { toast(e.message); }
      draw();
    }));
    body.querySelectorAll("[data-versions]").forEach((b) => (b.onclick = () => openVersionsModal(b.dataset.versions)));
  };
  await draw();
}

/* ---------------- book versions: export/import/apply/download (offline <-> online reconciliation) --- */
async function openVersionsModal(bookId) {
  const draw = async () => {
    const { versions } = await api(`/api/admin/books/${encodeURIComponent(bookId)}/versions`);
    showModal(`<h2>Versions — ${esc(bookId)}</h2>
      <p class="dim small">Export saves a snapshot of this book's current review data as a new version, ready to download and take offline. Import saves an uploaded file as a new version too - nothing changes here until you Apply one. Applying merges it in without ever overwriting a newer suggestion or un-finalizing an already-finalized snippet - to truly roll a finalized snippet back, re-open it first (Admin → that snippet), then apply the older version.</p>
      <div class="row" style="margin-bottom:0.8rem;">
        <button class="btn primary" id="ver-export">Export current as a new version</button>
        <label class="btn" style="cursor:pointer;">Import a version…<input type="file" id="ver-import" accept="application/json" hidden></label>
      </div>
      <div id="ver-table">${versions.length ? `<table><thead><tr><th>Source</th><th>Summary</th><th>By</th><th>When</th><th></th></tr></thead><tbody>${versions.map((v) => `
        <tr><td>${esc(v.source)}</td><td>${esc(v.summary)}</td><td class="dim small">${esc(v.created_by || "")}</td><td class="dim small">${esc(ago(v.created_at))}</td>
          <td class="nowrap"><button class="btn good" data-apply="${v.id}">Apply</button><button class="btn" data-download="${v.id}">Download</button></td></tr>`).join("")}</tbody></table>`
        : '<p class="dim small">No versions yet.</p>'}</div>`);

    $("ver-export").onclick = async () => {
      $("ver-export").disabled = true;
      try { await downloadFile(`/api/admin/books/${encodeURIComponent(bookId)}/export`, `${bookId}-version.json`); toast("Exported"); await draw(); }
      finally { const btn = $("ver-export"); if (btn) btn.disabled = false; }
    };
    $("ver-import").onchange = async () => {
      const input = $("ver-import"), file = input.files[0];
      if (!file) return;
      try {
        const parsed = JSON.parse(await file.text());
        await api(`/api/admin/books/${encodeURIComponent(bookId)}/import`, { method: "POST", body: parsed });
        toast("Imported as a new version"); await draw();
      } catch (e) { toast(e instanceof SyntaxError ? "That file isn't valid JSON" : e.message); }
      input.value = "";
    };
    document.querySelectorAll("#ver-table [data-apply]").forEach((btn) => (btn.onclick = async () => {
      if (!confirm("Apply this version? It merges into the live review data now - suggestions/finalizations from this version are added, never overwriting a newer or already-finalized one.")) return;
      try {
        const r = await api(`/api/admin/books/${encodeURIComponent(bookId)}/versions/${btn.dataset.apply}/apply`, { method: "POST" });
        toast(`Applied: ${r.suggestions_applied} suggestion(s), ${r.finalizations_applied} finalization(s), ${r.working_text_applied} text update(s), ${r.skipped} skipped`);
      } catch (e) { toast(e.message); }
    }));
    document.querySelectorAll("#ver-table [data-download]").forEach((btn) => (btn.onclick = () =>
      downloadFile(`/api/admin/books/${encodeURIComponent(bookId)}/versions/${btn.dataset.download}/download`, `${bookId}-version-${btn.dataset.download}.json`)));
  };
  await draw();
}

async function adminPermissions(body) {
  const m = await api("/api/admin/permissions");
  body.innerHTML = `<h1>Who can do what</h1>
    <table><thead><tr><th>Permission</th>${m.roles.map((r) => `<th style="text-align:center">${r}</th>`).join("")}</tr></thead><tbody>${m.permissions.map((p) => `
      <tr><td><b>${esc(p.name)}</b><div class="dim small">${esc(p.description)}</div></td>${m.roles.map((r) => `<td style="text-align:center">${p.roles.includes(r) ? "✓" : ""}</td>`).join("")}</tr>`).join("")}</tbody></table>
    <p class="dim small">This is fixed in the code (<code>reviewapi/permissions.py</code>), so it is the same everywhere. New sign-ins start as <b>reviewer</b>; roles are assigned under Users or through an invite.
    Nobody can change their own role, and only a superadmin can grant or change superadmin.</p>`;
}

/* ---------------- boot ---------------- */
if (CFG.AUTH_MODE === "dev") {
  loadMe().then(route);
} else {
  // Firebase restores a persisted session asynchronously - wait for this (fires once immediately with
  // the restored user, or null, and again on every future sign-in/out) rather than calling loadMe()
  // with firebaseUser still unset, which would look like "signed out" for a moment on every page load.
  firebaseAuth().onAuthStateChanged((user) => {
    firebaseUser = user;
    loadMe().then(route);
  });
}
