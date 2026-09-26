"""Word-level tally of reviewer suggestions for one snippet.

Each edit suggestion is diffed against the snippet's current text; identical
word changes from different reviewers are grouped, so an admin sees e.g.
"ತುಂಡ -> ತುಂಟ: 3 reviewers (1 editor)" rather than three unrelated full-text
edits. "Confirm original" votes are counted alongside.

Guests (not logged in) are recorded and shown separately but do not count
toward the attention threshold - anonymous suggestions are the easiest to spam."""

import re
from collections import OrderedDict
from difflib import SequenceMatcher

ATTENTION_THRESHOLD = 2  # distinct signed-in reviewers agreeing on one change
GUEST_ROLES = {"guest"}
EDITOR_ROLES = {"editor", "admin", "superadmin"}

_WORD_RE = re.compile(r"\S+")


def _spans(text: str) -> list[tuple[str, int, int]]:
    """Each non-whitespace run in text, with its exact character span - lets diff_changes/
    apply_changes splice verbatim substrings instead of rejoining words with a hardcoded single
    space, which used to silently collapse any newline a reviewer typed (see tally.py history)."""
    return [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]


def words(text: str) -> list[str]:
    return [w for w, _, _ in _spans(text)]


def diff_changes(base_text: str, suggested_text: str) -> list[dict]:
    """Word-level changes turning base_text into suggested_text.
    start/end are word indices into base_text; start == end is an insertion.
    original/replacement are verbatim substrings (preserving any internal whitespace/newlines the
    reviewer actually typed), not the words rejoined with a synthetic single space."""
    base_spans, sugg_spans = _spans(base_text), _spans(suggested_text)
    base_words = [w for w, _, _ in base_spans]
    sugg_words = [w for w, _, _ in sugg_spans]
    changes = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, base_words, sugg_words, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        original = base_text[base_spans[i1][1]:base_spans[i2 - 1][2]] if i1 < i2 else ""
        replacement = suggested_text[sugg_spans[j1][1]:sugg_spans[j2 - 1][2]] if j1 < j2 else ""
        changes.append({"start": i1, "end": i2, "original": original, "replacement": replacement})
    return changes


def tally(base_text: str, suggestions: list[dict]) -> dict:
    """suggestions: [{user_id, name, role, kind ('confirm'|'edit'), text}].
    Returns counts plus the grouped word changes, most-agreed first."""
    confirms = {"reviewers": 0, "guests": 0}
    edits = 0
    groups: "OrderedDict[tuple, dict]" = OrderedDict()

    for s in suggestions:
        is_guest = s["role"] in GUEST_ROLES
        if s["kind"] == "confirm":
            confirms["guests" if is_guest else "reviewers"] += 1
            continue
        changes = diff_changes(base_text, s["text"])
        if not changes:  # an "edit" identical to the current text is a confirmation
            confirms["guests" if is_guest else "reviewers"] += 1
            continue
        edits += 1
        for c in changes:
            key = (c["start"], c["end"], c["replacement"])
            g = groups.setdefault(key, {**c, "reviewers": [], "editors": 0, "guests": 0})
            if is_guest:
                g["guests"] += 1
            else:
                g["reviewers"].append({"user_id": s["user_id"], "name": s.get("name"), "role": s["role"]})
                if s["role"] in EDITOR_ROLES:
                    g["editors"] += 1

    word_changes = [{**g, "count": len(g["reviewers"])} for g in groups.values()]
    word_changes.sort(key=lambda g: (-g["count"], -g["guests"], g["start"]))

    return {
        "confirms": confirms,
        "edit_suggestions": edits,
        "word_changes": word_changes,
        "needs_attention": any(g["count"] >= ATTENTION_THRESHOLD for g in word_changes),
        "has_activity": bool(suggestions),
    }


def _pad_insert(text: str, pos: int, replacement: str) -> str:
    """Inserts replacement at character position pos, padding with a single space on whichever
    side(s) don't already border whitespace - matches the old word-list-splice behavior's spacing
    exactly (no padding at the very start/end of text, single space between real words) without
    needing to rejoin the whole string."""
    before, after = text[:pos], text[pos:]
    pad_before = "" if (not before or before[-1].isspace()) else " "
    pad_after = "" if (not after or after[0].isspace()) else " "
    return before + pad_before + replacement + pad_after + after


def apply_changes(base_text: str, changes: list[dict]) -> str:
    """Applies word changes (dicts with start/end/replacement) to base_text - splicing verbatim at
    each change's character span, so everything outside the edited span(s) (including any newlines)
    survives untouched, rather than rejoining every word in the whole text with a hardcoded single
    space (which used to silently collapse a reviewer's line breaks - see tally.py history).
    Applied right-to-left, using word spans computed once from the original base_text, so earlier
    (lower-index) offsets stay valid as later ones are spliced in; a change overlapping an
    already-applied one is skipped rather than producing garbage - same as before."""
    spans = _spans(base_text)
    n_words = len(spans)
    out = base_text
    lowest_applied = n_words + 1
    for c in sorted(changes, key=lambda c: (c["start"], c["end"]), reverse=True):
        if c["end"] > lowest_applied:
            continue
        if c["start"] == c["end"]:
            pos = spans[c["start"]][1] if c["start"] < n_words else len(base_text)
            out = _pad_insert(out, pos, c["replacement"])
        else:
            end_idx = min(c["end"], n_words) - 1
            if end_idx < c["start"]:
                continue
            char_start, char_end = spans[c["start"]][1], spans[end_idx][2]
            out = out[:char_start] + c["replacement"] + out[char_end:]
        lowest_applied = c["start"]
    return out


def auto_approvable_changes(t: dict) -> list[dict]:
    """The word changes a bulk 'approve page' applies: agreed on by at least
    ATTENTION_THRESHOLD signed-in reviewers AND by more reviewers than
    confirmed the text as it stands - a clear majority, not merely a popular
    minority. Everything else is left as it is."""
    return [
        g
        for g in t["word_changes"]
        if g["count"] >= ATTENTION_THRESHOLD and g["count"] > t["confirms"]["reviewers"]
    ]
