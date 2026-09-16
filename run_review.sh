#!/usr/bin/env bash
# Starts the Lipi-Sampada Phase 2 review web app.
#
# Thin wrapper around `uvicorn lipisampada.review_app:app` that sets up
# PYTHONPATH/PYTHONUTF8 and uses the project's .venv. Optionally takes a
# run directory as the first argument (sets LIPISAMPADA_RUN_DIR); without
# it, review_app.py defaults to the newest run under output/. Any
# remaining arguments are forwarded to uvicorn.
#
# Once running, open http://127.0.0.1:8000 in a browser.
#
# Examples:
#   ./run_review.sh                                        # newest run under output/
#   ./run_review.sh output/poc_5pages_refined               # a specific run
#   ./run_review.sh output/poc_5pages_refined --port 8080   # extra args forwarded to uvicorn

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$root/.venv/Scripts/python.exe"

if [ ! -f "$python_bin" ]; then
    echo "Virtualenv not found at $python_bin. Run: python -m venv .venv && .venv/Scripts/pip install -r requirements.txt" >&2
    exit 1
fi

export PYTHONPATH="$root/src"
export PYTHONUTF8=1

if [ $# -gt 0 ] && [[ "$1" != --* ]]; then
    export LIPISAMPADA_RUN_DIR="$1"
    shift
fi

exec "$python_bin" -m uvicorn lipisampada.review_app:app --reload "$@"
