<#
.SYNOPSIS
    App 3 of 3: the frontend. The web page reviewers, editors and admins actually use.

.DESCRIPTION
    Start app 2 (run_2_backend.ps1) first - this only reads/writes through it, via
    web/config.js's API_BASE. Plain static files, no build step; in production the
    same web/ folder deploys to Firebase Hosting.

    Thin wrapper around run_web.ps1. Once running, open http://127.0.0.1:8300 .

.EXAMPLE
    .\run_3_frontend.ps1
#>
param([int]$Port = 8300)
& (Join-Path $PSScriptRoot "run_web.ps1") -Port $Port
exit $LASTEXITCODE
