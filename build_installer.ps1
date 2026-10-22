$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Desktop = Join-Path $Root "desktop"
$Staging = Join-Path $Root "dist\win-unpacked"
$Resources = Join-Path $Staging "resources"
$AppDir = Join-Path $Resources "app"
$Runtime = Join-Path $Resources "paokra"
$BuildTemp = "C:\PAOKRA-build-20261020\installer-temp"
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
& $Npx electron-builder --win nsis --publish never
$code = $LASTEXITCODE
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
$AiSeamQaScript = Join-Path $Root "qa_clipflow_20261017\test_ai_seam_studio_20261020.py"
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

