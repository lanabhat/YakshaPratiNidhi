from lipisampada.reviewapi import storage as storagemod


def test_local_storage_delete_removes_the_file(tmp_path):
    s = storagemod.LocalStorage(tmp_path, "http://127.0.0.1:8200")
    s.put_bytes("books/b1/snippets/x.webp", b"data", "image/webp")
    assert s.exists("books/b1/snippets/x.webp")

    s.delete("books/b1/snippets/x.webp")

    assert not s.exists("books/b1/snippets/x.webp")


def test_local_storage_delete_of_a_missing_key_does_not_raise(tmp_path):
    s = storagemod.LocalStorage(tmp_path, "http://127.0.0.1:8200")
    s.delete("books/b1/snippets/never-existed.webp")


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_supabase_storage_delete_hits_the_object_url(monkeypatch):
    s = storagemod.SupabaseStorage("https://proj.supabase.co", "key", "lipisampada")
    calls = []

    def fake_delete(url, headers=None, timeout=None):
        calls.append((url, headers))
        return _FakeResponse(204)

    monkeypatch.setattr(storagemod.requests, "delete", fake_delete)
    s.delete("books/b1/snippets/x.webp")

    assert calls[0][0] == "https://proj.supabase.co/storage/v1/object/lipisampada/books/b1/snippets/x.webp"
    assert calls[0][1] == s.headers


def test_supabase_storage_delete_tolerates_already_gone(monkeypatch):
    s = storagemod.SupabaseStorage("https://proj.supabase.co", "key", "lipisampada")
    monkeypatch.setattr(storagemod.requests, "delete", lambda *a, **k: _FakeResponse(404))
    s.delete("books/b1/snippets/x.webp")  # should not raise


def test_supabase_storage_delete_raises_on_a_real_error(monkeypatch):
    s = storagemod.SupabaseStorage("https://proj.supabase.co", "key", "lipisampada")
    monkeypatch.setattr(storagemod.requests, "delete", lambda *a, **k: _FakeResponse(500))
    try:
        s.delete("books/b1/snippets/x.webp")
        assert False, "expected an exception"
    except RuntimeError:
        pass
