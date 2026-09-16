<#
.SYNOPSIS
    Starts the Lipi-Sampada Phase 2 review web app.

.DESCRIPTION
    Thin wrapper around `uvicorn lipisampada.review_app:app` that sets up
    PYTHONPATH/PYTHONUTF8 and uses the project's .venv. Optionally takes a
    run directory as the first argument (sets LIPISAMPADA_RUN_DIR); without
    it, review_app.py defaults to the newest run under output/. Any
    remaining arguments are forwarded to uvicorn.

    Once running, open http://127.0.0.1:8000 in a browser.

.EXAMPLE
    .\run_review.ps1
    # reviews the newest run under output/

.EXAMPLE
    .\run_review.ps1 output\poc_5pages_refined
    # reviews a specific run

.EXAMPLE
    .\run_review.ps1 output\poc_5pages_refined --port 8080
    # extra args forwarded to uvicorn
#>

param(
    [string]$RunDir,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "Virtualenv not found at $python. Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUTF8 = "1"
if ($RunDir) {
    $env:LIPISAMPADA_RUN_DIR = $RunDir
}

& $python -m uvicorn lipisampada.review_app:app --reload @ExtraArgs
exit $LASTEXITCODE
