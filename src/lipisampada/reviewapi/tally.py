"""Word-level tally of reviewer suggestions for one snippet.

Each edit suggestion is diffed against the snippet's current text; identical
word changes from different reviewers are grouped, so an admin sees e.g.
"ತುಂಡ -> ತುಂಟ: 3 reviewers (1 editor)" rather than three unrelated full-text
edits. "Confirm original" votes are counted alongside.

Guests (not logged in) are recorded and shown separately but do not count
toward the attention threshold - anonymous suggestions are the easiest to spam."""

from collections import OrderedDict
from difflib import SequenceMatcher

ATTENTION_THRESHOLD = 2  # distinct signed-in reviewers agreeing on one change
GUEST_ROLES = {"guest"}
EDITOR_ROLES = {"editor", "admin", "superadmin"}


def words(text: str) -> list[str]:
    return (text or "").split()


def diff_changes(base_text: str, suggested_text: str) -> list[dict]:
    """Word-level changes turning base_text into suggested_text.
    start/end are word indices into base_text; start == end is an insertion."""
    base, sugg = words(base_text), words(suggested_text)
    changes = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, base, sugg, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changes.append(
            {
                "start": i1,
                "end": i2,
                "original": " ".join(base[i1:i2]),
                "replacement": " ".join(sugg[j1:j2]),
            }
        )
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


def apply_changes(base_text: str, changes: list[dict]) -> str:
    """Applies word changes (dicts with start/end/replacement) to base_text.
    Applied right-to-left so indices stay valid; a change overlapping an
    already-applied one is skipped rather than producing garbage."""
    ws = words(base_text)
    lowest_applied = len(ws) + 1
    for c in sorted(changes, key=lambda c: (c["start"], c["end"]), reverse=True):
        if c["end"] > lowest_applied:
            continue
        ws[c["start"]:c["end"]] = words(c["replacement"])
        lowest_applied = c["start"]
    return " ".join(ws)


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
