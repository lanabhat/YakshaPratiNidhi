import pytest

from lipisampada.reviewapi.app import create_app

KEY = "test-ingest-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "dev")
    monkeypatch.setenv("INGEST_API_KEY", KEY)
    monkeypatch.setenv("SUPERADMIN_EMAILS", "root@x.org")
    monkeypatch.setenv("REVIEW_API_DATA", str(tmp_path))
    app = create_app(tmp_path / "t.sqlite3", tmp_path / "files")
    return app.test_client()


def h(email):
    return {"Authorization": f"dev:{email}".replace("dev:", "Bearer dev:", 1)}


REVIEWER1, REVIEWER2, REVIEWER3 = h("r1@x.org"), h("r2@x.org"), h("r3@x.org")
EDITOR, ROOT = h("ed@x.org"), h("root@x.org")


def snippets():
    out = []
    for page in (1, 2):
        for seq in range(2):
            out.append({"page_number": page, "side": "P", "seq": seq, "ai_text": f"ಹರಿ ತುಂಡತನ p{page}s{seq}",
                        "page_image_url": f"http://x/p{page}.webp", "snippet_image_url": f"http://x/p{page}s{seq}.webp",
                        "bbox": [0, 0, 10, 10]})
    return out


@pytest.fixture()
def seeded(client):
    r = client.post("/api/ingest/book", headers={"X-Ingest-Key": KEY},
                    json={"book": {"id": "B1", "title": "Book One", "page_count": 2}, "snippets": snippets()})
    assert r.status_code == 200 and r.json["created"] == 4
    # roles: ed@ becomes editor via the superadmin
    client.get("/api/me", headers=EDITOR)
    client.get("/api/me", headers=ROOT)
    assert client.post("/api/admin/users/dev:ed@x.org/role", headers=ROOT, json={"role": "editor"}).status_code == 200
    return client


SID = "B1:1:P:0"


def test_ingest_requires_key_and_is_idempotent(client):
    payload = {"book": {"id": "B1"}, "snippets": snippets()}
    assert client.post("/api/ingest/book", json=payload).status_code == 401
    assert client.post("/api/ingest/book", headers={"X-Ingest-Key": "nope"}, json=payload).status_code == 401
    assert client.post("/api/ingest/book", headers={"X-Ingest-Key": KEY}, json=payload).json["created"] == 4
    again = client.post("/api/ingest/book", headers={"X-Ingest-Key": KEY}, json=payload).json
    assert again["created"] == 0 and again["updated"] == 4


def test_reingest_keeps_human_work(seeded):
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "final text"})
    changed = snippets()
    changed[0]["ai_text"] = "different ai"
    seeded.post("/api/ingest/book", headers={"X-Ingest-Key": KEY}, json={"book": {"id": "B1"}, "snippets": changed})
    assert seeded.get(f"/api/snippet?id={SID}").json["final_text"] == "final text"


def test_library_stats(seeded):
    b = seeded.get("/api/library").json["books"][0]
    assert (b["total"], b["finalized"], b["pending"], b["untouched"], b["completion_pct"]) == (4, 0, 4, 4, 0.0)
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": "B1:2:P:1"})
    b = seeded.get("/api/library").json["books"][0]
    assert (b["finalized"], b["awaiting_approval"], b["untouched"], b["completion_pct"]) == (1, 1, 2, 25.0)


def test_three_reviewers_same_word_flags_attention(seeded):
    for who in (REVIEWER1, REVIEWER2):
        seeded.post("/api/suggest", headers=who, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    seeded.post("/api/suggest", headers=REVIEWER3, json={"snippet_id": SID, "kind": "confirm"})
    s = seeded.get(f"/api/snippet?id={SID}").json
    g = s["tally"]["word_changes"][0]
    assert (g["original"], g["replacement"], g["count"]) == ("ತುಂಡತನ", "ತುಂಟತನ", 2)
    assert s["tally"]["confirms"]["reviewers"] == 1 and s["tally"]["needs_attention"]
    assert seeded.get("/api/library").json["books"][0]["needs_attention"] == 1


def test_reviewer_count_and_full_suggestion_text_are_visible_to_everyone(seeded):
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    seeded.post("/api/suggest", headers=REVIEWER2, json={"snippet_id": SID, "kind": "confirm"})
    # visible even to a completely anonymous guest (no auth headers at all)
    s = seeded.get(f"/api/snippet?id={SID}").json
    assert s["reviewer_count"] == 2  # 1 edit + 1 confirm - everyone who weighed in, not just editors
    assert len(s["suggestions"]) == 1
    sug = s["suggestions"][0]
    assert sug["text"] == "ಹರಿ ತುಂಟತನ p1s0" and sug["role"] == "reviewer" and sug["kind"] == "edit"
    assert "name" in sug and "created_at" in sug
    # a confirm-only vote doesn't show up in the full-text suggestion list (nothing to read - see
    # tally.py's "confirm" kind), only in reviewer_count
    assert all(x["kind"] == "edit" for x in s["suggestions"])


def test_full_suggestion_text_preserves_line_breaks(seeded):
    multiline = "ಹರಿ ತುಂಟತನ\np1s0 second line"
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": multiline})
    s = seeded.get(f"/api/snippet?id={SID}").json
    assert s["suggestions"][0]["text"] == multiline


def test_resuggesting_replaces_not_duplicates(seeded):
    for text in ("ಹರಿ A p1s0", "ಹರಿ B p1s0"):
        seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": text})
    s = seeded.get(f"/api/snippet?id={SID}", headers=REVIEWER1).json
    assert s["suggestion_count"] == 1 and s["my_suggestion"]["text"] == "ಹರಿ B p1s0"


def test_guest_can_suggest_but_not_approve_and_not_count(seeded):
    guest = {"X-Guest-Id": "abc-123"}
    assert seeded.post("/api/suggest", headers=guest, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ Z p1s0"}).status_code == 200
    assert seeded.post("/api/finalize", headers=guest, json={"snippet_id": SID}).status_code == 403
    assert seeded.post("/api/suggest", json={"snippet_id": SID, "kind": "confirm"}).status_code == 401  # anonymous
    t = seeded.get(f"/api/snippet?id={SID}").json["tally"]
    assert t["word_changes"][0]["guests"] == 1 and not t["needs_attention"]


def test_only_editors_and_admins_can_approve(seeded):
    assert seeded.post("/api/finalize", headers=REVIEWER1, json={"snippet_id": SID}).status_code == 403
    assert seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "ok"}).status_code == 200
    assert seeded.post("/api/unfinalize", headers=EDITOR, json={"snippet_id": SID}).status_code == 403  # admin only
    assert seeded.post("/api/unfinalize", headers=ROOT, json={"snippet_id": SID}).status_code == 200


def test_suggestions_allowed_after_finalization_and_challenge_is_reported(seeded):
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "ಹರಿ ತುಂಡತನ p1s0"})
    for who in (REVIEWER1, REVIEWER2):
        assert seeded.post("/api/suggest", headers=who, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"}).status_code == 200
    b = seeded.get("/api/library").json["books"][0]
    assert b["finalized"] == 1 and b["finalized_challenged"] == 1 and b["needs_attention"] == 0


def test_accept_word_then_finalize(seeded):
    for who in (REVIEWER1, REVIEWER2):
        seeded.post("/api/suggest", headers=who, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    s = seeded.post("/api/accept-word", headers=EDITOR, json={"snippet_id": SID, "start": 1, "end": 2, "replacement": "ತುಂಟತನ"}).json["snippet"]
    assert s["working_text"] == "ಹರಿ ತುಂಟತನ p1s0" and not s["finalized"]
    assert s["tally"]["word_changes"] == [] and s["tally"]["confirms"]["reviewers"] == 2  # now matches -> confirmations
    assert seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID}).json["snippet"]["final_text"] == "ಹರಿ ತುಂಟತನ p1s0"


def test_approve_page_preview_then_apply(seeded):
    for who in (REVIEWER1, REVIEWER2):
        seeded.post("/api/suggest", headers=who, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    url = "/api/books/B1/pages/1/approve"
    plan = seeded.post(url, headers=EDITOR, json={"preview": True}).json
    assert plan["count"] == 2 and plan["snippets"][0]["after"] == "ಹರಿ ತುಂಟತನ p1s0" and plan["snippets"][1]["changes_applied"] == 0
    assert seeded.get("/api/library").json["books"][0]["finalized"] == 0  # preview wrote nothing
    assert seeded.post(url, headers=REVIEWER1, json={"preview": False}).status_code == 403
    seeded.post(url, headers=EDITOR, json={"preview": False})
    b = seeded.get("/api/library").json["books"][0]
    assert b["finalized"] == 2
    assert seeded.get(f"/api/snippet?id={SID}").json["final_text"] == "ಹರಿ ತುಂಟತನ p1s0"


def test_pages_navigation_and_text_export(seeded):
    p = seeded.get("/api/books/B1/pages?page=1&per_page=1").json
    assert p["total_pages"] == 2 and len(p["pages"]) == 1
    d = seeded.get("/api/books/B1/pages/1").json
    assert (d["prev"], d["next"], d["total_pages"], len(d["snippets"])) == (None, 2, 2, 2)
    assert seeded.get("/api/books/B1/pages/2").json["next"] is None
    assert seeded.get("/api/books/B1/pages/9").status_code == 404
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "FINAL"})
    assert "FINAL" in seeded.get("/api/books/B1/text?use=final").text
    assert "p2s0" not in seeded.get("/api/books/B1/text?use=final").text
    assert "p2s0" in seeded.get("/api/books/B1/text").text


def test_role_management_rules(seeded):
    assert seeded.post("/api/admin/users/dev:r1@x.org/role", headers=REVIEWER1, json={"role": "admin"}).status_code == 403
    assert seeded.post("/api/admin/users/dev:root@x.org/role", headers=EDITOR, json={"role": "reviewer"}).status_code == 403
    seeded.get("/api/me", headers=REVIEWER1)
    seeded.post("/api/admin/users/dev:r1@x.org/role", headers=ROOT, json={"role": "admin"})
    # an admin cannot mint a superadmin
    seeded.get("/api/me", headers=REVIEWER2)
    assert seeded.post("/api/admin/users/dev:r2@x.org/role", headers=REVIEWER1, json={"role": "superadmin"}).status_code == 403
    assert seeded.post("/api/admin/users/dev:r2@x.org/role", headers=REVIEWER1, json={"role": "guest"}).status_code == 400


def test_read_view_returns_consecutive_pages_with_status(seeded):
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "FINAL ONE"})
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": "B1:2:P:0", "kind": "confirm"})
    r = seeded.get("/api/books/B1/read?start=1&count=5").json
    assert r["total_pages"] == 2 and [p["page_index"] for p in r["pages"]] == [1, 2]
    first = r["pages"][0]["snippets"][0]
    assert first["text"] == "FINAL ONE" and first["finalized"] is True
    assert r["pages"][1]["snippets"][0]["suggestion_count"] == 1
    assert seeded.get("/api/books/B1/read?start=2&count=1").json["pages"][0]["page_index"] == 2


def test_api_root_explains_itself_instead_of_404(client):
    r = client.get("/")
    assert r.status_code == 200 and "PratiNidhi" in r.json["service"] and "/api/library" in r.json["try_these"]


def test_dashboard_top_reviewers_trends_and_participation(seeded):
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    seeded.post("/api/suggest", headers=REVIEWER2, json={"snippet_id": "B1:1:P:1", "kind": "confirm"})
    seeded.post("/api/suggest", headers={"X-Guest-Id": "g1"}, json={"snippet_id": "B1:2:P:0", "kind": "confirm"})
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "ಹರಿ ತುಂಟತನ p1s0"})  # matches REVIEWER1's suggestion exactly

    d = seeded.get("/api/dashboard").json  # no auth at all - a guest must be able to see this

    assert sum(r["suggestions"] for r in d["top_suggesters"]) == 2  # REVIEWER1 + REVIEWER2 only - guest excluded
    assert all(r["role"] != "guest" for r in d["top_suggesters"])

    approved = d["top_approved"]
    assert len(approved) == 1 and approved[0]["approved"] == 1  # only REVIEWER1's wording was adopted

    # 3 touched snippets, 3 words each: SID (finalized), B1:1:P:1 (reviewer confirm), B1:2:P:0 (guest
    # confirm - counts toward activity/trends even though the guest is excluded from the leaderboards)
    assert d["trends"] == {"books_reviewed": 1, "pages_reviewed": 2, "words_reviewed": 9}

    part = {p["book_id"]: p["participants"] for p in d["participation"]}
    assert part["B1"] == 2  # REVIEWER1 + REVIEWER2 - the guest confirm doesn't count

    assert any(b["id"] == "B1" for b in d["books"])
    assert d["pending_admin_review"] == []  # no 2-reviewer consensus on any unfinalized snippet yet


def test_dashboard_excludes_superadmins_from_leaderboards_and_participation(seeded):
    # ROOT is the seeded fixture's superadmin (SUPERADMIN_EMAILS=root@x.org)
    seeded.post("/api/suggest", headers=ROOT, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ROOT p1s0"})
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "ಹರಿ ROOT p1s0"})  # adopts ROOT's own wording
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": "B1:1:P:1", "kind": "confirm"})

    d = seeded.get("/api/dashboard").json
    assert all(r["role"] != "superadmin" for r in d["top_suggesters"])
    assert all(r["role"] != "superadmin" for r in d["top_approved"])  # ROOT's adopted suggestion still doesn't count
    part = {p["book_id"]: p["participants"] for p in d["participation"]}
    assert part["B1"] == 1  # only REVIEWER1 - ROOT's suggestion on the same book doesn't count toward participation
    # trends still count the activity regardless of who did it - this isn't a leaderboard
    assert d["trends"]["books_reviewed"] == 1


def test_dashboard_pending_admin_review_needs_real_consensus(seeded):
    for who in (REVIEWER1, REVIEWER2):
        seeded.post("/api/suggest", headers=who, json={"snippet_id": "B1:2:P:0", "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p2s0"})
    d = seeded.get("/api/dashboard").json
    assert [b["id"] for b in d["pending_admin_review"]] == ["B1"]


def test_books_backup_requires_manage_books_and_returns_a_real_zip(seeded):
    import zipfile
    from io import BytesIO

    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID, "text": "BACKED UP TEXT"})
    assert seeded.get("/api/admin/books/backup", headers=REVIEWER1).status_code == 403
    assert seeded.get("/api/admin/books/backup", headers=EDITOR).status_code == 403  # editor is below admin

    r = seeded.get("/api/admin/books/backup", headers=ROOT)
    assert r.status_code == 200 and r.mimetype == "application/zip"
    zf = zipfile.ZipFile(BytesIO(r.data))
    names = zf.namelist()
    assert len(names) == 1 and names[0].startswith("B1_")
    content = zf.read(names[0]).decode("utf-8")
    assert "BACKED UP TEXT" in content  # the one finalized snippet
    # Regression: the backup used to call book_text(id, "final"), which *omits* any unfinalized
    # snippet entirely - for a book barely reviewed yet, that produced a near-empty file. It must use
    # "current" (final text where approved, else the OCR'd/working text) so nothing is silently lost.
    assert "ಹರಿ ತುಂಡತನ p1s1" in content  # B1:1:P:1 was never touched - still its original OCR text
    assert "ಹರಿ ತುಂಡತನ p2s0" in content and "ಹರಿ ತುಂಡತನ p2s1" in content


def test_export_version_snapshots_current_state_and_is_downloadable(seeded):
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0"})
    assert seeded.get("/api/admin/books/B1/export", headers=REVIEWER1).status_code == 403

    r = seeded.get("/api/admin/books/B1/export", headers=ROOT)
    assert r.status_code == 200 and r.mimetype == "application/json"
    assert "Book_One" in r.headers["Content-Disposition"] or "B1" in r.headers["Content-Disposition"]
    payload = r.json
    assert payload["book_id"] == "B1" and payload["book_title"] == "Book One"
    snippet = next(s for s in payload["snippets"] if s["snippet_id"] == SID)
    assert snippet["suggestions"] == [{"email": "r1@x.org", "kind": "edit", "text": "ಹರಿ ತುಂಟತನ p1s0",
                                        "created_at": snippet["suggestions"][0]["created_at"]}]

    versions = seeded.get("/api/admin/books/B1/versions", headers=ROOT).json["versions"]
    assert len(versions) == 1 and versions[0]["source"] == "export"
    assert "1 touched snippet" in versions[0]["summary"]


def test_import_version_only_stores_it_does_not_touch_live_data(seeded):
    payload = {"book_id": "B1", "book_title": "Book One", "book_kavi": None, "users": [{"email": "r1@x.org", "name": "R1", "role": "reviewer"}],
               "snippets": [{"snippet_id": SID, "working_text": "IMPORTED TEXT", "final_text": None,
                              "finalized_by_email": None, "finalized_at": None, "suggestions": []}]}
    assert seeded.post("/api/admin/books/B1/import", headers=REVIEWER1, json=payload).status_code == 403
    r = seeded.post("/api/admin/books/B1/import", headers=ROOT, json=payload)
    assert r.status_code == 200 and r.json["source"] == "import"

    # not applied yet - live data is untouched
    assert seeded.get(f"/api/snippet?id={SID}").json["current_text"] != "IMPORTED TEXT"
    versions = seeded.get("/api/admin/books/B1/versions", headers=ROOT).json["versions"]
    assert len(versions) == 1 and versions[0]["source"] == "import"


def test_import_version_rejects_a_malformed_payload(seeded):
    r = seeded.post("/api/admin/books/B1/import", headers=ROOT, json={"not": "a version"})
    assert r.status_code == 400


def test_apply_version_merges_via_sync_reviews_and_respects_its_safety_rules(seeded):
    payload = {"book_id": "B1", "users": [{"email": "r1@x.org", "name": "R1", "role": "reviewer"}],
               "snippets": [{"snippet_id": SID, "working_text": "IMPORTED TEXT", "final_text": "IMPORTED TEXT",
                              "finalized_by_email": "r1@x.org", "finalized_at": "2020-01-01T00:00:00+00:00", "suggestions": []}]}
    imported = seeded.post("/api/admin/books/B1/import", headers=ROOT, json=payload).json
    assert seeded.post(f"/api/admin/books/B1/versions/{imported['id']}/apply", headers=REVIEWER1).status_code == 403

    r = seeded.post(f"/api/admin/books/B1/versions/{imported['id']}/apply", headers=ROOT)
    assert r.status_code == 200 and r.json["finalizations_applied"] == 1
    assert seeded.get(f"/api/snippet?id={SID}").json["final_text"] == "IMPORTED TEXT"

    # regression: applying an older/different version must never override an *already*-finalized
    # snippet - same rule sync_reviews() already enforces for the automatic app-1 sync
    payload2 = {**payload, "snippets": [{**payload["snippets"][0], "final_text": "SOMETHING ELSE"}]}
    v2 = seeded.post("/api/admin/books/B1/import", headers=ROOT, json=payload2).json
    r2 = seeded.post(f"/api/admin/books/B1/versions/{v2['id']}/apply", headers=ROOT)
    assert r2.json["finalizations_applied"] == 0
    assert seeded.get(f"/api/snippet?id={SID}").json["final_text"] == "IMPORTED TEXT"  # unchanged


def test_download_a_historical_version(seeded):
    v = seeded.get("/api/admin/books/B1/export", headers=ROOT).json
    versions = seeded.get("/api/admin/books/B1/versions", headers=ROOT).json["versions"]
    vid = versions[0]["id"]
    assert seeded.get(f"/api/admin/books/B1/versions/{vid}/download", headers=REVIEWER1).status_code == 403
    r = seeded.get(f"/api/admin/books/B1/versions/{vid}/download", headers=ROOT)
    assert r.status_code == 200 and r.json["book_id"] == v["book_id"]


def test_delete_snippet_requires_manage_books(seeded):
    assert seeded.delete(f"/api/admin/snippets/{SID}").status_code == 401  # not signed in at all
    assert seeded.delete(f"/api/admin/snippets/{SID}", headers=REVIEWER1).status_code == 403
    assert seeded.delete(f"/api/admin/snippets/{SID}", headers=EDITOR).status_code == 403  # editor is below admin
    assert seeded.get(f"/api/snippet?id={SID}").status_code == 200  # still there


def test_delete_snippet_removes_it_and_its_suggestions(seeded):
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "a suggestion"})
    before = seeded.get("/api/library").json["books"][0]["total"]

    r = seeded.delete(f"/api/admin/snippets/{SID}", headers=ROOT)
    assert r.status_code == 200 and r.json["ok"] is True

    assert seeded.get(f"/api/snippet?id={SID}").status_code == 404
    assert seeded.get("/api/library").json["books"][0]["total"] == before - 1

    # re-ingesting the same book no longer finds a suggestion to keep - it's gone, not just hidden
    seeded.post("/api/ingest/book", headers={"X-Ingest-Key": KEY}, json={"book": {"id": "B1"}, "snippets": snippets()})
    assert seeded.get(f"/api/snippet?id={SID}", headers=REVIEWER1).json["my_suggestion"] is None


def test_delete_snippet_404_for_unknown_id(seeded):
    r = seeded.delete("/api/admin/snippets/B1:9:P:9", headers=ROOT)
    assert r.status_code == 404


def test_delete_snippet_also_removes_its_local_storage_file(client, tmp_path):
    r = client.post("/api/ingest/book", headers={"X-Ingest-Key": KEY}, json={
        "book": {"id": "B2", "title": "Book Two", "page_count": 1},
        "snippets": [{"page_number": 1, "side": "P", "seq": 0, "ai_text": "text",
                       "snippet_image_url": "http://127.0.0.1:8200/files/books/B2/snippets/x.webp",
                       "bbox": [0, 0, 10, 10]}],
    })
    assert r.status_code == 200
    img = tmp_path / "files" / "books" / "B2" / "snippets" / "x.webp"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"fake-image")

    client.get("/api/me", headers=ROOT)
    resp = client.delete("/api/admin/snippets/B2:1:P:0", headers=ROOT)
    assert resp.status_code == 200 and resp.json["storage_warning"] is None
    assert not img.exists()
