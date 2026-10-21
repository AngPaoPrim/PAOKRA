@echo off
chcp 65001 >nul
cd /d G:\paokra
echo [1/3] Check JS syntax...
node --check app\web\app.js || (echo JS ERROR & pause & exit /b 1)
echo [2/3] Check Python syntax...
python -m py_compile app\server.py app\effects.py || (echo PY ERROR & pause & exit /b 1)
echo [3/3] Build Electron installer...
cd desktop
call npm run dist || (echo BUILD ERROR & pause & exit /b 1)
echo.
echo DONE - installer is in G:\paokra\dist
pause
