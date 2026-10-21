$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot
Write-Host "========================================"
Write-Host " PAOKRA Studio"
Write-Host " Project: $ProjectRoot"
Write-Host "========================================"
& (Join-Path $ProjectRoot "start-desktop.ps1")
