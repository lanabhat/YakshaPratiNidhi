import pytest

from test_api import EDITOR, KEY, ROOT, REVIEWER1, SID, client, h, seeded, snippets  # noqa: F401

NEWBIE = h("newbie@x.org")


def post(c, url, who, **body):
    return c.post(url, headers=who, json=body)


def test_a_fresh_google_sign_in_starts_new_and_guest_level(seeded):
    r = seeded.get("/api/me", headers=NEWBIE)
    assert r.json["user"]["application_status"] == "new"
    assert r.json["permissions"] == ["read", "suggest"]
    # already usable at guest level - can read and suggest before ever filling the form
    assert seeded.get("/api/library", headers=NEWBIE).status_code == 200
    assert seeded.post("/api/suggest", headers=NEWBIE, json={"snippet_id": SID, "kind": "confirm"}).status_code == 200
    # but not anything beyond that
    assert seeded.post("/api/finalize", headers=NEWBIE, json={"snippet_id": SID}).status_code == 403


def test_applying_yes_is_pending_and_still_guest_level_until_approved(seeded):
    seeded.get("/api/me", headers=NEWBIE)
    r = post(seeded, "/api/apply", NEWBIE, name="Newbie", place="Mysuru", wants_to_volunteer=True, extra_info="happy to help")
    assert r.status_code == 200 and r.json["user"]["application_status"] == "pending"
    assert r.json["permissions"] == ["read", "suggest"]
    assert seeded.post("/api/finalize", headers=NEWBIE, json={"snippet_id": SID}).status_code == 403
    apps = seeded.get("/api/admin/applications", headers=ROOT).json["applications"]
    assert [a["email"] for a in apps] == ["newbie@x.org"]
    assert apps[0]["place"] == "Mysuru" and apps[0]["wants_to_volunteer"] == 1

    assert post(seeded, "/api/admin/applications/dev:newbie@x.org/decide", ROOT, approve=True).status_code == 200
    me = seeded.get("/api/me", headers=NEWBIE).json
    assert me["user"]["application_status"] == "approved" and me["user"]["role"] == "reviewer"
    assert seeded.get("/api/admin/applications", headers=ROOT).json["applications"] == []
    # deciding twice is refused - it's no longer pending
    assert post(seeded, "/api/admin/applications/dev:newbie@x.org/decide", ROOT, approve=True).status_code == 403


def test_applying_no_declines_and_a_repeat_sign_in_does_not_reset_it(seeded):
    seeded.get("/api/me", headers=NEWBIE)
    r = post(seeded, "/api/apply", NEWBIE, name="Newbie", place="", wants_to_volunteer=False, extra_info="")
    assert r.json["user"]["application_status"] == "declined" and r.json["permissions"] == ["read", "suggest"]
    assert seeded.get("/api/admin/applications", headers=ROOT).json["applications"] == []
    # signing in again (e.g. a new browser tab) must not put them back at "new"
    again = seeded.get("/api/me", headers=NEWBIE).json
    assert again["user"]["application_status"] == "declined"
    assert post(seeded, "/api/apply", NEWBIE, wants_to_volunteer=True).status_code == 403  # "already answered"


def test_admin_can_reject_an_application(seeded):
    seeded.get("/api/me", headers=NEWBIE)
    post(seeded, "/api/apply", NEWBIE, wants_to_volunteer=True)
    assert post(seeded, "/api/admin/applications/dev:newbie@x.org/decide", ROOT, approve=False).status_code == 200
    me = seeded.get("/api/me", headers=NEWBIE).json
    assert me["user"]["application_status"] == "rejected" and me["permissions"] == ["read", "suggest"]


def test_an_invited_account_skips_the_gate_entirely(seeded):
    post(seeded, "/api/admin/invites", ROOT, email="vip@x.org", role="editor")
    r = seeded.get("/api/me", headers=h("vip@x.org")).json
    assert r["user"]["role"] == "editor" and r["user"]["application_status"] == "unset"
    assert "approve_text" in r["permissions"]


def test_a_direct_role_change_by_an_admin_also_counts_as_approval(seeded):
    seeded.get("/api/me", headers=NEWBIE)  # application_status starts "new"
    assert post(seeded, "/api/admin/users/dev:newbie@x.org/role", ROOT, role="editor").status_code == 200
    me = seeded.get("/api/me", headers=NEWBIE).json
    assert me["user"]["application_status"] == "approved" and "approve_text" in me["permissions"]


# ------------------------------------------------------------------ bans
def test_a_banned_user_has_zero_permissions_and_is_refused_everywhere_but_me(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    assert post(seeded, "/api/admin/users/dev:r1@x.org/ban", ROOT, reason="posting garbage").status_code == 200
    me = seeded.get("/api/me", headers=REVIEWER1).json
    assert me["permissions"] == [] and me["user"]["banned"] is True and me["user"]["ban_reason"] == "posting garbage"
    r = seeded.get("/api/library", headers=REVIEWER1)
    assert r.status_code == 403 and "banned" in r.json["error"]
    assert seeded.get(f"/api/books/{seeded.get('/api/library', headers=ROOT).json['books'][0]['id']}", headers=REVIEWER1).status_code == 403
    assert seeded.post("/api/suggest", headers=REVIEWER1, json={"snippet_id": SID, "kind": "confirm"}).status_code == 403
    # unban restores their normal role-based permissions
    assert post(seeded, "/api/admin/users/dev:r1@x.org/ban", ROOT, banned=False).status_code == 200
    me2 = seeded.get("/api/me", headers=REVIEWER1).json
    assert me2["user"]["banned"] is False and me2["permissions"] == ["read", "suggest"]


def test_ban_safeguards_mirror_deactivate(seeded):
    seeded.get("/api/me", headers=REVIEWER1)
    assert post(seeded, "/api/admin/users/dev:root@x.org/ban", ROOT, reason="x").status_code == 403  # can't ban self
    seeded.get("/api/me", headers=h("adm2@x.org"))
    post(seeded, "/api/admin/users/dev:adm2@x.org/role", ROOT, role="admin")
    assert post(seeded, "/api/admin/users/dev:root@x.org/ban", h("adm2@x.org"), reason="x").status_code == 403  # admin can't ban a superadmin
    assert post(seeded, "/api/admin/users/dev:r1@x.org/ban", h("adm2@x.org"), reason="unhelpful").status_code == 200  # but can ban a reviewer


# ------------------------------------------------------------------ rate limiting
def test_apply_is_rate_limited(seeded):
    seeded.get("/api/me", headers=NEWBIE)
    ok = post(seeded, "/api/apply", NEWBIE, wants_to_volunteer=True)
    assert ok.status_code == 200
    hit_limit = False
    for _ in range(6):
        r = post(seeded, "/api/apply", NEWBIE, wants_to_volunteer=True)
        if r.status_code == 429:
            hit_limit = True
            assert "error" in r.json
            break
    assert hit_limit


# ------------------------------------------------------------------ migration
def test_an_old_database_gains_the_application_and_ban_columns(tmp_path):
    import sqlite3

    from lipisampada.reviewapi.db import Db

    path = tmp_path / "old.sqlite3"
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE, email TEXT, name TEXT,"
        " role TEXT NOT NULL DEFAULT 'reviewer', created_at TEXT NOT NULL);"
        "CREATE TABLE books (id TEXT PRIMARY KEY, title TEXT, kavi TEXT, catalog_entry_id INTEGER, page_count INTEGER,"
        " bundle_url TEXT, published_at TEXT);"
        "INSERT INTO users (uid, email, role, created_at) VALUES ('dev:a@x.org','a@x.org','editor','2026-01-01');"
    )
    c.commit()
    c.close()
    db = Db(path)
    u = db.get_or_create_user("dev:a@x.org", "a@x.org")
    # a pre-existing account must NOT retroactively get gated to guest-level
    assert u["application_status"] == "unset" and u["banned"] == 0
    from lipisampada.reviewapi import permissions as P

    assert P.permissions_for(u) == P.ROLE_PERMS["editor"]
