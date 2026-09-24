"""Review platform API (Flask - runs as-is on PythonAnywhere's WSGI hosting).

Run locally:  .\\run_api.ps1   ->  http://127.0.0.1:8200
"""

import os
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from lipisampada import config
from lipisampada.reviewapi import auth, db as dbmod, permissions


def create_app(db_path: str | Path | None = None, local_storage_dir: str | Path | None = None) -> Flask:
    config.load_env()
    app = Flask(__name__)
    CORS(app, origins=os.environ.get("CORS_ORIGINS", "*").split(","))

    data_dir = Path(os.environ.get("REVIEW_API_DATA", config.PROJECT_ROOT / "review_api_data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    database = dbmod.Db(db_path or data_dir / "review.sqlite3")
    files_dir = Path(local_storage_dir or os.environ.get("LOCAL_STORAGE_DIR", data_dir / "files"))
    files_dir.mkdir(parents=True, exist_ok=True)
    app.config["DB"] = database

    @app.errorhandler(dbmod.Forbidden)
    def _forbidden(e):
        return jsonify(error=str(e)), 403

    @app.errorhandler(dbmod.NotFound)
    def _notfound(e):
        return jsonify(error=str(e)), 404

    @app.errorhandler(ValueError)
    def _badrequest(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(auth.AuthError)
    def _autherr(e):
        return jsonify(error=str(e)), 401

    @app.errorhandler(429)
    def _too_many(e):
        return jsonify(error="too many requests - please slow down and try again shortly"), 429

    @app.before_request
    def _identify():
        g.user = None
        if request.path.startswith("/files/") or request.path == "/api/health" or request.path.startswith("/api/ingest"):
            return
        g.user = auth.current_user(database, request.headers)
        if request.path != "/api/me":  # a banned/gated person still needs to learn *why* via /api/me
            permissions.require(g.user, "read")

    def _rate_key():
        if g.get("user"):
            return g.user["uid"]
        guest = request.headers.get("X-Guest-Id", "").strip()
        return guest or get_remote_address()

    # In-memory (single process) - matches this app's single-SQLite-writer scale; a real bot-farm
    # needs more than this, but it stops one runaway script or a naive scraper. Bans and the
    # application gate are the real backstop against a *person* misusing an account, not a rate limit.
    limiter = Limiter(key_func=_rate_key, app=app, default_limits=["300 per minute"], storage_uri="memory://")

    def need_user():
        if g.user is None:
            raise auth.AuthError("sign in, or send an X-Guest-Id header to act as a guest")
        return g.user

    def body():
        return request.get_json(silent=True) or {}

    def visible(book_id):
        """Hidden books look like they do not exist, except to people who manage books."""
        if database.is_hidden(book_id) and not permissions.can(g.user, "manage_books"):
            raise dbmod.NotFound("no such book")
        return book_id

    def visible_snippet(snippet_id):
        visible(snippet_id.split(":", 1)[0])
        return snippet_id

    # -- basics ---------------------------------------------------------------
    @app.get("/")
    def index():
        # A browser pointed at the API root should learn what this is, not see a bare 404.
        return jsonify(
            service="Lipi-Sampada review API",
            note="This is the API, not the website. Open the web app (run_web.ps1 -> http://127.0.0.1:8300).",
            try_these=["/api/health", "/api/library"],
        )

    @app.get("/api/health")
    def health():
        return jsonify(ok=True)

    @app.get("/api/me")
    def me():
        u = g.user
        if u is None:
            return jsonify(user=None, permissions=permissions.permissions_for(None))
        user = {k: u[k] for k in ("uid", "email", "name", "role")}
        user["active"] = bool(u.get("active", 1))
        user["application_status"] = u.get("application_status", "unset")
        user["banned"] = bool(u.get("banned"))
        user["ban_reason"] = u.get("ban_reason")
        return jsonify(user=user, permissions=permissions.permissions_for(u))

    @app.get("/files/<path:key>")
    @limiter.exempt  # page/snippet images: a single page view can request dozens of these
    def files(key):
        if ".." in key.replace("\\", "/").split("/"):
            abort(404)
        # explicit types: Windows' registry-based mimetypes doesn't know .webp
        types = {".webp": "image/webp", ".gz": "application/gzip", ".json": "application/json"}
        return send_from_directory(files_dir, key, mimetype=types.get(Path(key).suffix.lower()))

    # -- library / reading ----------------------------------------------------
    @app.get("/api/library")
    def library():
        return jsonify(books=database.library(include_hidden=permissions.can(g.user, "manage_books")))

    @app.get("/api/books/<book_id>")
    def book(book_id):
        return jsonify(book=database.book(visible(book_id)))

    @app.get("/api/books/<book_id>/pages")
    def pages(book_id):
        return jsonify(database.pages(visible(book_id), int(request.args.get("page", 1)), int(request.args.get("per_page", 20))))

    @app.get("/api/books/<book_id>/pages/<int:page_index>")
    def page(book_id, page_index):
        return jsonify(database.page_detail(visible(book_id), page_index, g.user))

    @app.get("/api/books/<book_id>/read")
    def read(book_id):
        return jsonify(database.read_pages(visible(book_id), int(request.args.get("start", 1)), int(request.args.get("count", 5))))

    @app.get("/api/books/<book_id>/text")
    def text(book_id):
        use = request.args.get("use", "current")
        if use not in ("current", "final"):
            raise ValueError("use must be 'current' or 'final'")
        return app.response_class(database.book_text(visible(book_id), use), mimetype="text/plain; charset=utf-8")

    @app.get("/api/snippet")
    def snippet():
        return jsonify(database.snippet_detail(visible_snippet(request.args["id"]), g.user))

    # -- anyone signed in or guest: apply to volunteer, suggest -----------------
    @app.post("/api/apply")
    @limiter.limit("5 per hour")  # this is a one-time form, not something a real person resubmits often
    def apply_to_volunteer():
        b = body()
        u = database.apply(need_user(), b.get("name", ""), b.get("place", ""), bool(b.get("wants_to_volunteer")), b.get("extra_info", ""))
        return jsonify(user={k: u[k] for k in ("uid", "email", "name", "role", "application_status")},
                       permissions=permissions.permissions_for(u))

    @app.post("/api/suggest")
    @limiter.limit("60 per minute")
    def suggest():
        b = body()
        t = database.suggest(need_user(), visible_snippet(b.get("snippet_id", "")), b.get("kind", "edit"), b.get("text"))
        return jsonify(tally=t)

    # -- editors / admins: approve ---------------------------------------------
    @app.post("/api/accept-word")
    @limiter.limit("60 per minute")
    def accept_word():
        b = body()
        s = database.accept_word_change(
            need_user(), b["snippet_id"], int(b["start"]), int(b["end"]), b.get("replacement", "")
        )
        return jsonify(snippet=s)

    @app.post("/api/finalize")
    @limiter.limit("60 per minute")
    def finalize():
        b = body()
        return jsonify(snippet=database.finalize(need_user(), b["snippet_id"], b.get("text")))

    @app.post("/api/unfinalize")
    def unfinalize():
        return jsonify(snippet=database.unfinalize(need_user(), body()["snippet_id"]))

    @app.post("/api/books/<book_id>/pages/<int:page_index>/approve")
    def approve_page(book_id, page_index):
        preview = bool(body().get("preview", True))
        return jsonify(database.approve_page(need_user(), book_id, page_index, preview))

    # -- admin ------------------------------------------------------------------
    @app.get("/api/admin/permissions")
    def admin_permissions():
        permissions.require(need_user(), "manage_users")
        return jsonify(permissions.matrix())

    @app.get("/api/admin/users")
    def users():
        permissions.require(need_user(), "manage_users")
        return jsonify(users=database.list_users(request.args.get("q", ""), request.args.get("role", "")))

    @app.post("/api/admin/users/<path:uid>/role")
    def set_role(uid):
        database.set_role(need_user(), uid, body().get("role", ""))
        return jsonify(ok=True)

    @app.post("/api/admin/users/<path:uid>/active")
    def set_active(uid):
        database.set_active(need_user(), uid, bool(body().get("active", True)))
        return jsonify(ok=True)

    @app.post("/api/admin/users/<path:uid>/ban")
    def ban_user(uid):
        b = body()
        database.set_banned(need_user(), uid, bool(b.get("banned", True)), b.get("reason"))
        return jsonify(ok=True)

    @app.get("/api/admin/applications")
    def admin_applications():
        return jsonify(applications=database.list_applications(need_user()))

    @app.post("/api/admin/applications/<path:uid>/decide")
    def decide_application(uid):
        database.decide_application(need_user(), uid, bool(body().get("approve", True)))
        return jsonify(ok=True)

    @app.get("/api/admin/invites")
    def invites():
        return jsonify(invites=database.list_invites(need_user()))

    @app.post("/api/admin/invites")
    def add_invite():
        b = body()
        database.create_invite(need_user(), b.get("email", ""), b.get("role", "reviewer"))
        return jsonify(ok=True)

    @app.delete("/api/admin/invites")
    def remove_invite():
        database.delete_invite(need_user(), request.args.get("email", ""))
        return jsonify(ok=True)

    @app.get("/api/admin/stats")
    def stats():
        return jsonify(database.contributor_stats(need_user(), request.args.get("book") or None))

    @app.get("/api/admin/books")
    def admin_books():
        permissions.require(need_user(), "manage_books")
        return jsonify(books=database.library(include_hidden=True))

    @app.post("/api/admin/books/<book_id>/hidden")
    def hide_book(book_id):
        database.set_hidden(need_user(), book_id, bool(body().get("hidden", True)))
        return jsonify(ok=True)

    # -- ingest (called by the local intake app, never by browsers) -----------
    def _ingest_key_error():
        """None if the request's ingest key is valid, else a (jsonify(...), status) to return as-is."""
        expected = os.environ.get("INGEST_API_KEY")
        if not expected:
            return jsonify(error="ingest disabled: INGEST_API_KEY not set on the server"), 503
        if request.headers.get("X-Ingest-Key") != expected:
            return jsonify(error="bad ingest key"), 401
        return None

    @app.post("/api/ingest/book")
    @limiter.exempt
    def ingest():
        err = _ingest_key_error()
        if err:
            return err
        b = body()
        return jsonify(database.ingest_book(b["book"], b["snippets"]))

    @app.post("/api/ingest/reviews")
    @limiter.exempt
    def ingest_reviews():
        """Merges suggestions/finalized text made against someone's LOCAL copy of this app into this
        deployment - the review-work counterpart to /api/ingest/book's images/AI text."""
        err = _ingest_key_error()
        if err:
            return err
        b = body()
        return jsonify(database.sync_reviews(b["book_id"], b.get("users", []), b.get("snippets", [])))

    return app


def wsgi():  # entry point for `flask --app lipisampada.reviewapi.app:wsgi run` / PythonAnywhere
    return create_app()
