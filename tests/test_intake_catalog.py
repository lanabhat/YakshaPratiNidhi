import importlib
import sqlite3
from pathlib import Path

import pytest

from lipisampada import pdf_intake

PLACEHOLDER = "(ನಿರೀಕ್ಷಿಸಿ)"  # what the catalog stores for "no kosha link yet"
DRIVE = "https://drive.google.com/file/d/{}/view?usp=sharing"

# id, entry_id, kosha_link, prati_link
ENTRIES = [
    (1, "PRS1", DRIVE.format("KOSHA1"), DRIVE.format("PRATI1")),                 # has kosha
    (2, "PRS2", None, DRIVE.format("PRATI2")),                                   # NULL kosha
    (3, "PRS3", PLACEHOLDER, "https://archive.org/details/unset0000unse_e3a1/mode/2up"),
    (4, "PRS4", PLACEHOLDER, "https://1ngo.in/media/nyt/Appe%20Anjane.pdf"),
    (5, "PRS5", None, "https://example.com/some-page"),                          # unusable prati
    (6, "PRS6", PLACEHOLDER, "https://drive.google.com/open?id=PRATI6"),
]


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("intake")
    catalog = tmp / "catalog.sqlite3"
    c = sqlite3.connect(catalog)
    c.executescript(
        """
        CREATE TABLE catalog_kavi (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE catalog_prasanga (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE catalog_publisher (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE catalog_catalogentry (
            id INTEGER PRIMARY KEY, entry_id TEXT, kosha_link TEXT, prati_link TEXT,
            publish_date_kannada TEXT, publish_date_english TEXT, view_count INTEGER,
            kavi_id INTEGER, prasanga_id INTEGER, publisher_id INTEGER);
        INSERT INTO catalog_kavi VALUES (1, 'Kavi One');
        INSERT INTO catalog_publisher VALUES (1, 'Pub One');
        """
    )
    for i, (id_, eid, kosha, prati) in enumerate(ENTRIES, 1):
        c.execute("INSERT INTO catalog_prasanga VALUES (?, ?)", (id_, f"Prasanga {id_}"))
        c.execute(
            "INSERT INTO catalog_catalogentry VALUES (?,?,?,?,?,?,?,?,?,?)",
            (id_, eid, kosha, prati, "", "2020-01-01", 0, 1, id_, 1),
        )
    c.commit()
    c.close()

    import os

    old = {k: os.environ.get(k) for k in ("INTAKE_CATALOG_DB", "INTAKE_QUEUE_DB", "INTAKE_NO_WORKER")}
    os.environ.update(INTAKE_CATALOG_DB=str(catalog), INTAKE_QUEUE_DB=str(tmp / "queue.sqlite3"), INTAKE_NO_WORKER="1")
    from lipisampada import intake_app

    mod = importlib.reload(intake_app)
    yield mod
    for k, v in old.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def ids(resp):
    return [e["id"] for e in resp.json()["entries"]]


# ---------------------------------------------------------------- filter + paging
def test_no_kosha_filter_counts_null_and_placeholder_as_missing(client):
    r = client.get("/api/catalog?kosha=none")
    assert ids(r) == [2, 3, 4, 5, 6] and r.json()["total"] == 5


def test_has_kosha_filter_only_real_urls(client):
    r = client.get("/api/catalog?kosha=has")
    assert ids(r) == [1] and r.json()["total"] == 1


def test_counts_for_all_three_filter_options(client):
    assert client.get("/api/catalog").json()["counts"] == {"any": 6, "has": 1, "none": 5}


def test_filter_combines_with_search(client):
    assert ids(client.get("/api/catalog?kosha=none&search=Prasanga 3")) == [3]
    assert ids(client.get("/api/catalog?kosha=has&search=Prasanga 3")) == []
    assert client.get("/api/catalog?kosha=none&search=Prasanga 3").json()["counts"] == {"any": 1, "has": 0, "none": 1}


def test_pagination(client):
    first = client.get("/api/catalog?kosha=none&limit=2&offset=0").json()
    second = client.get("/api/catalog?kosha=none&limit=2&offset=2").json()
    assert first["total"] == 5 and [e["id"] for e in first["entries"]] == [2, 3] and [e["id"] for e in second["entries"]] == [4, 5]


def test_bad_filter_value_rejected(client):
    assert client.get("/api/catalog?kosha=maybe").status_code == 400


def test_entry_payload_has_both_previews_and_kinds(client):
    e = {x["id"]: x for x in client.get("/api/catalog").json()["entries"]}
    assert e[1]["has_kosha"] and e[1]["kosha_preview_url"].endswith("/KOSHA1/preview") and e[1]["preview_url"].endswith("/PRATI1/preview")
    assert not e[2]["has_kosha"] and e[2]["kosha_preview_url"] is None
    assert e[3]["prati_kind"] == "archive" and e[3]["preview_url"] == "https://archive.org/embed/unset0000unse_e3a1"
    assert e[4]["prati_kind"] == "direct_pdf" and e[5]["prati_kind"] == "unsupported" and not e[5]["can_queue"]


# ---------------------------------------------------------------- queueing
def test_single_enqueue_then_duplicate_is_not_added_twice(client):
    r = client.post("/api/queue", json={"catalog_entry_id": 2})
    assert r.status_code == 200 and r.json()["outcome"] == "added"
    again = client.post("/api/queue", json={"catalog_entry_id": 2}).json()
    assert again["outcome"] == "already_queued" and again["queue_item_id"] == r.json()["queue_item_id"]
    assert sum(1 for i in client.get("/api/queue").json()["items"] if i["catalog_entry_id"] == 2) == 1


def test_catalog_reports_queue_status_for_queued_entries(client):
    e = {x["id"]: x for x in client.get("/api/catalog").json()["entries"]}
    assert e[2]["queue_status"] == "queued" and e[2]["queue_item_id"] and e[6]["queue_status"] is None


def test_bulk_enqueue_reports_each_outcome(client):
    r = client.post("/api/queue/bulk", json={"catalog_entry_ids": [2, 3, 4, 5, 999, 3]}).json()
    assert r["already_queued"] == [2]           # queued in the previous test
    assert r["added"] == [3, 4]                 # archive.org and direct-pdf both accepted; duplicate id in request ignored
    assert r["unsupported"] == [5] and r["missing"] == [999]


def test_single_enqueue_of_unsupported_link_is_a_clear_400(client):
    r = client.post("/api/queue", json={"catalog_entry_id": 5})
    assert r.status_code == 400 and "not a Drive" in r.json()["detail"]


def test_finished_book_is_not_re_added(app_module, client):
    item = client.post("/api/queue", json={"catalog_entry_id": 6}).json()["queue_item_id"]
    app_module._queue_conn.execute("UPDATE queue_items SET status='done' WHERE id=?", (item,))
    app_module._queue_conn.commit()
    assert client.post("/api/queue/bulk", json={"catalog_entry_ids": [6]}).json()["already_queued"] == [6]


# ---------------------------------------------------------------- link kinds + downloads
@pytest.mark.parametrize(
    "link,kind",
    [
        (DRIVE.format("abc"), "drive"),
        ("https://drive.google.com/open?id=abc", "drive"),
        ("https://archive.org/details/unset0000unse_e3a1/page/n3/mode/2up", "archive"),
        ("https://1ngo.in/media/nyt/x.pdf", "direct_pdf"),
        ("https://example.com/page", "unsupported"),
        (PLACEHOLDER, "unsupported"),
        (None, "unsupported"),
    ],
)
def test_source_kind(link, kind):
    assert pdf_intake.source_kind(link) == kind


def test_preview_url_per_kind():
    assert pdf_intake.preview_url(DRIVE.format("abc")) == "https://drive.google.com/file/d/abc/preview"
    assert pdf_intake.preview_url("https://archive.org/details/xyz/mode/2up") == "https://archive.org/embed/xyz"
    assert pdf_intake.preview_url("https://h.in/a.pdf") == "https://h.in/a.pdf"
    assert pdf_intake.preview_url(PLACEHOLDER) is None


class FakeResp:
    def __init__(self, body=b"", json_data=None, status=200):
        self._body, self._json, self.status_code = body, json_data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json

    def iter_content(self, chunk_size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_archive_download_resolves_pdf_name_and_writes_file(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "/metadata/" in url:
            return FakeResp(json_data={"files": [{"name": "item_djvu.txt"}, {"name": "item.pdf"}, {"name": "other.pdf"}]})
        return FakeResp(body=b"%PDF-1.4 fake pdf body")

    monkeypatch.setattr(pdf_intake.requests, "get", fake_get)
    out = pdf_intake.download_pdf("https://archive.org/details/item/mode/2up", tmp_path / "s.pdf")
    assert out.read_bytes().startswith(b"%PDF")
    assert calls == ["https://archive.org/metadata/item", "https://archive.org/download/item/item.pdf"]


def test_archive_item_without_pdf_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_intake.requests, "get", lambda url, **kw: FakeResp(json_data={"files": [{"name": "x.txt"}]}))
    with pytest.raises(RuntimeError, match="no downloadable PDF"):
        pdf_intake.download_pdf("https://archive.org/details/item", tmp_path / "s.pdf")


def test_html_error_page_is_rejected_not_rasterized(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_intake.requests, "get", lambda url, **kw: FakeResp(body=b"<html>Access denied</html>"))
    dest = tmp_path / "s.pdf"
    with pytest.raises(RuntimeError, match="did not return a PDF"):
        pdf_intake.download_pdf("https://h.in/a.pdf", dest)
    assert not dest.exists()


def test_unsupported_link_download_fails_clearly(tmp_path):
    with pytest.raises(RuntimeError, match="Unsupported link"):
        pdf_intake.download_pdf("https://example.com/page", tmp_path / "s.pdf")
