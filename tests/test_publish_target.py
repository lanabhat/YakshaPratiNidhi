from lipisampada import intake_queue_db as qdb
from test_page_editor_api import client, env, make_item  # noqa: F401  (fixtures + helpers)


def mark_done(env, item_id):
    env[0]._queue_conn.execute("UPDATE queue_items SET status='done' WHERE id=?", (item_id,))
    env[0]._queue_conn.commit()


def test_publish_target_label_shows_host_and_storage_backend(env):
    mod, _ = env
    assert mod._publish_target_label("https://yakshapratinidhi.pythonanywhere.com", "supabase") == \
        "yakshapratinidhi.pythonanywhere.com + supabase storage"
    assert mod._publish_target_label("http://127.0.0.1:8200", "local") == "127.0.0.1:8200 + local storage"


def test_local_publish_only_needs_the_ingest_key_not_api_base_url(env, monkeypatch):
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("INGEST_API_KEY", raising=False)
    mod, _ = env
    assert not mod._local_publish_configured() and not mod._web_publish_configured()

    monkeypatch.setenv("INGEST_API_KEY", "k")
    assert mod._local_publish_configured()  # local's target is hardcoded, no API_BASE_URL needed
    assert not mod._web_publish_configured()  # web still needs API_BASE_URL too

    monkeypatch.setenv("API_BASE_URL", "https://yakshapratinidhi.pythonanywhere.com")
    assert mod._web_publish_configured()


def test_queue_response_reports_the_currently_configured_target_per_stage(client, env, monkeypatch):
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("INGEST_API_KEY", raising=False)
    body = client.get("/api/queue").json()
    assert body["configured_local_target"] is None and body["configured_web_target"] is None

    monkeypatch.setenv("INGEST_API_KEY", "k")
    body = client.get("/api/queue").json()
    assert body["configured_local_target"] == "127.0.0.1:8200 + local storage"
    assert body["configured_web_target"] is None  # still no API_BASE_URL

    monkeypatch.setenv("API_BASE_URL", "https://yakshapratinidhi.pythonanywhere.com")
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    body = client.get("/api/queue").json()
    assert body["configured_web_target"] == "yakshapratinidhi.pythonanywhere.com + supabase storage"
    assert body["configured_local_target"] == "127.0.0.1:8200 + local storage"  # unaffected by web's config


def test_item_keeps_its_last_real_publish_target_per_stage_even_if_env_changes_later(client, env):
    mod, _ = env
    item, _, _ = make_item(env, 1, "target_book")
    qdb.set_publish(mod._queue_conn, item, "web", "published", target="yakshapratinidhi.pythonanywhere.com + supabase storage")
    qdb.set_publish(mod._queue_conn, item, "local", "published", target="127.0.0.1:8200 + local storage")
    row = client.get(f"/api/queue/{item}").json()["item"]
    assert row["web_publish_target"] == "yakshapratinidhi.pythonanywhere.com + supabase storage"
    assert row["local_publish_target"] == "127.0.0.1:8200 + local storage"

    # a later call that doesn't know a target (e.g. the "skipped: not configured" path) must not erase it,
    # and must not touch the *other* stage's columns at all
    qdb.set_publish(mod._queue_conn, item, "web", "skipped", "API_BASE_URL / INGEST_API_KEY not set in .env")
    row = client.get(f"/api/queue/{item}").json()["item"]
    assert row["web_publish_status"] == "skipped"
    assert row["web_publish_target"] == "yakshapratinidhi.pythonanywhere.com + supabase storage"
    assert row["local_publish_status"] == "published"
    assert row["local_publish_target"] == "127.0.0.1:8200 + local storage"


def test_next_unpublished_local_picks_up_a_freshly_done_item_but_not_a_settled_one(client, env):
    mod, _ = env
    item, _, _ = make_item(env, 1, "local_pickup")
    mark_done(env, item)
    conn = mod._queue_conn
    assert qdb.next_unpublished(conn, "local")["id"] == item  # NULL local_publish_status: eligible

    qdb.set_publish(conn, item, "local", "published")
    assert qdb.next_unpublished(conn, "local") is None
    qdb.set_publish(conn, item, "local", "skipped", "not configured")
    assert qdb.next_unpublished(conn, "local") is None  # waits for an explicit retry, not looped forever


def test_next_unpublished_web_only_picks_up_an_explicitly_requested_item(client, env):
    mod, _ = env
    item, _, _ = make_item(env, 1, "web_pickup")
    mark_done(env, item)
    conn = mod._queue_conn
    assert qdb.next_unpublished(conn, "web") is None  # never auto-picked-up from a fresh NULL

    qdb.set_publish(conn, item, "web", "requested")
    assert qdb.next_unpublished(conn, "web")["id"] == item
    qdb.set_publish(conn, item, "web", "published")
    assert qdb.next_unpublished(conn, "web") is None


def test_publish_progress_is_reported_and_cleared_on_the_next_status_change(client, env):
    mod, _ = env
    item, _, _ = make_item(env, 1, "progress_book")
    mark_done(env, item)
    conn = mod._queue_conn

    qdb.set_publish(conn, item, "web", "publishing")
    qdb.set_publish_progress(conn, item, "web", "uploaded 12/94 images")
    row = client.get(f"/api/queue/{item}").json()["item"]
    assert row["web_publish_progress"] == "uploaded 12/94 images"

    # a stage's progress never bleeds into the other stage's column
    assert row["local_publish_progress"] is None

    # any new set_publish() call (published, failed, or a fresh retry) clears it - never shows a stale
    # message once that attempt is over
    qdb.set_publish(conn, item, "web", "published")
    assert client.get(f"/api/queue/{item}").json()["item"]["web_publish_progress"] is None


def test_publish_local_and_publish_web_routes_set_the_right_stage_to_pick_up(client, env):
    mod, _ = env
    item, _, _ = make_item(env, 1, "route_book")
    mark_done(env, item)
    conn = mod._queue_conn
    qdb.set_publish(conn, item, "local", "published")
    qdb.set_publish(conn, item, "web", "failed", "boom")

    assert client.post(f"/api/queue/{item}/publish/local").status_code == 200
    row = qdb.get_item(conn, item)
    assert row["local_publish_status"] is None  # reset -> eligible for the worker's next tick
    assert row["web_publish_status"] == "failed"  # untouched

    assert client.post(f"/api/queue/{item}/publish/web").status_code == 200
    row = qdb.get_item(conn, item)
    assert row["web_publish_status"] == "requested"
    assert row["local_publish_status"] is None  # still untouched by the web route
