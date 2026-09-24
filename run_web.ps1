<#
.SYNOPSIS
    Serves the review web app (web/) locally at http://127.0.0.1:8300 .
    Start run_api.ps1 first. In production the same web/ folder is what you
    deploy to Firebase Hosting - it is plain static files, no build step.
#>
param([int]$Port = 8300)
$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m http.server $Port --bind 127.0.0.1 --directory (Join-Path $PSScriptRoot "web")
