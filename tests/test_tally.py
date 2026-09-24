from lipisampada.reviewapi import tally as t


def sug(uid, role, text=None, kind="edit"):
    return {"user_id": uid, "name": f"u{uid}", "role": role, "kind": kind, "text": text}


BASE = "ಹರಿ ತುಂಡತನ ಮಾಡಿದ"


def test_diff_replace_and_insert_and_delete():
    assert t.diff_changes("a b c", "a X c") == [{"start": 1, "end": 2, "original": "b", "replacement": "X"}]
    assert t.diff_changes("a b", "a b c") == [{"start": 2, "end": 2, "original": "", "replacement": "c"}]
    assert t.diff_changes("a b c", "a c") == [{"start": 1, "end": 2, "original": "b", "replacement": ""}]
    assert t.diff_changes("a b", "a b") == []


def test_same_word_change_from_several_reviewers_is_grouped():
    s = [sug(1, "reviewer", "ಹರಿ ತುಂಟತನ ಮಾಡಿದ"), sug(2, "editor", "ಹರಿ ತುಂಟತನ ಮಾಡಿದ"), sug(3, "reviewer", "ಹರಿ ತುಂಡತನ ಮಾಡಿದ", "confirm")]
    r = t.tally(BASE, s)
    assert len(r["word_changes"]) == 1
    g = r["word_changes"][0]
    assert (g["original"], g["replacement"], g["count"], g["editors"]) == ("ತುಂಡತನ", "ತುಂಟತನ", 2, 1)
    assert r["confirms"] == {"reviewers": 1, "guests": 0}
    assert r["needs_attention"] is True


def test_different_words_in_same_snippet_are_separate_groups():
    r = t.tally("a b c d", [sug(1, "reviewer", "a X c d"), sug(2, "reviewer", "a b c Y")])
    assert {g["replacement"] for g in r["word_changes"]} == {"X", "Y"}
    assert r["needs_attention"] is False  # each has only one reviewer


def test_guests_are_recorded_but_do_not_trigger_attention():
    r = t.tally("a b", [sug(1, "guest", "a X"), sug(2, "guest", "a X"), sug(3, "guest", "a X")])
    g = r["word_changes"][0]
    assert (g["count"], g["guests"]) == (0, 3)
    assert r["needs_attention"] is False


def test_edit_identical_to_current_text_counts_as_confirmation():
    r = t.tally("a b", [sug(1, "reviewer", "a b")])
    assert r["confirms"]["reviewers"] == 1 and r["word_changes"] == []


def test_apply_changes_right_to_left_and_skips_overlaps():
    assert t.apply_changes("a b c d", [{"start": 1, "end": 2, "replacement": "X"}, {"start": 3, "end": 4, "replacement": "Y Z"}]) == "a X c Y Z"
    assert t.apply_changes("a b c", [{"start": 0, "end": 2, "replacement": "P"}, {"start": 1, "end": 3, "replacement": "Q"}]) == "a Q"
    assert t.apply_changes("a c", [{"start": 1, "end": 1, "replacement": "b"}]) == "a b c"


def test_auto_approve_needs_agreement_and_majority_over_confirms():
    two = [sug(1, "reviewer", "a X"), sug(2, "reviewer", "a X")]
    assert len(t.auto_approvable_changes(t.tally("a b", two))) == 1
    outvoted = two + [sug(3, "reviewer", None, "confirm"), sug(4, "reviewer", None, "confirm"), sug(5, "reviewer", None, "confirm")]
    assert t.auto_approvable_changes(t.tally("a b", outvoted)) == []
    lone = [sug(1, "reviewer", "a X")]
    assert t.auto_approvable_changes(t.tally("a b", lone)) == []
