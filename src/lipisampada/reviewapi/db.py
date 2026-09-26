"""Storage + business rules for the review platform (SQLite; plain SQL kept
portable so it can move to MySQL/Postgres on PythonAnywhere later).

Model in one paragraph: a *book* has *snippets* (paragraphs). Each snippet
has the AI text, a *working_text* (the AI text plus any word changes an
editor has accepted so far) and, once an editor/admin approves it, a
*final_text*. Anyone (even a guest) can attach a *suggestion* (an edited
text, or a "confirm - looks right" vote); one per person per snippet, the
latest replaces the earlier. Suggestions are diffed against the current text
(final if finalized, else working) and tallied per word - see tally.py.
Suggestions stay open after finalization so a finalized snippet can be
challenged."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from lipisampada.reviewapi import tally as tallymod

from lipisampada.reviewapi import permissions as perms
from lipisampada.reviewapi.permissions import ASSIGNABLE_ROLES, ROLES, Forbidden, require  # noqa: F401 (Forbidden re-exported)

FINALIZE_ROLES = perms.roles_with("approve_text")
ADMIN_ROLES = perms.roles_with("manage_users")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL UNIQUE,
    email TEXT,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'reviewer',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    email TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    invited_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT,
    kavi TEXT,
    catalog_entry_id INTEGER,
    page_count INTEGER,
    bundle_url TEXT,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS snippets (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    side TEXT NOT NULL,
    seq INTEGER NOT NULL,
    page_image_url TEXT,
    snippet_image_url TEXT,
    bbox TEXT,
    ai_text TEXT NOT NULL,
    working_text TEXT NOT NULL,
    final_text TEXT,
    finalized_by TEXT,
    finalized_at TEXT,
    refine_failed INTEGER NOT NULL DEFAULT 0,
    suggestion_count INTEGER NOT NULL DEFAULT 0,
    attention INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_snippets_book_page ON snippets(book_id, page_index, seq);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snippet_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(snippet_id, user_id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    user_uid TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS book_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_book_versions_book ON book_versions(book_id, created_at);
"""


class NotFound(Exception):
    pass


class Db:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._add_columns("users", {"active": "INTEGER NOT NULL DEFAULT 1", "last_seen": "TEXT"})
        self._add_columns("users", {
            "place": "TEXT", "wants_to_volunteer": "INTEGER", "extra_info": "TEXT", "applied_at": "TEXT",
            "application_status": "TEXT NOT NULL DEFAULT 'unset'",
            "banned": "INTEGER NOT NULL DEFAULT 0", "ban_reason": "TEXT",
        })
        self._add_columns("books", {"hidden": "INTEGER NOT NULL DEFAULT 0"})
        self.conn.commit()

    def _add_columns(self, table: str, columns: dict):  # databases created before a column existed
        have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # -- helpers ----------------------------------------------------------
    def q(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def q1(self, sql, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def log(self, user, action, target=None, detail=None):
        self.conn.execute(
            "INSERT INTO audit (at, user_uid, action, target, detail) VALUES (?,?,?,?,?)",
            (now(), user["uid"] if user else None, action, target, json.dumps(detail) if detail else None),
        )

    # -- users ------------------------------------------------------------
    def get_or_create_user(self, uid, email=None, name=None, role="reviewer") -> dict:
        with self.lock:
            u = self.q1("SELECT * FROM users WHERE uid = ?", (uid,))
            if u:
                if (email and u["email"] != email) or (name and u["name"] != name):
                    self.conn.execute(
                        "UPDATE users SET email = COALESCE(?, email), name = COALESCE(?, name) WHERE uid = ?",
                        (email, name, uid),
                    )
                    u = self.q1("SELECT * FROM users WHERE uid = ?", (uid,))
                if self._stale(u["last_seen"]):  # throttled: at most one write a minute per person
                    self.conn.execute("UPDATE users SET last_seen = ? WHERE uid = ?", (now(), uid))
                    u = self.q1("SELECT * FROM users WHERE uid = ?", (uid,))
                self.conn.commit()
                return u
            # A review sync (see sync_reviews) may have already created a "pending:<email>" placeholder
            # for this person, attributing suggestions/finalizations to it before they ever signed in
            # here for real. Claim it now - same row, same id, so that history stays theirs - rather
            # than starting a second, empty account.
            if email:
                pending = self.q1("SELECT * FROM users WHERE uid = ?", (f"pending:{email.lower()}",))
                if pending:
                    self.conn.execute(
                        "UPDATE users SET uid = ?, name = COALESCE(?, name), last_seen = ? WHERE uid = ?",
                        (uid, name, now(), pending["uid"]),
                    )
                    self.conn.commit()
                    return self.q1("SELECT * FROM users WHERE uid = ?", (uid,))
            invited = False
            if email and role == "reviewer":  # first sign-in: an invite decides the starting role
                inv = self.q1("SELECT * FROM invites WHERE email = ?", (email.lower(),))
                if inv:
                    role, invited = inv["role"], True
                    self.conn.execute("DELETE FROM invites WHERE email = ?", (email.lower(),))
                    self.log(None, "invite_used", uid, {"email": email, "role": role})
            # A brand-new, uninvited Google sign-in has to fill in the onboarding form (see apply())
            # before it counts as more than a guest - everyone else (invited, superadmin bootstrap,
            # anonymous guests) skips that gate entirely.
            application_status = "new" if (email and role == "reviewer" and not invited) else "unset"
            self.conn.execute(
                "INSERT INTO users (uid, email, name, role, created_at, last_seen, application_status) VALUES (?,?,?,?,?,?,?)",
                (uid, email, name, role, now(), now(), application_status),
            )
            self.conn.commit()
            return self.q1("SELECT * FROM users WHERE uid = ?", (uid,))

    @staticmethod
    def _stale(ts) -> bool:
        if not ts:
            return True
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() > 60

    def _target_user(self, uid: str) -> dict:
        target = self.q1("SELECT * FROM users WHERE uid = ?", (uid,))
        if not target:
            raise NotFound("no such user")
        return target

    def set_role(self, actor: dict, target_uid: str, role: str):
        if role not in ASSIGNABLE_ROLES:
            raise ValueError(f"invalid role {role!r}")
        require(actor, "manage_users")
        with self.lock:
            target = self._target_user(target_uid)
            if target["uid"] == actor["uid"]:
                raise Forbidden("you can't change your own role - ask another admin")
            if "superadmin" in (target["role"], role):
                require(actor, "grant_superadmin")
            # An admin directly setting a role is itself an approval - it clears any pending/declined
            # onboarding gate so the new role actually takes effect (see permissions.GATED_STATUSES).
            self.conn.execute(
                "UPDATE users SET role = ?, application_status = CASE WHEN application_status = 'unset' "
                "THEN 'unset' ELSE 'approved' END WHERE uid = ?",
                (role, target_uid),
            )
            self.log(actor, "set_role", target_uid, {"role": role})
            self.conn.commit()

    def set_active(self, actor: dict, target_uid: str, active: bool):
        require(actor, "manage_users")
        with self.lock:
            target = self._target_user(target_uid)
            if target["uid"] == actor["uid"]:
                raise Forbidden("you can't deactivate your own account")
            if target["role"] == "superadmin":
                require(actor, "grant_superadmin")
            self.conn.execute("UPDATE users SET active = ? WHERE uid = ?", (1 if active else 0, target_uid))
            self.log(actor, "activate" if active else "deactivate", target_uid)
            self.conn.commit()

    def set_banned(self, actor: dict, target_uid: str, banned: bool, reason: str | None = None):
        """A ban is stronger than deactivate: zero access at all (not even read), for someone who is
        actively unhelpful or disruptive rather than just on pause."""
        require(actor, "manage_users")
        with self.lock:
            target = self._target_user(target_uid)
            if target["uid"] == actor["uid"]:
                raise Forbidden("you can't ban your own account")
            if target["role"] == "superadmin":
                require(actor, "grant_superadmin")
            reason = (reason or "").strip() or None if banned else None
            self.conn.execute("UPDATE users SET banned = ?, ban_reason = ? WHERE uid = ?", (1 if banned else 0, reason, target_uid))
            self.log(actor, "ban" if banned else "unban", target_uid, {"reason": reason} if banned else None)
            self.conn.commit()

    # -- volunteer applications: the onboarding form after a brand-new Google sign-in -----
    def apply(self, user: dict, name: str, place: str, wants_to_volunteer: bool, extra_info: str) -> dict:
        if user["application_status"] != "new":
            raise Forbidden("you've already answered this")
        status = "pending" if wants_to_volunteer else "declined"
        with self.lock:
            self.conn.execute(
                "UPDATE users SET name = COALESCE(?, name), place = ?, wants_to_volunteer = ?, extra_info = ?, "
                "application_status = ?, applied_at = ? WHERE uid = ?",
                ((name or "").strip() or None, (place or "").strip() or None, int(bool(wants_to_volunteer)),
                 (extra_info or "").strip() or None, status, now(), user["uid"]),
            )
            self.log(user, "apply", user["uid"], {"status": status})
            self.conn.commit()
            return self.q1("SELECT * FROM users WHERE uid = ?", (user["uid"],))

    def list_applications(self, actor: dict):
        require(actor, "manage_users")
        return self.q(
            "SELECT uid, email, name, place, wants_to_volunteer, extra_info, applied_at FROM users "
            "WHERE application_status = 'pending' ORDER BY applied_at"
        )

    def decide_application(self, actor: dict, target_uid: str, approve: bool):
        require(actor, "manage_users")
        with self.lock:
            target = self._target_user(target_uid)
            if target["application_status"] != "pending":
                raise Forbidden("this application is not pending")
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE users SET application_status = ?, role = COALESCE(role, 'reviewer') WHERE uid = ?",
                (status, target_uid),
            )
            self.log(actor, "approve_application" if approve else "reject_application", target_uid)
            self.conn.commit()
            return self.q1("SELECT * FROM users WHERE uid = ?", (target_uid,))

    def list_users(self, q: str = "", role: str = ""):
        sql = ("SELECT id, uid, email, name, role, active, last_seen, created_at, application_status, "
               "banned, ban_reason FROM users WHERE role != 'guest'")
        params: list = []
        if q.strip():
            sql += " AND (LOWER(COALESCE(name,'')) LIKE ? OR LOWER(COALESCE(email,'')) LIKE ?)"
            params += [f"%{q.strip().lower()}%"] * 2
        if role:
            sql += " AND role = ?"
            params.append(role)
        return self.q(sql + " ORDER BY id", params)

    # -- invites: a role waiting for someone's first sign-in --------------
    def create_invite(self, actor: dict, email: str, role: str):
        require(actor, "manage_users")
        email = (email or "").strip().lower()
        if "@" not in email or " " in email:
            raise ValueError("enter a valid email address")
        if role not in ASSIGNABLE_ROLES:
            raise ValueError(f"invalid role {role!r}")
        if role == "superadmin":
            require(actor, "grant_superadmin")
        with self.lock:
            if self.q1("SELECT 1 AS x FROM users WHERE LOWER(email) = ?", (email,)):
                raise ValueError("that person already has an account - change their role under Users")
            self.conn.execute(
                "INSERT INTO invites (email, role, invited_by, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET role=excluded.role, invited_by=excluded.invited_by, created_at=excluded.created_at",
                (email, role, actor["uid"], now()),
            )
            self.log(actor, "invite", email, {"role": role})
            self.conn.commit()

    def list_invites(self, actor: dict):
        require(actor, "manage_users")
        return self.q("SELECT email, role, invited_by, created_at FROM invites ORDER BY created_at DESC")

    def delete_invite(self, actor: dict, email: str):
        require(actor, "manage_users")
        with self.lock:
            self.conn.execute("DELETE FROM invites WHERE email = ?", ((email or "").strip().lower(),))
            self.log(actor, "invite_deleted", email)
            self.conn.commit()

    # -- contributor stats --------------------------------------------------
    def contributor_stats(self, actor: dict, book_id: str | None = None) -> dict:
        require(actor, "view_stats")
        scope = "AND su.snippet_id IN (SELECT id FROM snippets WHERE book_id = ?)" if book_id else ""
        params = [book_id] if book_id else []
        rows = self.q(
            f"""SELECT u.uid, u.name, u.email, u.role, u.active, u.last_seen,
                  COALESCE(SUM(CASE WHEN su.kind='edit' THEN 1 ELSE 0 END),0) AS edits,
                  COALESCE(SUM(CASE WHEN su.kind='confirm' THEN 1 ELSE 0 END),0) AS confirms
                FROM users u LEFT JOIN suggestions su ON su.user_id = u.id {scope}
                WHERE u.role != 'guest' GROUP BY u.id ORDER BY u.id""",
            params,
        )
        approved = {
            r["finalized_by"]: r["n"]
            for r in self.q(
                "SELECT finalized_by, COUNT(*) AS n FROM snippets WHERE final_text IS NOT NULL"
                + (" AND book_id = ?" if book_id else "") + " GROUP BY finalized_by",
                params,
            )
        }
        for r in rows:
            r["approvals"] = approved.get(r["uid"], 0)
        g = self.q1(
            f"""SELECT COUNT(DISTINCT su.user_id) AS people,
                  COALESCE(SUM(CASE WHEN su.kind='edit' THEN 1 ELSE 0 END),0) AS edits,
                  COALESCE(SUM(CASE WHEN su.kind='confirm' THEN 1 ELSE 0 END),0) AS confirms
                FROM suggestions su JOIN users u ON u.id = su.user_id WHERE u.role = 'guest' {scope}""",
            params,
        )
        return {"contributors": rows, "guests": g}

    # -- book visibility ----------------------------------------------------
    def is_hidden(self, book_id: str) -> bool:
        b = self.q1("SELECT hidden FROM books WHERE id = ?", (book_id,))
        return bool(b and b["hidden"])

    def set_hidden(self, actor: dict, book_id: str, hidden: bool):
        require(actor, "manage_books")
        with self.lock:
            if not self.q1("SELECT 1 AS x FROM books WHERE id = ?", (book_id,)):
                raise NotFound("no such book")
            self.conn.execute("UPDATE books SET hidden = ? WHERE id = ?", (1 if hidden else 0, book_id))
            self.log(actor, "hide_book" if hidden else "show_book", book_id)
            self.conn.commit()

    def delete_snippet(self, actor: dict, snippet_id: str) -> dict:
        """Removes a snippet's review data (its suggestions and the row itself). Returns the
        snippet's stored image URL so the caller can also remove it from storage - deleting the DB
        row first means a storage hiccup only orphans a harmless, no-longer-referenced image rather
        than leaving a snippet visible in review with a dead image link."""
        require(actor, "manage_books")
        with self.lock:
            s = self.q1("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
            if not s:
                raise NotFound("no such snippet")
            self.conn.execute("DELETE FROM suggestions WHERE snippet_id = ?", (snippet_id,))
            self.conn.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
            self.log(actor, "delete_snippet", snippet_id, {"snippet_image_url": s["snippet_image_url"]})
            self.conn.commit()
        return {"snippet_image_url": s["snippet_image_url"]}

    # -- ingest -----------------------------------------------------------
    def ingest_book(self, book: dict, snippets: list[dict]) -> dict:
        """Idempotent. Re-ingesting refreshes image URLs/metadata but never
        overwrites text a person has touched (suggested on, edited, finalized)."""
        with self.lock:
            self.conn.execute(
                """INSERT INTO books (id, title, kavi, catalog_entry_id, page_count, bundle_url, published_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, kavi=excluded.kavi,
                     catalog_entry_id=excluded.catalog_entry_id, page_count=excluded.page_count,
                     bundle_url=excluded.bundle_url, published_at=excluded.published_at""",
                (book["id"], book.get("title"), book.get("kavi"), book.get("catalog_entry_id"),
                 book.get("page_count"), book.get("bundle_url"), now()),
            )
            order = sorted({(s["page_number"], s["side"]) for s in snippets})
            page_index = {k: i for i, k in enumerate(order, 1)}
            created = updated = 0
            for s in snippets:
                sid = f"{book['id']}:{s['page_number']}:{s['side']}:{s['seq']}"
                pi = page_index[(s["page_number"], s["side"])]
                existing = self.q1("SELECT * FROM snippets WHERE id = ?", (sid,))
                bbox = json.dumps(s.get("bbox")) if s.get("bbox") is not None else None
                if not existing:
                    self.conn.execute(
                        """INSERT INTO snippets (id, book_id, page_index, page_number, side, seq, page_image_url,
                             snippet_image_url, bbox, ai_text, working_text, refine_failed, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, book["id"], pi, s["page_number"], s["side"], s["seq"], s.get("page_image_url"),
                         s.get("snippet_image_url"), bbox, s["ai_text"], s["ai_text"],
                         int(bool(s.get("refine_failed"))), now()),
                    )
                    created += 1
                else:
                    untouched = (
                        existing["final_text"] is None
                        and existing["suggestion_count"] == 0
                        and existing["working_text"] == existing["ai_text"]
                    )
                    self.conn.execute(
                        """UPDATE snippets SET page_index=?, page_image_url=?, snippet_image_url=?, bbox=?,
                             refine_failed=?, updated_at=? WHERE id=?""",
                        (pi, s.get("page_image_url"), s.get("snippet_image_url"), bbox,
                         int(bool(s.get("refine_failed"))), now(), sid),
                    )
                    if untouched and existing["ai_text"] != s["ai_text"]:
                        self.conn.execute(
                            "UPDATE snippets SET ai_text=?, working_text=? WHERE id=?",
                            (s["ai_text"], s["ai_text"], sid),
                        )
                    updated += 1
            self.log(None, "ingest_book", book["id"], {"created": created, "updated": updated})
            self.conn.commit()
        return {"created": created, "updated": updated, "pages": len(order)}

    # -- review sync: for someone who reviews against a LOCAL copy of this app (their own machine,
    # a separate database from this one) before publishing reaches this, the deployed copy ---------
    def _find_or_create_synced_user(self, email: str, name: str | None, role: str) -> dict:
        """Resolve who a synced suggestion/finalization belongs to: an existing real account with this
        email if one has ever signed in here, else a placeholder they'll claim the moment they do
        (see get_or_create_user) - never a second, disconnected account for the same person."""
        email = email.strip().lower()
        u = self.q1(
            "SELECT * FROM users WHERE LOWER(email) = ? AND role != 'guest' ORDER BY (uid LIKE 'pending:%') LIMIT 1",
            (email,),
        )
        if u:
            return u
        uid = f"pending:{email}"
        self.conn.execute(
            "INSERT INTO users (uid, email, name, role, created_at, application_status) VALUES (?,?,?,?,?,'unset')",
            (uid, email, name, role if role in ASSIGNABLE_ROLES else "reviewer", now()),
        )
        return self.q1("SELECT * FROM users WHERE uid = ?", (uid,))

    def sync_reviews(self, book_id: str, users: list[dict], snippets: list[dict]) -> dict:
        """Merges suggestions/working-text/finalization made against a LOCAL copy of this app into
        this (the deployed) database. Additive and one-directional: never removes or downgrades
        anything already here - a suggestion only overwrites an older one from the *same* person, an
        already-finalized snippet here is left exactly as this deployment's editor left it."""
        with self.lock:
            if not self.q1("SELECT 1 AS x FROM books WHERE id = ?", (book_id,)):
                raise NotFound("no such book - publish its text/images first")
            by_email = {}
            for u in users:
                row = self._find_or_create_synced_user(u["email"], u.get("name"), u.get("role", "reviewer"))
                by_email[row["email"].lower()] = row
            suggestions_applied = finalizations_applied = working_text_applied = skipped = 0
            for s in snippets:
                existing = self.q1("SELECT * FROM snippets WHERE id = ? AND book_id = ?", (s["snippet_id"], book_id))
                if not existing:
                    skipped += 1
                    continue
                for sug in s.get("suggestions", []):
                    email = sug["email"].strip().lower()
                    user = by_email.get(email) or self._find_or_create_synced_user(email, None, "reviewer")
                    by_email[email] = user
                    self.conn.execute(
                        """INSERT INTO suggestions (snippet_id, user_id, kind, text, created_at) VALUES (?,?,?,?,?)
                           ON CONFLICT(snippet_id, user_id) DO UPDATE SET kind=excluded.kind, text=excluded.text,
                             created_at=excluded.created_at WHERE excluded.created_at > suggestions.created_at""",
                        (s["snippet_id"], user["id"], sug["kind"], sug.get("text"), sug["created_at"]),
                    )
                    suggestions_applied += 1
                if s.get("final_text") and existing["final_text"] is None:
                    finalizer = by_email.get((s.get("finalized_by_email") or "").strip().lower())
                    self.conn.execute(
                        "UPDATE snippets SET final_text=?, working_text=?, finalized_by=?, finalized_at=? WHERE id=?",
                        (s["final_text"], s["final_text"], finalizer["uid"] if finalizer else None,
                         s.get("finalized_at") or now(), s["snippet_id"]),
                    )
                    finalizations_applied += 1
                elif (existing["final_text"] is None and s.get("working_text")
                      and s["working_text"] != existing["working_text"]):
                    # not finalized anywhere yet, but local had already accepted word-changes this deployment hasn't seen
                    self.conn.execute("UPDATE snippets SET working_text = ? WHERE id = ?", (s["working_text"], s["snippet_id"]))
                    working_text_applied += 1
                self.recompute(s["snippet_id"])
            self.log(None, "sync_reviews", book_id,
                     {"suggestions": suggestions_applied, "finalizations": finalizations_applied, "skipped": skipped})
            self.conn.commit()
        return {
            "suggestions_applied": suggestions_applied, "finalizations_applied": finalizations_applied,
            "working_text_applied": working_text_applied, "skipped": skipped,
        }

    # -- manual export/import (browser-driven; the app 1 publish-time sync above stays automatic) ----
    def _build_review_payload(self, book_id: str) -> dict:
        """{"book_id", "book_title", "book_kavi", "users": [...], "snippets": [...]} - the export
        counterpart to sync_reviews()'s import shape, built from this (the live) connection. Same
        real-users-only, only-touched-snippets logic as publisher.sync_reviews_to_remote, which builds
        this same shape from an *external* sqlite file (app 1's local review database) instead."""
        book = self.q1("SELECT title, kavi FROM books WHERE id = ?", (book_id,))
        if not book:
            raise NotFound("no such book")
        snip_rows = self.q("SELECT * FROM snippets WHERE book_id = ? ORDER BY seq", (book_id,))
        users_by_id = {u["id"]: u for u in self.q("SELECT * FROM users")}

        def real(u):  # a real, attributable account - not an anonymous guest
            return u and u.get("email") and u["role"] != "guest"

        users_out, seen, snippets_out = [], set(), []
        for s in snip_rows:
            sug_out = []
            for sub in self.q("SELECT * FROM suggestions WHERE snippet_id = ?", (s["id"],)):
                u = users_by_id.get(sub["user_id"])
                if not real(u):
                    continue
                sug_out.append({"email": u["email"], "kind": sub["kind"], "text": sub["text"], "created_at": sub["created_at"]})
                if u["email"].lower() not in seen:
                    seen.add(u["email"].lower())
                    users_out.append({"email": u["email"], "name": u.get("name"), "role": u["role"]})
            finalizer_email = None
            if s["finalized_by"]:
                fu = self.q1("SELECT email, name, role FROM users WHERE uid = ?", (s["finalized_by"],))
                if real(fu):
                    finalizer_email = fu["email"]
                    if fu["email"].lower() not in seen:
                        seen.add(fu["email"].lower())
                        users_out.append({"email": fu["email"], "name": fu["name"], "role": fu["role"]})
            if not sug_out and not s["final_text"] and s["working_text"] == s["ai_text"]:
                continue  # nothing to say about this snippet - not worth including
            snippets_out.append({
                "snippet_id": s["id"], "working_text": s["working_text"], "final_text": s["final_text"],
                "finalized_by_email": finalizer_email, "finalized_at": s["finalized_at"], "suggestions": sug_out,
            })
        return {"book_id": book_id, "book_title": book["title"], "book_kavi": book["kavi"],
                "users": users_out, "snippets": snippets_out}

    def _summarize_payload(self, payload: dict) -> str:
        snippets = payload.get("snippets", [])
        finalized = sum(1 for s in snippets if s.get("final_text"))
        suggestions = sum(len(s.get("suggestions", [])) for s in snippets)
        return f"{len(snippets)} touched snippet(s), {finalized} finalized, {suggestions} suggestion(s)"

    def _save_version(self, book_id: str, source: str, payload: dict, created_by: str | None) -> dict:
        payload_json = json.dumps(payload, ensure_ascii=False)
        cur = self.conn.execute(
            "INSERT INTO book_versions (book_id, source, payload, summary, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (book_id, source, payload_json, self._summarize_payload(payload), created_by, now()),
        )
        self.conn.commit()
        return self.q1("SELECT * FROM book_versions WHERE id = ?", (cur.lastrowid,))

    def export_version(self, actor: dict, book_id: str) -> dict:
        """Snapshots the book's current review state as a new version (source='export') and returns it
        (including its payload, ready to hand straight to the caller for download - no second query)."""
        require(actor, "manage_books")
        payload = self._build_review_payload(book_id)
        return self._save_version(book_id, "export", payload, actor["uid"])

    def import_version(self, actor: dict, book_id: str, payload: dict) -> dict:
        """Stores an uploaded file as a new version (source='import') - purely additive, never touches
        live snippets/suggestions. See apply_version() for the separate, explicit step that does."""
        require(actor, "manage_books")
        if not self.q1("SELECT 1 AS x FROM books WHERE id = ?", (book_id,)):
            raise NotFound("no such book")
        if not isinstance(payload, dict) or not isinstance(payload.get("users"), list) or not isinstance(payload.get("snippets"), list):
            raise ValueError("not a valid version file - expected an object with \"users\" and \"snippets\" lists")
        return self._save_version(book_id, "import", payload, actor["uid"])

    def list_versions(self, actor: dict, book_id: str) -> list[dict]:
        require(actor, "manage_books")
        return self.q(
            "SELECT id, book_id, source, summary, created_by, created_at FROM book_versions WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        )

    def get_version(self, actor: dict, book_id: str, version_id: int) -> dict:
        require(actor, "manage_books")
        v = self.q1("SELECT * FROM book_versions WHERE id = ? AND book_id = ?", (version_id, book_id))
        if not v:
            raise NotFound("no such version")
        v["payload"] = json.loads(v["payload"])
        return v

    def apply_version(self, actor: dict, book_id: str, version_id: int) -> dict:
        """Merges a stored version's payload into live data via the exact same sync_reviews() used by
        app 1's automatic publish-time sync - same safety rules apply (never overwrites a newer
        suggestion from the same person, never un-finalizes an already-finalized snippet)."""
        require(actor, "manage_books")
        v = self.get_version(actor, book_id, version_id)
        payload = v["payload"]
        return self.sync_reviews(book_id, payload.get("users", []), payload.get("snippets", []))

    # -- tally bookkeeping ------------------------------------------------
    def _current_text(self, s: dict) -> str:
        return s["final_text"] if s["final_text"] is not None else s["working_text"]

    def snippet_tally(self, s: dict) -> dict:
        subs = self.q(
            """SELECT su.user_id, u.name, u.role, su.kind, su.text FROM suggestions su
               JOIN users u ON u.id = su.user_id WHERE su.snippet_id = ?""",
            (s["id"],),
        )
        return tallymod.tally(self._current_text(s), subs)

    def recompute(self, snippet_id: str):
        s = self.q1("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
        t = self.snippet_tally(s)
        n = self.q1("SELECT COUNT(*) AS n FROM suggestions WHERE snippet_id = ?", (snippet_id,))["n"]
        self.conn.execute(
            "UPDATE snippets SET suggestion_count = ?, attention = ?, updated_at = ? WHERE id = ?",
            (n, int(t["needs_attention"]), now(), snippet_id),
        )
        return t

    # -- suggestions ------------------------------------------------------
    def suggest(self, user: dict, snippet_id: str, kind: str, text: str | None) -> dict:
        require(user, "suggest")
        if kind not in ("confirm", "edit"):
            raise ValueError("kind must be 'confirm' or 'edit'")
        if kind == "edit" and not (text or "").strip():
            raise ValueError("an edit needs text")
        with self.lock:
            s = self.q1("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
            if not s:
                raise NotFound("no such snippet")
            self.conn.execute(
                """INSERT INTO suggestions (snippet_id, user_id, kind, text, created_at) VALUES (?,?,?,?,?)
                   ON CONFLICT(snippet_id, user_id) DO UPDATE SET kind=excluded.kind, text=excluded.text,
                     created_at=excluded.created_at""",
                (snippet_id, user["id"], kind, (text or "").strip() or None, now()),
            )
            t = self.recompute(snippet_id)
            self.conn.commit()
        return t

    # -- editor / admin actions -------------------------------------------
    def _require_finalizer(self, user):
        require(user, "approve_text")

    def accept_word_change(self, user: dict, snippet_id: str, start: int, end: int, replacement: str) -> dict:
        """Applies one word change to the working text (not final until approved)."""
        self._require_finalizer(user)
        with self.lock:
            s = self.q1("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
            if not s:
                raise NotFound("no such snippet")
            if s["final_text"] is not None:
                raise Forbidden("snippet is finalized - un-finalize it (admin) or edit the final text")
            new = tallymod.apply_changes(s["working_text"], [{"start": start, "end": end, "replacement": replacement}])
            self.conn.execute("UPDATE snippets SET working_text = ? WHERE id = ?", (new, snippet_id))
            self.log(user, "accept_word", snippet_id, {"start": start, "end": end, "replacement": replacement})
            self.recompute(snippet_id)
            self.conn.commit()
        return self.snippet_detail(snippet_id)

    def finalize(self, user: dict, snippet_id: str, text: str | None = None) -> dict:
        """text=None finalizes the current working text as-is."""
        self._require_finalizer(user)
        with self.lock:
            s = self.q1("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
            if not s:
                raise NotFound("no such snippet")
            final = (text if text is not None else self._current_text(s)).strip()
            if not final:
                raise ValueError("final text cannot be empty")
            self.conn.execute(
                "UPDATE snippets SET final_text=?, working_text=?, finalized_by=?, finalized_at=? WHERE id=?",
                (final, final, user["uid"], now(), snippet_id),
            )
            self.log(user, "finalize", snippet_id)
            self.recompute(snippet_id)
            self.conn.commit()
        return self.snippet_detail(snippet_id)

    def unfinalize(self, user: dict, snippet_id: str) -> dict:
        require(user, "reopen")
        with self.lock:
            if not self.q1("SELECT 1 AS x FROM snippets WHERE id = ?", (snippet_id,)):
                raise NotFound("no such snippet")
            self.conn.execute(
                "UPDATE snippets SET final_text=NULL, finalized_by=NULL, finalized_at=NULL WHERE id=?", (snippet_id,)
            )
            self.log(user, "unfinalize", snippet_id)
            self.recompute(snippet_id)
            self.conn.commit()
        return self.snippet_detail(snippet_id)

    def approve_page(self, user: dict, book_id: str, page_index: int, preview: bool = True) -> dict:
        """Finalizes every not-yet-final snippet on a page: each gets the word
        changes a clear majority of reviewers agreed on (see
        tally.auto_approvable_changes), else its text as it stands. preview=True
        computes and returns the plan without writing anything."""
        self._require_finalizer(user)
        with self.lock:
            rows = self.q(
                "SELECT * FROM snippets WHERE book_id=? AND page_index=? AND final_text IS NULL ORDER BY seq",
                (book_id, page_index),
            )
            if not self.q1("SELECT 1 AS x FROM snippets WHERE book_id=? AND page_index=?", (book_id, page_index)):
                raise NotFound("no such page")
            plan = []
            for s in rows:
                t = self.snippet_tally(s)
                changes = tallymod.auto_approvable_changes(t)
                new_text = tallymod.apply_changes(s["working_text"], changes) if changes else s["working_text"]
                plan.append(
                    {"snippet_id": s["id"], "seq": s["seq"], "before": s["working_text"], "after": new_text,
                     "changes_applied": len(changes)}
                )
            if not preview:
                for p in plan:
                    self.conn.execute(
                        "UPDATE snippets SET final_text=?, working_text=?, finalized_by=?, finalized_at=? WHERE id=?",
                        (p["after"], p["after"], user["uid"], now(), p["snippet_id"]),
                    )
                    self.recompute(p["snippet_id"])
                self.log(user, "approve_page", f"{book_id}:{page_index}", {"snippets": len(plan)})
                self.conn.commit()
        return {"preview": preview, "snippets": plan, "count": len(plan)}

    # -- reads ------------------------------------------------------------
    def snippet_detail(self, snippet_id: str, viewer: dict | None = None) -> dict:
        s = self.q1("SELECT * FROM snippets WHERE id = ?", (snippet_id,))
        if not s:
            raise NotFound("no such snippet")
        t = self.snippet_tally(s)
        s["bbox"] = json.loads(s["bbox"]) if s["bbox"] else None
        s["finalized"] = s["final_text"] is not None
        s["current_text"] = self._current_text(s)
        s["tally"] = t
        # total distinct people who've weighed in (confirm or edit) - already maintained by
        # recompute(), no extra query needed.
        s["reviewer_count"] = s["suggestion_count"]
        # full text of every suggested edit, not just the derived word-level diff tally - so a
        # reviewer/admin can actually read what someone else proposed, not only the fragments the
        # tally agreed on. Visible to everyone (including guests), same as the rest of this response.
        s["suggestions"] = self.q(
            """SELECT u.name, u.role, su.kind, su.text, su.created_at FROM suggestions su
               JOIN users u ON u.id = su.user_id WHERE su.snippet_id = ? AND su.kind = 'edit'
               ORDER BY su.created_at""",
            (snippet_id,),
        )
        if viewer:
            mine = self.q1("SELECT kind, text FROM suggestions WHERE snippet_id=? AND user_id=?", (snippet_id, viewer["id"]))
            s["my_suggestion"] = mine
        return s

    def dashboard(self) -> dict:
        """Public review-activity dashboard - no permission gate beyond the site-wide 'read' every
        route already needs. Deliberately a separate, lighter query set from contributor_stats()
        (which is admin-only and includes email) - this is meant to be shown to everyone."""
        # Guests and superadmins are both excluded from the leaderboards/participation below - guests
        # for the usual "not a real attributable account" reason, superadmins because they're the
        # platform owner/operator, not a community reviewer this leaderboard is meant to rank.
        NOT_RANKED = "u.role NOT IN ('guest', 'superadmin')"
        top_suggesters = self.q(
            f"""SELECT u.name, u.role, COUNT(*) AS suggestions FROM suggestions su
               JOIN users u ON u.id = su.user_id WHERE {NOT_RANKED}
               GROUP BY u.id ORDER BY suggestions DESC LIMIT 10"""
        )
        # "approved" = this reviewer's suggested wording is exactly what the snippet was finalized
        # with - not who clicked finalize (that's contributor_stats's "approvals", a different, admin-
        # facing metric about editor/admin activity).
        top_approved = self.q(
            f"""SELECT u.name, u.role, COUNT(*) AS approved FROM suggestions su
               JOIN users u ON u.id = su.user_id JOIN snippets s ON s.id = su.snippet_id
               WHERE {NOT_RANKED} AND su.kind = 'edit' AND s.final_text IS NOT NULL AND su.text = s.final_text
               GROUP BY u.id ORDER BY approved DESC LIMIT 10"""
        )
        # "reviewed" = touched by at least one suggestion or finalized - activity, not completion (the
        # books table below already answers "how much is done" via completion_pct).
        touched = self.q(
            "SELECT book_id, page_index, working_text, final_text FROM snippets WHERE suggestion_count > 0 OR final_text IS NOT NULL"
        )
        words_reviewed = sum(len((r["final_text"] or r["working_text"] or "").split()) for r in touched)
        trends = {
            "books_reviewed": len({r["book_id"] for r in touched}),
            "pages_reviewed": len({(r["book_id"], r["page_index"]) for r in touched}),
            "words_reviewed": words_reviewed,
        }
        participation = self.q(
            f"""SELECT s.book_id, b.title, COUNT(DISTINCT su.user_id) AS participants FROM suggestions su
               JOIN snippets s ON s.id = su.snippet_id JOIN users u ON u.id = su.user_id
               JOIN books b ON b.id = s.book_id WHERE {NOT_RANKED}
               GROUP BY s.book_id ORDER BY participants DESC"""
        )
        books = self.library()
        return {
            "top_suggesters": top_suggesters,
            "top_approved": top_approved,
            "trends": trends,
            "participation": participation,
            "books": books,
            "pending_admin_review": [b for b in books if b["needs_attention"] > 0],
        }

    def library(self, include_hidden: bool = False) -> list[dict]:
        rows = self.q(
            f"""SELECT b.*, COUNT(s.id) AS total,
                 COALESCE(SUM(CASE WHEN s.final_text IS NOT NULL THEN 1 ELSE 0 END),0) AS finalized,
                 COALESCE(SUM(CASE WHEN s.final_text IS NULL AND s.suggestion_count > 0 THEN 1 ELSE 0 END),0) AS awaiting_approval,
                 COALESCE(SUM(CASE WHEN s.final_text IS NULL AND s.suggestion_count = 0 THEN 1 ELSE 0 END),0) AS untouched,
                 COALESCE(SUM(CASE WHEN s.final_text IS NULL AND s.attention = 1 THEN 1 ELSE 0 END),0) AS needs_attention,
                 COALESCE(SUM(CASE WHEN s.final_text IS NOT NULL AND s.attention = 1 THEN 1 ELSE 0 END),0) AS finalized_challenged
               FROM books b LEFT JOIN snippets s ON s.book_id = b.id
               {'' if include_hidden else 'WHERE b.hidden = 0'} GROUP BY b.id ORDER BY b.id"""
        )
        for r in rows:
            r["pending"] = r["total"] - r["finalized"]
            r["completion_pct"] = round(100.0 * r["finalized"] / r["total"], 1) if r["total"] else 0.0
        return rows

    def book(self, book_id: str) -> dict:
        b = next((r for r in self.library(include_hidden=True) if r["id"] == book_id), None)
        if not b:
            raise NotFound("no such book")
        return b

    def pages(self, book_id: str, page: int = 1, per_page: int = 20) -> dict:
        self.book(book_id)
        rows = self.q(
            """SELECT page_index, MIN(page_number) AS page_number, side, COUNT(*) AS total,
                 SUM(CASE WHEN final_text IS NOT NULL THEN 1 ELSE 0 END) AS finalized,
                 SUM(CASE WHEN final_text IS NULL AND attention = 1 THEN 1 ELSE 0 END) AS needs_attention
               FROM snippets WHERE book_id = ? GROUP BY page_index, side ORDER BY page_index""",
            (book_id,),
        )
        total = len(rows)
        per_page = max(1, min(per_page, 100))
        start = (max(1, page) - 1) * per_page
        return {"total_pages": total, "page": page, "per_page": per_page, "pages": rows[start:start + per_page]}

    def page_detail(self, book_id: str, page_index: int, viewer: dict | None = None) -> dict:
        rows = self.q("SELECT id FROM snippets WHERE book_id=? AND page_index=? ORDER BY seq", (book_id, page_index))
        if not rows:
            raise NotFound("no such page")
        snippets = [self.snippet_detail(r["id"], viewer) for r in rows]
        total_pages = self.q1("SELECT MAX(page_index) AS m FROM snippets WHERE book_id=?", (book_id,))["m"]
        first = snippets[0]
        return {
            "book_id": book_id,
            "page_index": page_index,
            "page_number": first["page_number"],
            "side": first["side"],
            "page_image_url": first["page_image_url"],
            "total_pages": total_pages,
            "prev": page_index - 1 if page_index > 1 else None,
            "next": page_index + 1 if page_index < total_pages else None,
            "snippets": snippets,
        }

    def read_pages(self, book_id: str, start: int = 1, count: int = 5) -> dict:
        """Several consecutive pages' text in one light request, for the
        full-text reading view (no images/tallies - just enough to show
        each paragraph's text and whether it is finalized / has activity)."""
        self.book(book_id)
        count = max(1, min(count, 20))
        total = self.q1("SELECT MAX(page_index) AS m FROM snippets WHERE book_id=?", (book_id,))["m"] or 0
        rows = self.q(
            """SELECT id, page_index, page_number, side, seq, working_text, final_text, suggestion_count, attention
               FROM snippets WHERE book_id=? AND page_index BETWEEN ? AND ? ORDER BY page_index, seq""",
            (book_id, start, start + count - 1),
        )
        pages = {}
        for r in rows:
            p = pages.setdefault(
                r["page_index"],
                {"page_index": r["page_index"], "page_number": r["page_number"], "side": r["side"], "snippets": []},
            )
            p["snippets"].append(
                {
                    "id": r["id"],
                    "seq": r["seq"],
                    "text": self._current_text(r),
                    "finalized": r["final_text"] is not None,
                    "suggestion_count": r["suggestion_count"],
                    "attention": bool(r["attention"]),
                }
            )
        return {"total_pages": total, "start": start, "count": count, "pages": list(pages.values())}

    def book_text(self, book_id: str, use: str = "current") -> str:
        """The whole book as plain text, page by page. use='final' returns only
        finalized text (unfinalized snippets omitted); 'current' uses final
        text where it exists and the working text elsewhere."""
        self.book(book_id)
        out, last_page = [], None
        for s in self.q("SELECT * FROM snippets WHERE book_id=? ORDER BY page_index, seq", (book_id,)):
            if use == "final" and s["final_text"] is None:
                continue
            if s["page_index"] != last_page:
                out.append("")
                last_page = s["page_index"]
            out.append(self._current_text(s))
        return "\n".join(out).strip() + "\n"
