@echo off
rem ============================================================
rem  miyako safe mode (foreground debug console)
rem  ASCII console messages on purpose (see install.bat note).
rem  No dependency-hash check here ON PURPOSE: safe mode exists to
rem  diagnose cases where the normal launcher refuses to start.
rem ============================================================
chcp 65001 >nul
setlocal
set "PATH=%SystemRoot%\System32;%PATH%"
goto :main

:main
title miyako safe mode (debug)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [FAIL] Not installed yet. Double-click install.bat first.
    pause
    exit /b 1
)

echo This window runs the SERVER in the foreground with debug logging.
echo Close this window or press Ctrl+C to stop the server.
echo The client is started alongside (no console window).
echo.
start "" ".venv\Scripts\pythonw.exe" run_client.py
".venv\Scripts\python.exe" run_server.py --log-level debug
goto :fin

:fin
echo.
echo Server exited.
pause
exit /b 0
