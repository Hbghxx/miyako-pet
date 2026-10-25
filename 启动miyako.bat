@echo off
rem ============================================================
rem  miyako launcher
rem  Console messages are ASCII on purpose (see install.bat note):
rem  cmd may mis-read multi-byte UTF-8 echo lines at its read-block
rem  boundary under chcp 65001. Chinese guide: README.txt
rem  NO port probing here: duplicate launch is blocked by the client's
rem  named mutex (plan.md §0.4).
rem ============================================================
chcp 65001 >nul
setlocal
set "PATH=%SystemRoot%\System32;%PATH%"
goto :main

:main
title miyako
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [FAIL] Not installed yet. Double-click install.bat first.
    pause
    exit /b 1
)

rem dependency consistency check (requirements/pyproject changed = reinstall)
".venv\Scripts\python.exe" scripts\check_req_hash.py >nul 2>nul
if errorlevel 1 (
    echo [FAIL] Dependency manifests changed or install incomplete.
    echo        Fix: run install.bat again, then launch.
    pause
    exit /b 1
)

rem ---- start TTS service (GPT-SoVITS api_v2, trained voice) ----
rem paths are ASCII on purpose; delete/REM this block to disable auto-start
set "TTS_DIR=E:\game\gal\tool\GPT-SoVITS"
set "TTS_PY=C:\Users\hua\.conda\envs\GPT-SoVITS\python.exe"
if exist "%TTS_PY%" (
    set "HF_ENDPOINT=https://hf-mirror.com"
    set "PYTHONNOUSERSITE=1"
    set "FTLANG_CACHE=%TTS_DIR%\pretrained_models\fast_langdetect"
    rem /D pins cwd to the GPT-SoVITS root: sv.py builds its eres2net import
    rem path and the SV checkpoint path from os.getcwd(); launching from this
    rem repo's directory crashes api_v2 silently and TTS degrades to edge.
    rem Runner redirects console output to logs\tts_console.log so a startup
    rem crash in the minimized window leaves a trace.
    set "TTS_LOG=%~dp0logs\tts_console.log"
    start "miyako-tts" /min /D "%TTS_DIR%" "%COMSPEC%" /C "%~dp0scripts\run_tts_api.cmd"
) else (
    echo [WARN] GPT-SoVITS env not found; TTS falls back to edge/mock.
)

rem ---- start server (windowless pythonw: no console to close by accident; ----
rem      self-check/warnings all go to logs\server.log via logging) ----
start "" ".venv\Scripts\pythonw.exe" run_server.py
rem wait ~2s before starting the client (ping is stdin-safe, timeout.exe is not)
ping -n 3 127.0.0.1 >nul

rem ---- start client (pythonw, no console) ----
start "" ".venv\Scripts\pythonw.exe" run_client.py
goto :done

:done
echo [OK] miyako started. Server runs windowless (log: logs\server_console.log).
echo      To quit fully, use the tray menu (it stops the server too).
echo      Chinese guide: README.txt
pause
exit /b 0
