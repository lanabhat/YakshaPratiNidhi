import pytest


@pytest.fixture(autouse=True)
def _sane_storage_defaults(monkeypatch):
    """The real .env in this checkout (used for actually publishing from App 1) points
    STORAGE_BACKEND at production Supabase credentials. Without this, every test that builds the
    review API's Flask app would construct a real SupabaseStorage client from those credentials -
    harmless unless a delete/upload route is ever exercised with a URL that happens to resolve to
    a real object key, which is a risk worth closing off entirely for automated tests. A test that
    needs different behavior can still monkeypatch these itself; that later call simply wins."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:8200")
