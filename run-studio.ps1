$ErrorActionPreference = "Stop"
$backend = "G:\paokra"
$frontend = "G:\paokra\obs-web"
$backendUp = [bool](Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8797 -State Listen -ErrorAction SilentlyContinue)
$frontendUp = [bool](Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue)

if (-not $backendUp) {
  Start-Process powershell -ArgumentList "-NoExit","-Command","Set-Location '$backend'; py -3 .\app\server.py"
}

if (-not $frontendUp) {
  Start-Process powershell -ArgumentList "-NoExit","-Command","Set-Location '$frontend'; npm run dev -- --host 127.0.0.1 --port 4002"
}

Start-Sleep -Seconds 3
Start-Process "http://127.0.0.1:4002/"
