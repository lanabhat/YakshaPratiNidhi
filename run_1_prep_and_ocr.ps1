<#
.SYNOPSIS
    App 1 of 3: local prep & OCR. Browse the catalog, queue a book, crop/straighten its
    pages in the browser, then OCR runs and auto-publishes to app 2 (the backend).

.DESCRIPTION
    This is the one app that always runs on YOUR machine - it needs your GPU and Ollama,
    so it can never move to PythonAnywhere. It never talks to app 3 (the frontend)
    directly; it only pushes finished books to app 2 via API_BASE_URL/INGEST_API_KEY (.env).

    Thin wrapper around run_intake.ps1 - all arguments are forwarded as-is.
    Once running, open http://127.0.0.1:8100 .

.EXAMPLE
    .\run_1_prep_and_ocr.ps1
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs)
& (Join-Path $PSScriptRoot "run_intake.ps1") @ExtraArgs
exit $LASTEXITCODE
