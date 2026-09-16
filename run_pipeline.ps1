<#
.SYNOPSIS
    Runs the Lipi-Sampada OCR + AI-refiner pipeline.

.DESCRIPTION
    Thin wrapper around `python -m lipisampada.pipeline` that sets up
    PYTHONPATH/PYTHONUTF8 and uses the project's .venv. All arguments are
    forwarded as-is to pipeline.py's argparse CLI.

.EXAMPLE
    .\run_pipeline.ps1 --input Input_Prasanga --pages 5

.EXAMPLE
    .\run_pipeline.ps1 --input Input_Prasanga --output output\full_run --no-refine

.EXAMPLE
    .\run_pipeline.ps1 --help
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

& $python -m lipisampada.pipeline @args
exit $LASTEXITCODE
