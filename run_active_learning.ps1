<#
.SYNOPSIS
    Runs the Lipi-Sampada Phase 3 active learning tooling (retraining
    trigger check, JSON-pair export, tesstrain data staging).

.DESCRIPTION
    Thin wrapper around `python -m lipisampada.active_learning` that sets
    up PYTHONPATH/PYTHONUTF8 and uses the project's .venv. All arguments
    are forwarded as-is to active_learning.py's argparse CLI.

.EXAMPLE
    .\run_active_learning.ps1

.EXAMPLE
    .\run_active_learning.ps1 --export-jsonl corrections_export.jsonl

.EXAMPLE
    .\run_active_learning.ps1 --prepare-tesstrain tesstrain_data --lang kan

.EXAMPLE
    .\run_active_learning.ps1 --help
#>

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtualenv not found at $python. Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUTF8 = "1"

& $python -m lipisampada.active_learning @args
exit $LASTEXITCODE
