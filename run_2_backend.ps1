<#
.SYNOPSIS
    App 2 of 3: the backend (review API). Stores the review database, roles and image
    URLs; this is what app 1 publishes to and app 3 reads from.

.DESCRIPTION
    Locally: SQLite + a local image folder under review_api_data/ (git-ignored).
    In production: the same app, hosted on PythonAnywhere, with STORAGE_BACKEND=supabase
    in .env so images go to Supabase instead of a local folder.

    Thin wrapper around run_api.ps1. Once running, open http://127.0.0.1:8200 .

.EXAMPLE
    .\run_2_backend.ps1
#>
param([int]$Port = 8200)
& (Join-Path $PSScriptRoot "run_api.ps1") -Port $Port
exit $LASTEXITCODE
