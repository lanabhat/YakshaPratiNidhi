<#
.SYNOPSIS
    Starts the Lipi-Sampada review platform API (Flask) locally.

.DESCRIPTION
    Serves http://127.0.0.1:8200 . Data lives in review_api_data/ (git-ignored):
    review.sqlite3 plus, in local-storage mode, the uploaded images under files/.
    Settings come from .env (see .env.example). The same app runs on
    PythonAnywhere later as the WSGI entry point lipisampada.reviewapi.app:wsgi.
#>

param(
    [int]$Port = 8200
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

& $python -c "from lipisampada.reviewapi.app import create_app; create_app().run(host='127.0.0.1', port=$Port, threaded=True)"
exit $LASTEXITCODE
