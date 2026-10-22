$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Desktop = Join-Path $Root "desktop"
$Staging = Join-Path $Root "dist\win-unpacked"
$Resources = Join-Path $Staging "resources"
$AppDir = Join-Path $Resources "app"
$Runtime = Join-Path $Resources "paokra"
$BuildTemp = "C:\PAOKRA-build-20261022\installer-temp"
New-Item -ItemType Directory -Force -Path $BuildTemp | Out-Null
# Prevent overlapping builds from moving/rewriting the same NSIS archive and installer.
$BuildLockPath = Join-Path $BuildTemp "installer-build.lock"
try {
  $BuildLockHandle = [System.IO.File]::Open($BuildLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
  $BuildLockHandle.SetLength(0)
  $lockBytes = [System.Text.Encoding]::UTF8.GetBytes("PID=$PID Started=$([DateTime]::Now.ToString('o'))")
  $BuildLockHandle.Write($lockBytes, 0, $lockBytes.Length)
  $BuildLockHandle.Flush()
} catch {
  throw "Another PAOKRA installer build is already running, or build lock is unavailable: $BuildLockPath ($($_.Exception.Message))"
}
$env:TEMP = $BuildTemp
$env:TMP = $BuildTemp
$RoboCopy = Join-Path $env:SystemRoot "System32\robocopy.exe"
$Npx = Join-Path $env:ProgramFiles "nodejs\npx.cmd"
if(-not (Test-Path -LiteralPath $RoboCopy)){ throw "robocopy.exe not found: $RoboCopy" }
if(-not (Test-Path -LiteralPath $Npx)){ throw "npx.cmd not found: $Npx" }

Write-Host "=== PAOKRA Windows Installer Build ==="
$Node = Join-Path $env:ProgramFiles "nodejs\node.exe"
$UiQaScript = Join-Path $Root "qa_clipflow_timeline_ui_20261022.js"
if(-not (Test-Path -LiteralPath $Node)){ throw "node.exe not found: $Node" }
if(-not (Test-Path -LiteralPath $UiQaScript)){ throw "Clip Flow timeline QA script missing: $UiQaScript" }
Write-Host "=== PRE-BUILD CLIP FLOW TIMELINE QA ===" -ForegroundColor Cyan
& $Node --check (Join-Path $Root "app\web\app.js")
if($LASTEXITCODE -ne 0){ throw "Frontend JavaScript syntax QA failed: $LASTEXITCODE" }
& $Node $UiQaScript
$UiQaCode = $LASTEXITCODE
if($UiQaCode -ne 0){ throw "Clip Flow timeline regression QA failed ($UiQaCode). Installer build aborted." }
Write-Host "=== PRE-BUILD CLIP FLOW TIMELINE QA PASS ===" -ForegroundColor Green

# Version 1.4.6 Fix Bug Beta preflight: compile both Python runtime modules and run
# focused Make Frames concurrency, seek/start exclusion, and directed-seam checks.
$QaPythonPre = Join-Path $Root ".venv\Scripts\python.exe"
if(-not (Test-Path -LiteralPath $QaPythonPre)){ throw "Pre-build QA Python is missing: $QaPythonPre" }
Write-Host "=== PRE-BUILD v1.4.6 FIX BUG BETA SOURCE + DIRECTED SEAM MATRIX QA ===" -ForegroundColor Cyan
& $QaPythonPre -m py_compile (Join-Path $Root "app\ai_seam.py") (Join-Path $Root "app\server.py")
if($LASTEXITCODE -ne 0){ throw "Python source compile QA failed ($LASTEXITCODE)." }
$MatrixQaScripts = @(
  (Join-Path $Root ".build-temp\test_clip_flow_seam_automation.py"),
  (Join-Path $Root ".build-temp\test_random_seam_matrix_144.py"),
  (Join-Path $Root ".build-temp\test_validate_transition_matrix_144.py"),
  (Join-Path $Root "qa_clipflow_20261017\test_protocol_stop_wait_budget_20261023.py"),
  (Join-Path $Root "qa_makeframes_stability_145.py"),
  (Join-Path $Root "qa_clipflow_20261017\test_open_fps_profile_145.py")
)
foreach($qaScriptPath in $MatrixQaScripts){
  if(-not (Test-Path -LiteralPath $qaScriptPath)){ throw "Required v1.4.6 Fix Bug Beta QA script missing: $qaScriptPath" }
  & $QaPythonPre $qaScriptPath
  if($LASTEXITCODE -ne 0){ throw "Pre-build v1.4.6 Fix Bug Beta QA failed: $qaScriptPath (exit $LASTEXITCODE)." }
}
$AiSeamSourceQa = Join-Path $Root "qa_clipflow_20261017\test_ai_seam_studio_20261022.py"
& $QaPythonPre $AiSeamSourceQa --timeout 90 --skip-flow
if($LASTEXITCODE -ne 0){ throw "Source AI Seam/RIFE QA failed ($LASTEXITCODE); refusing to build installer." }
Write-Host "=== PRE-BUILD v1.4.6 FIX BUG BETA SOURCE + DIRECTED SEAM MATRIX QA PASS ===" -ForegroundColor Green

Write-Host "=== BUILD FROZEN PYTHON BACKEND FROM CURRENT SOURCES ===" -ForegroundColor Cyan
$BackendPython = Join-Path $Root ".venv\Scripts\python.exe"
$BackendPythonArgs = @()
if(-not (Test-Path -LiteralPath $BackendPython)){
  $BackendPython = (Get-Command py -ErrorAction Stop).Source
  $BackendPythonArgs = @("-3")
}
$BackendDist = Join-Path $Root "build\server-dist-v146"
$BackendWork = Join-Path $Root "build\server-build-v146"
$BackendSpec = Join-Path $Root "build\server-spec-v146"
foreach($dir in @($BackendDist,$BackendWork,$BackendSpec)){
  Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
$BackendArgs = @("-m","PyInstaller","--noconfirm","--clean","--onefile","--name","paokra-server",
  "--distpath",$BackendDist,"--workpath",$BackendWork,"--specpath",$BackendSpec,
  "--collect-all","pyvirtualcam","--collect-all","sounddevice","--collect-all","playwright",
  "--collect-all","cv2","--hidden-import=playwright.sync_api","--hidden-import=sounddevice",
  (Join-Path $Root "app\server.py"))
# Windows PowerShell 5.1 promotes native stderr lines to ErrorRecords; PyInstaller
# writes ordinary progress to stderr, so keep those lines in the build log without
# treating normal progress as a terminating PowerShell exception.
$SavedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
if($BackendPythonArgs.Count -eq 0){
  & $BackendPython @BackendArgs 2>&1
}else{
  & $BackendPython @BackendPythonArgs @BackendArgs 2>&1
}
$BackendCode = $LASTEXITCODE
$ErrorActionPreference = $SavedErrorActionPreference
if($BackendCode -ne 0){ throw "PyInstaller backend build failed ($BackendCode). Refusing to package a stale paokra-server.exe." }
$BackendCandidate = Join-Path $BackendDist "paokra-server.exe"
$BackendRuntime = Join-Path $Root "build\runtime\paokra-server.exe"
if(-not (Test-Path -LiteralPath $BackendCandidate)){ throw "Fresh backend executable not found: $BackendCandidate" }
if((Get-Item -LiteralPath $BackendCandidate).Length -lt 20MB){ throw "Fresh backend executable is unexpectedly small; refusing to package it." }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $BackendRuntime) | Out-Null
Copy-Item -LiteralPath $BackendCandidate -Destination $BackendRuntime -Force
$BackendBuiltHash = (Get-FileHash -LiteralPath $BackendCandidate -Algorithm SHA256).Hash
$BackendRuntimeHash = (Get-FileHash -LiteralPath $BackendRuntime -Algorithm SHA256).Hash
if($BackendBuiltHash -ne $BackendRuntimeHash){ throw "Backend runtime copy hash mismatch." }
Write-Host ("Fresh paokra-server.exe ready: {0:N1} MiB · SHA256 {1}" -f ((Get-Item -LiteralPath $BackendRuntime).Length/1MB),$BackendRuntimeHash) -ForegroundColor Green

$Dist = Join-Path $Root "dist"
$Old = Join-Path $Dist "old"
New-Item -ItemType Directory -Force -Path $Dist,$Old | Out-Null
Get-ChildItem -LiteralPath $Dist -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match "^PAOKRA[- ]Studio[- ]Setup.*\.exe(\.blockmap)?$" -or $_.Name -match "^paokra-desktop-.*\.nsis\.7z$" -or $_.Name -eq "builder-debug.yml" } |
  ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination (Join-Path $Old $_.Name) -Force }
Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $AppDir,$Runtime | Out-Null

& $RoboCopy (Join-Path $Desktop "node_modules\electron\dist") $Staging /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -gt 7){ throw "Electron runtime copy failed: robocopy $LASTEXITCODE" }
Rename-Item -LiteralPath (Join-Path $Staging "electron.exe") -NewName "PAOKRA Studio.exe"

Copy-Item (Join-Path $Desktop "main.cjs") (Join-Path $AppDir "main.cjs") -Force
Copy-Item (Join-Path $Desktop "preload.cjs") (Join-Path $AppDir "preload.cjs") -Force
Copy-Item (Join-Path $Desktop "environment.cjs") (Join-Path $AppDir "environment.cjs") -Force
Copy-Item (Join-Path $Desktop "package.json") (Join-Path $AppDir "package.json") -Force

Copy-Item (Join-Path $Root "build\runtime\paokra-server.exe") (Join-Path $Runtime "paokra-server.exe") -Force
$PackagedBackend = Join-Path $Runtime "paokra-server.exe"
$PackagedBackendHash = (Get-FileHash -LiteralPath $PackagedBackend -Algorithm SHA256).Hash
if($PackagedBackendHash -ne $BackendRuntimeHash){ throw "Packaged backend does not match the fresh v1.4.6 Fix Bug Beta PyInstaller output." }
Write-Host ("Packaged backend integrity verified · SHA256 {0}" -f $PackagedBackendHash) -ForegroundColor Green

# Bundle tiktokls.exe (TikTok LIVE Studio CLI) into cli/ inside the runtime
$CliDest = Join-Path $Runtime "cli"
New-Item -ItemType Directory -Force -Path $CliDest | Out-Null
Copy-Item (Join-Path $Root "cli\tiktokls.exe") (Join-Path $CliDest "tiktokls.exe") -Force
Write-Host "Bundled tiktokls.exe -> runtime\cli\tiktokls.exe"
& $RoboCopy (Join-Path $Root "build\runtime\ms-playwright") (Join-Path $Runtime "ms-playwright") /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -gt 7){ throw "Playwright runtime copy failed: robocopy $LASTEXITCODE" }
& $RoboCopy (Join-Path $Root "app\web") (Join-Path $Runtime "app\web") /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -gt 7){ throw "Web app copy failed: robocopy $LASTEXITCODE" }
& $RoboCopy (Join-Path $Root "app") (Join-Path $Runtime "project\app") /E /NFL /NDL /NJH /NJS /NP | Out-Null
if($LASTEXITCODE -gt 7){ throw "Project copy failed: robocopy $LASTEXITCODE" }
Copy-Item (Join-Path $Root "README.md") (Join-Path $Runtime "project\README.md") -Force
Copy-Item (Join-Path $Root "requirements.txt") (Join-Path $Runtime "project\requirements.txt") -Force
$env:PATH = "$env:SystemRoot\System32\WindowsPowerShell\v1.0;$env:SystemRoot\System32;$env:ProgramFiles\nodejs;$env:PATH"
$env:ComSpec = "$env:SystemRoot\System32\cmd.exe"

Write-Host "Cookie is intentionally NOT included in installer."

Push-Location $Desktop
$SavedErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Npx electron-builder --win nsis --publish never 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = $SavedErrorActionPreference
Pop-Location
if($code -ne 0){ throw "NSIS build failed: $code" }
$Target = Join-Path $Dist "PAOKRA-Studio-Setup.exe"
$TargetBlockmap = "$Target.blockmap"
if(-not (Test-Path -LiteralPath $Target)) {
  $candidate = Get-ChildItem -LiteralPath $Dist -File -Filter "*.exe" |
    Where-Object { $_.Name -like "PAOKRA-Studio-Setup*" -or $_.Name -like "PAOKRA Studio Setup*" } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if($candidate) { Move-Item -LiteralPath $candidate.FullName -Destination $Target -Force }
}
if(-not (Test-Path -LiteralPath $Target)) { throw "Final installer was not produced: $Target" }
Get-ChildItem -LiteralPath $Dist -File -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -ne $Target -and $_.FullName -ne $TargetBlockmap -and ($_.Name -match "^PAOKRA[- ]Studio[- ]Setup.*\.exe\.blockmap$" -or $_.Name -match "^paokra-desktop-.*\.nsis\.7z$" -or $_.Name -eq "builder-debug.yml") } |
  ForEach-Object { Move-Item -LiteralPath $_.FullName -Destination (Join-Path $Old $_.Name) -Force }
$QaPython = Join-Path $Root ".venv\Scripts\python.exe"
$QaPythonArgs = @()
if(-not (Test-Path -LiteralPath $QaPython)){
  $QaPython = (Get-Command py -ErrorAction Stop).Source
  $QaPythonArgs = @("-3")
}
if($QaPythonArgs.Count -eq 0){
  & $QaPython -c "import cv2,numpy,psutil,flask"
}else{
  & $QaPython @QaPythonArgs -c "import cv2,numpy,psutil,flask"
}
if($LASTEXITCODE -ne 0){ throw "QA Python lacks required modules (cv2, numpy, psutil, flask)." }
$env:PAOKRA_QA_HEADLESS = "1"
$env:PAOKRA_QA_SKIP_ENV = "1"
$QaScript = Join-Path $Root "qa_clipflow_20261017\test_stop_after_current_20261020.py"
$QaApp = Join-Path $Staging "PAOKRA Studio.exe"
if(-not (Test-Path -LiteralPath $QaScript)){ throw "Post-build QA script missing: $QaScript" }
if(-not (Test-Path -LiteralPath $QaApp)){ throw "Packaged app missing for post-build QA: $QaApp" }
Write-Host "=== POST-BUILD APP QA (Playlist Stop waits for current clip EOF) ===" -ForegroundColor Cyan
& $QaPython @QaPythonArgs $QaScript --app $QaApp --port 8799 --camera
$QaCode = $LASTEXITCODE
if($QaCode -ne 0){ throw "Post-build PAOKRA app QA failed (exit $QaCode). Installer is NOT marked complete." }
Write-Host "=== POST-BUILD APP QA PASS ===" -ForegroundColor Green
$AiSeamQaScript = Join-Path $Root "qa_clipflow_20261017\test_ai_seam_studio_20261022.py"
if(-not (Test-Path -LiteralPath $AiSeamQaScript)){ throw "AI Seam post-build QA script missing: $AiSeamQaScript" }
Write-Host "=== POST-BUILD AI SEAM QA (real Vulkan/CPU inference + exact A/B endpoints + packaged Clip Flow insertion) ===" -ForegroundColor Cyan
& $QaPython @QaPythonArgs $AiSeamQaScript --app $QaApp --port 8800 --timeout 150
$AiSeamQaCode = $LASTEXITCODE
if($AiSeamQaCode -ne 0){ throw "Post-build AI Seam Studio QA failed (exit $AiSeamQaCode). Installer is NOT marked complete." }
Write-Host "=== POST-BUILD AI SEAM QA PASS ===" -ForegroundColor Green
Write-Host "=== BUILD COMPLETE ==="
Write-Host "Installer: $Target"
Write-Host "Size: $((Get-Item -LiteralPath $Target).Length) bytes"
$BuildLockHandle.Dispose()

