"""Where images and per-book bundles are stored. One small interface, two
backends: a local folder (development, no accounts needed) and Supabase
Storage (production). The publisher and API only ever see put_bytes -> URL."""

import os
from pathlib import Path

import requests

from lipisampada import config


class LocalStorage:
    """Files under root, served by the API at <base_url>/files/<key>."""

    def __init__(self, root: str | Path, base_url: str):
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        # Deterministic traversal guard (resolve() can return different Windows
        # path forms while directories are being created concurrently).
        parts = key.replace("\\", "/").split("/")
        if not key or key.startswith(("/", "\\")) or ":" in parts[0] or ".." in parts:
            raise ValueError("bad storage key")
        return self.root.joinpath(*parts)

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/files/{key}"

    def exists(self, key: str) -> bool:
        return self.path_for(key).exists()

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        p = self.path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return self.url_for(key)


class SupabaseStorage:
    """Supabase Storage via its REST API, public bucket. NOTE: written to the
    documented API but not yet exercised against a real project - verify with
    a first small upload once credentials exist (see README)."""

    def __init__(self, url: str, service_key: str, bucket: str):
        self.url = url.rstrip("/")
        self.bucket = bucket
        self.headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}

    def url_for(self, key: str) -> str:
        return f"{self.url}/storage/v1/object/public/{self.bucket}/{key}"

    def exists(self, key: str) -> bool:
        return requests.head(self.url_for(key), timeout=30).status_code == 200

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        r = requests.post(
            f"{self.url}/storage/v1/object/{self.bucket}/{key}",
            data=data,
            headers={**self.headers, "Content-Type": content_type, "x-upsert": "true"},
            timeout=120,
        )
        r.raise_for_status()
        return self.url_for(key)


def from_env(api_base_url: str | None = None):
    backend = os.environ.get("STORAGE_BACKEND", "local")
    if backend == "supabase":
        return SupabaseStorage(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"], os.environ.get("SUPABASE_BUCKET", "lipisampada")
        )
    root = os.environ.get("LOCAL_STORAGE_DIR", config.PROJECT_ROOT / "review_api_data" / "files")
    return LocalStorage(root, api_base_url or os.environ.get("API_BASE_URL", "http://127.0.0.1:8200"))
