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
    assert r.status_code == 200 and "Lipi-Sampada" in r.json["service"] and "/api/library" in r.json["try_these"]
