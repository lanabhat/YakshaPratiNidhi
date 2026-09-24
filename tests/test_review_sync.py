import sqlite3
from pathlib import Path

import pytest

from lipisampada import publisher
from test_api import EDITOR, KEY, ROOT, REVIEWER1, SID, client, h, seeded, snippets  # noqa: F401


def sync(c, book_id="B1", users=None, snippets_payload=None):
    return c.post("/api/ingest/reviews", headers={"X-Ingest-Key": KEY},
                  json={"book_id": book_id, "users": users or [], "snippets": snippets_payload or []})


def sug(email, kind="confirm", text=None, created_at="2026-01-01T00:00:00+00:00"):
    return {"email": email, "kind": kind, "text": text, "created_at": created_at}


# ------------------------------------------------------------------ auth / basic wiring
def test_ingest_reviews_requires_the_key(seeded):
    r = seeded.post("/api/ingest/reviews", json={"book_id": "B1", "users": [], "snippets": []})
    assert r.status_code == 401
    r = seeded.post("/api/ingest/reviews", headers={"X-Ingest-Key": "nope"}, json={"book_id": "B1", "users": [], "snippets": []})
    assert r.status_code == 401


def test_unknown_book_is_rejected(seeded):
    assert sync(seeded, book_id="NOPE").status_code == 404


def test_unknown_snippet_ids_are_skipped_not_erroring(seeded):
    r = sync(seeded, users=[{"email": "r1@x.org", "role": "reviewer"}],
             snippets_payload=[{"snippet_id": "B1:99:P:0", "working_text": "x", "suggestions": [sug("r1@x.org")]}])
    assert r.status_code == 200 and r.json["skipped"] == 1 and r.json["suggestions_applied"] == 0


# ------------------------------------------------------------------ suggestions
def test_a_suggestion_for_an_existing_user_is_applied_by_email(seeded):
    seeded.get("/api/me", headers=REVIEWER1)  # r1@x.org already has a real account
    r = sync(seeded, users=[{"email": "r1@x.org", "role": "reviewer"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "ಹರಿ ತುಂಡತನ p1s0", "suggestions": [sug("r1@x.org", "confirm")]}])
    assert r.status_code == 200 and r.json["suggestions_applied"] == 1
    tally = seeded.get(f"/api/snippet?id={SID}").json["tally"]
    assert tally["confirms"]["reviewers"] == 1


def test_a_suggestion_for_a_brand_new_person_creates_a_pending_placeholder(seeded):
    r = sync(seeded, users=[{"email": "newperson@x.org", "name": "New Person", "role": "reviewer"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "x", "suggestions": [sug("newperson@x.org", "edit", "ಬದಲಾಯಿತು")]}])
    assert r.status_code == 200 and r.json["suggestions_applied"] == 1
    users = {u["email"]: u for u in seeded.get("/api/admin/users", headers=ROOT).json["users"]}
    assert users["newperson@x.org"]["role"] == "reviewer"


def test_when_that_pending_person_later_signs_in_for_real_they_claim_their_history(seeded):
    sync(seeded, users=[{"email": "newperson@x.org", "name": "New Person", "role": "editor"}],
         snippets_payload=[{"snippet_id": SID, "working_text": "x", "suggestions": [sug("newperson@x.org", "edit", "ಬದಲಾಯಿತು")]}])
    before = {u["email"]: u["uid"] for u in seeded.get("/api/admin/users", headers=ROOT).json["users"]}
    assert before["newperson@x.org"].startswith("pending:")

    me = seeded.get("/api/me", headers=h("newperson@x.org")).json  # first real sign-in, same email
    assert me["user"]["role"] == "editor"  # kept the role the sync gave them, not reset to default reviewer
    assert not me["user"]["uid"].startswith("pending:")

    tally = seeded.get(f"/api/snippet?id={SID}").json["tally"]
    assert tally["word_changes"] and tally["word_changes"][0]["count"] == 1  # the suggestion is still attributed to them
    after = {u["email"]: u["uid"] for u in seeded.get("/api/admin/users", headers=ROOT).json["users"]}
    assert after["newperson@x.org"] == me["user"]["uid"]  # same row claimed, not a second account


def test_a_newer_remote_suggestion_is_not_overwritten_by_an_older_synced_one(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "remote newer"})
    r = sync(seeded, users=[{"email": "r1@x.org", "role": "reviewer"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "x",
                                  "suggestions": [sug("r1@x.org", "edit", "local older", "2020-01-01T00:00:00+00:00")]}])
    assert r.status_code == 200
    mine = seeded.get(f"/api/snippet?id={SID}", headers=REVIEWER1).json["my_suggestion"]
    assert mine["text"] == "remote newer"


def test_an_older_remote_suggestion_is_overwritten_by_a_newer_synced_one(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "remote older"})
    r = sync(seeded, users=[{"email": "r1@x.org", "role": "reviewer"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "x",
                                  "suggestions": [sug("r1@x.org", "edit", "local newer", "2099-01-01T00:00:00+00:00")]}])
    assert r.status_code == 200
    mine = seeded.get(f"/api/snippet?id={SID}", headers=REVIEWER1).json["my_suggestion"]
    assert mine["text"] == "local newer"


def test_guest_suggestions_are_not_synced_as_attributable_users(seeded):
    # sync_reviews only cares about what the publisher actually sends it; guests simply never appear
    # in a well-formed payload (see the publisher-level test), but the endpoint should not choke if
    # asked to attribute something to a role of "guest" either.
    r = sync(seeded, users=[{"email": "ghost@x.org", "role": "guest"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "x", "suggestions": [sug("ghost@x.org")]}])
    assert r.status_code == 200 and r.json["suggestions_applied"] == 1
    # they still get a real (non-guest) row created for attribution purposes, since a synced person is a real account
    users = {u["email"]: u for u in seeded.get("/api/admin/users", headers=ROOT).json["users"]}
    assert users["ghost@x.org"]["role"] == "reviewer"  # invalid "guest" role falls back to reviewer, never silently guest


# ------------------------------------------------------------------ finalization / working text
def test_a_local_finalization_is_applied_when_remote_is_not_yet_finalized(seeded):
    r = sync(seeded, users=[{"email": "ed@x.org", "role": "editor"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "final text", "final_text": "final text",
                                  "finalized_by_email": "ed@x.org", "finalized_at": "2026-01-01T00:00:00+00:00", "suggestions": []}])
    assert r.status_code == 200 and r.json["finalizations_applied"] == 1
    s = seeded.get(f"/api/snippet?id={SID}").json
    assert s["finalized"] and s["current_text"] == "final text"


def test_a_local_finalization_never_overrides_an_already_finalized_remote_snippet(seeded):
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "remote final"})
    r = sync(seeded, users=[{"email": "ed@x.org", "role": "editor"}],
              snippets_payload=[{"snippet_id": SID, "working_text": "local final", "final_text": "local final",
                                  "finalized_by_email": "ed@x.org", "finalized_at": "2020-01-01T00:00:00+00:00", "suggestions": []}])
    assert r.status_code == 200 and r.json["finalizations_applied"] == 0
    s = seeded.get(f"/api/snippet?id={SID}").json
    assert s["current_text"] == "remote final"


def test_local_working_text_is_applied_when_nothing_is_finalized_yet(seeded):
    r = sync(seeded, snippets_payload=[{"snippet_id": SID, "working_text": "accepted word change", "suggestions": []}])
    assert r.status_code == 200 and r.json["working_text_applied"] == 1
    s = seeded.get(f"/api/snippet?id={SID}").json
    assert s["current_text"] == "accepted word change" and not s["finalized"]


# ------------------------------------------------------------------ publisher.sync_reviews_to_remote
def _local_review_db(tmp_path, book_id="B1"):
    from lipisampada.reviewapi.db import Db

    db = Db(tmp_path / "local_review.sqlite3")
    u1 = db.get_or_create_user("dev:r1@x.org", "r1@x.org", "R1", "reviewer")
    u2 = db.get_or_create_user("guest:abc", None, "Guest", "guest")
    db.conn.execute(
        "INSERT INTO snippets (id, book_id, page_index, page_number, side, seq, ai_text, working_text, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{book_id}:1:P:0", book_id, 1, 1, "P", 0, "ai text", "ai text", "2026-01-01T00:00:00+00:00"),
    )
    db.conn.execute(
        "INSERT INTO snippets (id, book_id, page_index, page_number, side, seq, ai_text, working_text, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{book_id}:1:P:1", book_id, 1, 1, "P", 1, "untouched", "untouched", "2026-01-01T00:00:00+00:00"),
    )
    # a snippet finalized locally (bypassing the editor-only finalize() permission check - this is
    # just seeding raw rows to exercise sync_reviews_to_remote's own finalized_by lookup)
    db.conn.execute(
        "INSERT INTO snippets (id, book_id, page_index, page_number, side, seq, ai_text, working_text, "
        "final_text, finalized_by, finalized_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{book_id}:1:P:2", book_id, 1, 1, "P", 2, "ai text", "final text", "final text", u1["uid"],
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    db.suggest(u1, f"{book_id}:1:P:0", "confirm", None)
    db.suggest(u2, f"{book_id}:1:P:0", "confirm", None)  # a guest vote - must not be sent upstream
    db.conn.commit()
    return db.path


def test_publisher_sync_is_a_noop_when_there_is_no_local_review_db(tmp_path):
    assert publisher.sync_reviews_to_remote(tmp_path / "does_not_exist.sqlite3", "B1", post=lambda *a: (_ for _ in ()).throw(AssertionError("should not be called"))) is None


def test_publisher_sync_is_a_noop_when_nothing_local_touches_this_book(tmp_path):
    db_path = _local_review_db(tmp_path, book_id="B1")
    called = []
    result = publisher.sync_reviews_to_remote(Path(db_path), "OTHER_BOOK", post=lambda *a: called.append(a) or {})
    assert result is None and called == []


def test_publisher_sync_sends_only_touched_snippets_and_excludes_guests(tmp_path):
    db_path = _local_review_db(tmp_path, book_id="B1")
    captured = {}

    def fake_post(path, payload):
        captured["path"], captured["payload"] = path, payload
        return {"ok": True}

    result = publisher.sync_reviews_to_remote(Path(db_path), "B1", post=fake_post)
    assert result == {"ok": True}
    assert captured["path"] == "/api/ingest/reviews"
    payload = captured["payload"]
    assert payload["book_id"] == "B1"
    by_id = {s["snippet_id"]: s for s in payload["snippets"]}
    assert set(by_id) == {"B1:1:P:0", "B1:1:P:2"}  # the untouched one (B1:1:P:1) is skipped
    assert [u["email"] for u in payload["users"]] == ["r1@x.org"]  # the guest never appears
    assert by_id["B1:1:P:0"]["suggestions"] == [{"email": "r1@x.org", "kind": "confirm", "text": None,
                                                  "created_at": by_id["B1:1:P:0"]["suggestions"][0]["created_at"]}]
    # regression: finalized_by used to crash sync_reviews_to_remote (sqlite3.Row has no .get(), which
    # real() calls) - this is the case that reproduces it
    assert by_id["B1:1:P:2"]["final_text"] == "final text"
    assert by_id["B1:1:P:2"]["finalized_by_email"] == "r1@x.org"


def test_publisher_sync_end_to_end_against_a_real_ingest_reviews_endpoint(tmp_path, seeded):
    db_path = _local_review_db(tmp_path, book_id="B1")

    def post(path, payload):
        r = seeded.post(path, headers={"X-Ingest-Key": KEY}, json=payload)
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.json

    result = publisher.sync_reviews_to_remote(Path(db_path), "B1", post=post)
    assert result["suggestions_applied"] == 1
    tally = seeded.get(f"/api/snippet?id={SID}").json["tally"]
    assert tally["confirms"]["reviewers"] == 1
