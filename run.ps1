$ErrorActionPreference="Stop"
Set-Location D:\paokra
Write-Host "PAOKRA Studio -> http://127.0.0.1:8797"
py -3 .\app\server.py
