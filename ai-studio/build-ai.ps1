$ErrorActionPreference='Stop'
$root='D:\paokra-ai'
$ai=Join-Path $root 'ai-studio'
$node=(Get-Command node.exe -ErrorAction Stop).Source
$builder=Join-Path $root 'desktop\node_modules\electron-builder\out\cli\cli.js'
if(!(Test-Path $node)){throw 'Node runtime not found'}
if(!(Test-Path $builder)){throw 'electron-builder not found'}
if(!(Test-Path (Join-Path $ai 'runtime\realesrgan\realesrgan-ncnn-vulkan.exe'))){throw 'Real-ESRGAN runtime not prepared'}
if(Test-Path (Join-Path $ai 'node_modules')){Remove-Item (Join-Path $ai 'node_modules') -Recurse -Force}
cmd /c ('mklink /J "' + $ai + '\node_modules" "' + $root + '\desktop\node_modules"')
Push-Location $ai
& $node $builder --win nsis
$code=$LASTEXITCODE
Pop-Location
exit $code