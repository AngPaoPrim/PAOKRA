$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
Set-Location $ProjectRoot

# Clean only stale processes whose command line points to this exact project folder.
# Do not stop unrelated Electron apps or another PAOKRA copy.
$serverNeedle = (Join-Path $ProjectRoot "app\server.py").ToLowerInvariant()
$mainNeedle = (Join-Path $ProjectRoot "desktop\main.cjs").ToLowerInvariant()
$launcherNeedle = (Join-Path $ProjectRoot "start-desktop.ps1").ToLowerInvariant()
$electronExePrefix = (Join-Path $ProjectRoot "desktop\node_modules\electron\").ToLowerInvariant()
$me = $PID
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object {
    $_.ProcessId -ne $me -and $_.CommandLine -and (
      ($_.CommandLine.ToLowerInvariant().Contains($serverNeedle)) -or
      ($_.CommandLine.ToLowerInvariant().Contains($mainNeedle)) -or
      ($_.CommandLine.ToLowerInvariant().Contains($launcherNeedle)) -or
      ($_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($electronExePrefix))
    )
  } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }

if (-not (Test-Path (Join-Path $ProjectRoot "desktop\node_modules\electron\dist\electron.exe"))) {
  Write-Host "Installing PAOKRA embedded browser engine..."
  Push-Location (Join-Path $ProjectRoot "desktop")
  try { npm install --no-audit --no-fund } finally { Pop-Location }
}

$env:PAOKRA_EXTERNAL_SERVER = "0"
Push-Location (Join-Path $ProjectRoot "desktop")
try {
  Write-Host "Starting PAOKRA Desktop from $ProjectRoot ..."
  npm start
} finally {
  Pop-Location
}
