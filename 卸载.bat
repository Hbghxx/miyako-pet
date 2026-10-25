@echo off
rem ============================================================
rem  miyako uninstaller
rem  ASCII console messages on purpose (see install.bat note).
rem  Removes ONLY .venv; all user data (data\, config\settings.yaml,
rem  .env) is preserved.
rem ============================================================
chcp 65001 >nul
setlocal
set "PATH=%SystemRoot%\System32;%PATH%"
goto :main

:main
title miyako uninstall
cd /d "%~dp0"

echo Stopping miyako server window ...
taskkill /FI "WINDOWTITLE eq miyako-server*" /T /F >nul 2>nul
echo If the client is running, quit it first via the tray menu.
echo.
choice /C YN /M "Uninstall miyako runtime (deletes .venv only, keeps all data)"
if errorlevel 2 goto :cancel

if exist ".venv" rmdir /s /q ".venv"
if exist ".venv" (
    echo [FAIL] Could not delete .venv - files in use? Close windows and rerun.
    pause
    exit /b 1
)
goto :done

:cancel
echo Cancelled. Nothing was changed.
pause
exit /b 0

:done
echo.
echo ✅ Uninstall complete. All your data is preserved:
echo    data\                 memories and character assets (do NOT delete)
echo    config\settings.yaml  your settings
echo    .env                  your API keys
echo    Delete those manually if you want a full wipe.
pause
exit /b 0
