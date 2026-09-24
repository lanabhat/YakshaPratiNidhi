import pytest

from lipisampada.reviewapi import permissions as P
from test_api import EDITOR, ROOT, REVIEWER1, SID, client, h, seeded  # noqa: F401  (fixtures + helpers)

ADMIN, NEWBIE, GUEST = h("adm@x.org"), h("new@x.org"), {"X-Guest-Id": "guest-abc"}


def post(c, url, who, **body):
    return c.post(url, headers=who, json=body)


def make_admin(c):
    c.get("/api/me", headers=ADMIN)
    assert post(c, "/api/admin/users/dev:adm@x.org/role", ROOT, role="admin").status_code == 200


# ------------------------------------------------------------------ the permission table
def test_the_permission_matrix_is_pinned():
    expected = {
        "guest": ["read", "suggest"],
        "reviewer": ["read", "suggest"],
        "editor": ["approve_text", "read", "suggest"],
        "admin": ["approve_text", "manage_books", "manage_users", "read", "reopen", "suggest", "view_stats"],
        "superadmin": ["approve_text", "grant_superadmin", "manage_books", "manage_users", "read", "reopen", "suggest", "view_stats"],
    }
    assert P.ROLE_PERMS == expected


def test_can_require_and_deactivated_accounts():
    assert P.can({"role": "editor"}, "approve_text") and not P.can({"role": "reviewer"}, "approve_text")
    assert P.can(None, "read") and not P.can(None, "suggest")
    off = {"role": "admin", "active": 0}
    assert P.permissions_for(off) == ["read"]
    with pytest.raises(P.Forbidden, match="deactivated"):
        P.require(off, "suggest")
    with pytest.raises(P.Forbidden, match="editor role or higher"):
        P.require({"role": "reviewer"}, "approve_text")


def test_me_lists_the_persons_permissions(seeded):
    assert seeded.get("/api/me", headers=EDITOR).json["permissions"] == P.ROLE_PERMS["editor"]
    assert seeded.get("/api/me", headers=REVIEWER1).json["permissions"] == P.ROLE_PERMS["reviewer"]
    assert seeded.get("/api/me").json["permissions"] == ["read"]


def test_admin_endpoints_are_closed_to_everyone_below_admin(seeded):
    for who in (EDITOR, REVIEWER1, GUEST, None):
        hdr = who or {}
        for method, url in [("get", "/api/admin/users"), ("get", "/api/admin/permissions"), ("get", "/api/admin/invites"),
                            ("get", "/api/admin/stats"), ("get", "/api/admin/books")]:
            r = getattr(seeded, method)(url, headers=hdr)
            assert r.status_code in (401, 403), (who, url, r.status_code)
    assert seeded.get("/api/admin/permissions", headers=ROOT).json["roles"] == P.ROLES


# ------------------------------------------------------------------ roles, safeguards
def test_nobody_changes_or_deactivates_their_own_account(seeded):
    assert post(seeded, "/api/admin/users/dev:root@x.org/role", ROOT, role="reviewer").status_code == 403
    assert post(seeded, "/api/admin/users/dev:root@x.org/active", ROOT, active=False).status_code == 403


def test_admins_cannot_touch_superadmins_or_grant_it(seeded):
    make_admin(seeded)
    assert post(seeded, "/api/admin/users/dev:root@x.org/role", ADMIN, role="reviewer").status_code == 403
    assert post(seeded, "/api/admin/users/dev:root@x.org/active", ADMIN, active=False).status_code == 403
    seeded.get("/api/me", headers=NEWBIE)
    assert post(seeded, "/api/admin/users/dev:new@x.org/role", ADMIN, role="superadmin").status_code == 403
    assert post(seeded, "/api/admin/invites", ADMIN, email="z@x.org", role="superadmin").status_code == 403
    assert post(seeded, "/api/admin/users/dev:new@x.org/role", ADMIN, role="editor").status_code == 200


# ------------------------------------------------------------------ invites
def test_an_invite_sets_the_role_on_first_sign_in_and_is_used_up(seeded):
    assert post(seeded, "/api/admin/invites", ROOT, email="New@X.org", role="editor").status_code == 200
    pending = seeded.get("/api/admin/invites", headers=ROOT).json["invites"]
    assert [(i["email"], i["role"]) for i in pending] == [("new@x.org", "editor")]
    assert seeded.get("/api/me", headers=NEWBIE).json["user"]["role"] == "editor"
    assert seeded.get("/api/admin/invites", headers=ROOT).json["invites"] == []


def test_invite_validation_and_removal(seeded):
    assert post(seeded, "/api/admin/invites", ROOT, email="not-an-email", role="editor").status_code == 400
    assert post(seeded, "/api/admin/invites", ROOT, email="a@x.org", role="guest").status_code == 400
    assert post(seeded, "/api/admin/invites", ROOT, email="ed@x.org", role="admin").status_code == 400  # already has an account
    post(seeded, "/api/admin/invites", ROOT, email="a@x.org", role="editor")
    assert seeded.delete("/api/admin/invites?email=a@x.org", headers=ROOT).status_code == 200
    assert seeded.get("/api/admin/invites", headers=ROOT).json["invites"] == []
    assert seeded.get("/api/me", headers=h("a@x.org")).json["user"]["role"] == "reviewer"


# ------------------------------------------------------------------ deactivation
def test_a_deactivated_user_can_read_but_not_write(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    body = {"snippet_id": SID, "kind": "confirm"}
    assert seeded.post("/api/suggest", headers=REVIEWER1, json=body).status_code == 200
    assert post(seeded, "/api/admin/users/dev:r1@x.org/active", ROOT, active=False).status_code == 200
    assert seeded.get("/api/library", headers=REVIEWER1).status_code == 200
    r = seeded.post("/api/suggest", headers=REVIEWER1, json=body)
    assert r.status_code == 403 and "deactivated" in r.json["error"]
    assert seeded.get("/api/me", headers=REVIEWER1).json["permissions"] == ["read"]
    assert post(seeded, "/api/admin/users/dev:r1@x.org/active", ROOT, active=True).status_code == 200
    assert seeded.post("/api/suggest", headers=REVIEWER1, json=body).status_code == 200


def test_a_deactivated_editor_cannot_approve(seeded):
    post(seeded, "/api/admin/users/dev:ed@x.org/active", ROOT, active=False)
    assert seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID}).status_code == 403


# ------------------------------------------------------------------ users list, stats
def test_user_search_and_role_filter(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    names = lambda q: [u["email"] for u in seeded.get(f"/api/admin/users{q}", headers=ROOT).json["users"]]
    assert set(names("")) == {"ed@x.org", "root@x.org", "r1@x.org"}
    assert names("?q=R1") == ["r1@x.org"]
    assert names("?role=editor") == ["ed@x.org"]
    assert all("last_seen" in u and u["active"] == 1 for u in seeded.get("/api/admin/users", headers=ROOT).json["users"])


def test_contributor_stats_count_edits_confirms_and_approvals(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "edit", "text": "ಹರಿ ಬದಲಾಯಿತು"})
    seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": "B1:1:P:1", "kind": "confirm"})
    seeded.post("/api/suggest", headers=GUEST, json={"snippet_id": SID, "kind": "confirm"})
    seeded.post("/api/finalize", headers=EDITOR, json={"snippet_id": SID})
    stats = seeded.get("/api/admin/stats", headers=ROOT).json
    by = {c["email"]: c for c in stats["contributors"]}
    assert (by["r1@x.org"]["edits"], by["r1@x.org"]["confirms"], by["r1@x.org"]["approvals"]) == (1, 1, 0)
    assert by["ed@x.org"]["approvals"] == 1
    assert stats["guests"]["people"] == 1 and stats["guests"]["confirms"] == 1
    one = {c["email"]: c for c in seeded.get("/api/admin/stats?book=NOPE", headers=ROOT).json["contributors"]}
    assert one["r1@x.org"]["edits"] == 0


# ------------------------------------------------------------------ hiding books
def test_a_hidden_book_vanishes_for_everyone_but_book_managers(seeded):
    make_admin(seeded)
    assert [b["id"] for b in seeded.get("/api/library", headers=REVIEWER1).json["books"]] == ["B1"]
    assert post(seeded, "/api/admin/books/B1/hidden", EDITOR, hidden=True).status_code == 403
    assert post(seeded, "/api/admin/books/B1/hidden", ADMIN, hidden=True).status_code == 200
    assert seeded.get("/api/library", headers=REVIEWER1).json["books"] == []
    assert seeded.get("/api/library").json["books"] == []
    for url in ("/api/books/B1", "/api/books/B1/pages/1", "/api/books/B1/read", "/api/books/B1/text", f"/api/snippet?id={SID}"):
        assert seeded.get(url, headers=REVIEWER1).status_code == 404, url
    assert seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "confirm"}).status_code == 404
    assert [b["id"] for b in seeded.get("/api/library", headers=ADMIN).json["books"]] == ["B1"]
    assert seeded.get("/api/books/B1/pages/1", headers=ADMIN).status_code == 200
    assert [b["hidden"] for b in seeded.get("/api/admin/books", headers=ADMIN).json["books"]] == [1]
    assert post(seeded, "/api/admin/books/B1/hidden", ADMIN, hidden=False).status_code == 200
    assert seeded.get("/api/library", headers=REVIEWER1).json["books"][0]["id"] == "B1"
    assert post(seeded, "/api/admin/books/NOPE/hidden", ADMIN, hidden=True).status_code == 404


def test_reingesting_a_hidden_book_keeps_it_hidden(seeded):
    from test_api import KEY, snippets

    post(seeded, "/api/admin/books/B1/hidden", ROOT, hidden=True)
    seeded.post("/api/ingest/book", headers={"X-Ingest-Key": KEY}, json={"book": {"id": "B1"}, "snippets": snippets()})
    assert seeded.get("/api/library", headers=REVIEWER1).json["books"] == []


# ------------------------------------------------------------------ old databases
def test_an_old_database_gains_the_new_columns(tmp_path):
    import sqlite3

    from lipisampada.reviewapi.db import Db

    path = tmp_path / "old.sqlite3"
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE, email TEXT, name TEXT,"
                    " role TEXT NOT NULL DEFAULT 'reviewer', created_at TEXT NOT NULL);"
                    "CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT, kavi TEXT, catalog_entry_id INTEGER, page_count INTEGER,"
                    " bundle_url TEXT, published_at TEXT);"
                    "INSERT INTO users (uid, email, role, created_at) VALUES ('dev:a@x.org','a@x.org','editor','2026-01-01');")
    c.commit()
    c.close()
    db = Db(path)
    u = db.get_or_create_user("dev:a@x.org", "a@x.org")
    assert u["role"] == "editor" and u["active"] == 1
    assert {r[1] for r in db.conn.execute("PRAGMA table_info(books)")} >= {"hidden"}
