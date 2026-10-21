param([switch]$ForceRuntimeBuild)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
Write-Host "=== PAOKRA Installer Build ===" -ForegroundColor Cyan
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python launcher (py) is required only on the BUILD machine." }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Node.js/npm is required only on the BUILD machine." }
if (-not (Get-Command npx -ErrorAction SilentlyContinue)) { throw "npx is required only on the BUILD machine." }
py -3 -m pip install --disable-pip-version-check pyinstaller
New-Item -ItemType Directory -Force build\runtime | Out-Null
Remove-Item build\server-build,build\server-dist,build\runtime\ms-playwright -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force build\runtime\ms-playwright | Out-Null
if ($ForceRuntimeBuild -or -not (Test-Path build\runtime\paokra-server.exe)) {
  Write-Host "Building fresh PAOKRA backend runtime..." -ForegroundColor Cyan
  py -3 -m PyInstaller --noconfirm --clean --onefile --name paokra-server --distpath build\server-dist --workpath build\server-build --specpath build --collect-all pyvirtualcam --collect-binaries sounddevice --hidden-import=sounddevice --collect-all playwright --collect-all cv2 --hidden-import=playwright.sync_api app\server.py
  Copy-Item build\server-dist\paokra-server.exe build\runtime\paokra-server.exe -Force
} else {
  Write-Host "Using existing build\runtime\paokra-server.exe" -ForegroundColor DarkGray
}
$pw = Join-Path $env:LOCALAPPDATA "ms-playwright"
$chromium = Get-ChildItem $pw -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$ffmpeg = Get-ChildItem $pw -Directory -Filter "ffmpeg-*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $chromium) { throw "Playwright Chromium is not installed on the BUILD machine." }
Copy-Item $chromium.FullName build\runtime\ms-playwright -Recurse -Force
if ($ffmpeg) { Copy-Item $ffmpeg.FullName build\runtime\ms-playwright -Recurse -Force }
Set-Location desktop
npm install --no-audit --no-fund
npm run dist
if ($LASTEXITCODE -ne 0) {
  Write-Host "Direct NSIS build hit electron-builder app-entry issue; using prepackaged fallback..." -ForegroundColor Yellow
  $u=Join-Path (Join-Path (Get-Location).Path "..dist") "win-unpackedesourcesapp"
  New-Item -ItemType Directory -Force $u | Out-Null
  Copy-Item ".main.cjs" (Join-Path $u "main.cjs") -Force
  Copy-Item ".preload.cjs" (Join-Path $u "preload.cjs") -Force
  Copy-Item ".electron-apppackage.json" (Join-Path $u "package.json") -Force
  & .
ode_modules.binelectron-builder.cmd --win nsis --prepackaged (Join-Path (Join-Path (Get-Location).Path "..dist") "win-unpacked") --projectDir (Get-Location).Path
  if ($LASTEXITCODE -ne 0) { throw "electron-builder prepackaged fallback failed with exit code $LASTEXITCODE" }
}
Write-Host "=== BUILD COMPLETE ===" -ForegroundColor Green
Get-ChildItem ..\dist\PAOKRA-Studio-Setup-*.exe | Select-Object FullName,@{N="GB";E={[math]::Round($_.Length/1GB,2)}} | Format-Table -AutoSize