#!/usr/bin/env bash
# Runs the Lipi-Sampada Phase 3 active learning tooling (retraining
# trigger check, JSON-pair export, tesstrain data staging).
#
# Thin wrapper around `python -m lipisampada.active_learning` that sets
# up PYTHONPATH/PYTHONUTF8 and uses the project's .venv. All arguments are
# forwarded as-is to active_learning.py's argparse CLI.
#
# Examples:
#   ./run_active_learning.sh
#   ./run_active_learning.sh --export-jsonl corrections_export.jsonl
#   ./run_active_learning.sh --prepare-tesstrain tesstrain_data --lang kan
#   ./run_active_learning.sh --help

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$root/.venv/Scripts/python.exe"

if [ ! -f "$python_bin" ]; then
    echo "Virtualenv not found at $python_bin. Run: python -m venv .venv && .venv/Scripts/pip install -r requirements.txt" >&2
    exit 1
fi

export PYTHONPATH="$root/src"
export PYTHONUTF8=1

exec "$python_bin" -m lipisampada.active_learning "$@"
