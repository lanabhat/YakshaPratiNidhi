#!/usr/bin/env bash
# Runs the Lipi-Sampada OCR + AI-refiner pipeline.
#
# Thin wrapper around `python -m lipisampada.pipeline` that sets up
# PYTHONPATH/PYTHONUTF8 and uses the project's .venv. All arguments are
# forwarded as-is to pipeline.py's argparse CLI.
#
# Examples:
#   ./run_pipeline.sh --input Input_Prasanga --pages 5
#   ./run_pipeline.sh --input Input_Prasanga --output output/full_run --no-refine
#   ./run_pipeline.sh --help

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$root/.venv/Scripts/python.exe"

if [ ! -f "$python_bin" ]; then
    echo "Virtualenv not found at $python_bin. Run: python -m venv .venv && .venv/Scripts/pip install -r requirements.txt" >&2
    exit 1
fi

export PYTHONPATH="$root/src"
export PYTHONUTF8=1

exec "$python_bin" -m lipisampada.pipeline "$@"
