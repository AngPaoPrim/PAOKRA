@echo off
rem tiktokls launcher: runs the Python wrapper directly (no separate .exe needed)
where python >nul 2>nul || goto usepy
python "%~dp0tiktok_ls_cli.py" %*
exit /b %errorlevel%
:usepy
py -3 "%~dp0tiktok_ls_cli.py" %*
exit /b %errorlevel%
