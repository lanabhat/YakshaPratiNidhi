"""Tiny .env loader (no dependency): KEY=VALUE lines from <project>/.env are put
into os.environ without overriding anything already set. Secrets (Supabase
service key, ingest API key) live only in that git-ignored file."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_env(path: Path | None = None) -> None:
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
