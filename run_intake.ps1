<#
.SYNOPSIS
    Starts the Lipi-Sampada PDF intake & OCR queue app.

.DESCRIPTION
    Thin wrapper around `uvicorn lipisampada.intake_app:app` that sets up
    PYTHONPATH/PYTHONUTF8 and uses the project's .venv. Browse the
    Pratisangraha catalog (db/pratisangraha.sqlite3), preview a book's PDF,
    and queue it — a background worker downloads, rasterizes, and
    auto-preps each queued book's pages; once you approve each page's
    crop/rotation/split in the browser, it's handed to the existing OCR
    pipeline automatically. Any arguments are forwarded to uvicorn.

    Once running, open http://127.0.0.1:8100 in a browser.

.EXAMPLE
    .\run_intake.ps1

.EXAMPLE
    .\run_intake.ps1 --port 8080
#>

param(
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

& $python -m uvicorn lipisampada.intake_app:app --reload --port 8100 @ExtraArgs
exit $LASTEXITCODE
