@echo off
setlocal
cd /d D:\tiktok-ls-cli
echo [1/4] compile check
python -m py_compile tiktok_ls_cli.py ls_golive.py ls_backend.py
if errorlevel 1 (echo COMPILE FAILED & exit /b 1)
echo [2/4] build exe
call build.cmd
if errorlevel 1 (echo BUILD FAILED & exit /b 1)
echo [3/4] version / status
dist\tiktokls.exe --version
dist\tiktokls.exe live status
echo [4/4] open LIVE Studio with UI and dump buttons
dist\tiktokls.exe setup --ui
dist\tiktokls.exe studio start
timeout /t 15 >nul
dist\tiktokls.exe studio ui-dump > golive_ui_dump.json
echo.
echo Done. Send D:\tiktok-ls-cli\golive_ui_dump.json to Claude to finish the button names.
endlocal
