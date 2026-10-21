@echo off
setlocal
cd /d D:\tiktok-ls-cli
pyinstaller --noconfirm --clean --onefile --name tiktokls tiktok_ls_cli.py
if errorlevel 1 exit /b %errorlevel%
copy /y dist\tiktokls.exe D:\tiktok-ls-cli\tiktokls.exe >nul
echo Built D:\tiktok-ls-cli\tiktokls.exe
