@echo off
setlocal
set ROOT=%~dp0..
"%ROOT%\.venv\Scripts\python.exe" "%~dp0paokra_live_cli.py" %*
endlocal
