"""Memory sampler for long OCR runs: every INTERVAL seconds appends one CSV row to
logs/memory_log.csv so growth (a leak) shows up as a trend instead of a guess.

Columns: time, this app's resident MB and committed MB, resident+committed MB of every
llama-server (Surya's and Ollama's), system RAM free, system commit free (the number that hit zero
before), and a caller-supplied note (e.g. the OCR page being processed)."""

import csv
import threading
import time
from pathlib import Path

import psutil

INTERVAL = 30
MB = 1024 * 1024


def sample(note: str = "") -> list:
    me = psutil.Process()
    mi = me.memory_info()
    llama_rss = llama_commit = 0
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            if (p.info["name"] or "").lower().startswith("llama-server"):
                llama_rss += p.info["memory_info"].rss
                llama_commit += getattr(p.info["memory_info"], "private", p.info["memory_info"].vms)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    commit_free = (vm.available + (sw.total - sw.used)) // MB  # approximates the commit headroom
    return [time.strftime("%Y-%m-%d %H:%M:%S"), mi.rss // MB, getattr(mi, "private", mi.vms) // MB,
            llama_rss // MB, llama_commit // MB, vm.available // MB, commit_free, note]


def start(path: Path, note_fn=lambda: "", interval: int = INTERVAL) -> threading.Thread:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()

    def loop():
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "app_rss_mb", "app_commit_mb", "llama_rss_mb", "llama_commit_mb",
                            "ram_free_mb", "commit_free_mb", "note"])
            while True:
                try:
                    w.writerow(sample(note_fn()))
                    f.flush()
                except Exception:
                    pass  # a failed sample must never disturb the app
                time.sleep(interval)

    t = threading.Thread(target=loop, daemon=True, name="memlog")
    t.start()
    return t
