@echo off
rem ============================================================
rem  miyako installer
rem  Console messages are ASCII on purpose: cmd's batch parser
rem  can mis-read multi-byte UTF-8 echo lines at its 512-byte
rem  read-block boundary under "chcp 65001" (observed: phantom
rem  line splits). Chinese guidance lives in README.txt; this
rem  file stays UTF-8 no BOM + CRLF per plan.md M-1.5.
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
rem Put System32 first so Git-bundled tools cannot shadow cmd builtins.
set "PATH=%SystemRoot%\System32;%PATH%"
rem goto re-syncs the batch parser position.
goto :main

:main
title miyako installer
cd /d "%~dp0"

echo ==================================================
echo   miyako installer
echo   Everything goes into .venv only (no system changes)
echo ==================================================
echo.

rem ---- [1/8] locate Python (python first, then py launcher) ----
set "PYLAUNCH=python"
python --version >nul 2>nul
if errorlevel 1 (
    py -3 --version >nul 2>nul
    if errorlevel 1 (
        echo [FAIL] Python not found. Opening python.org download page.
        echo        IMPORTANT: tick "Add python.exe to PATH" during install.
        start "" https://www.python.org/downloads/
        pause
        exit /b 1
    )
    set "PYLAUNCH=py -3"
)
for /f "tokens=2" %%v in ('%PYLAUNCH% --version 2^>^&1') do set "PYVER=%%v"
echo [OK] Found Python %PYVER%
%PYLAUNCH% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [FAIL] Python %PYVER% is too old. miyako needs 3.10+.
        echo        Install a newer Python - tick "Add python.exe to PATH" - and rerun.
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)

rem ---- [2/8] create venv ----
if exist ".venv\Scripts\python.exe" (
    echo [SKIP] .venv already exists.
) else (
    echo [....] Creating virtual environment .venv ...
    %PYLAUNCH% -m venv .venv
    if errorlevel 1 (
        echo [FAIL] venv creation failed. Check disk space, then rerun.
        pause
        exit /b 1
    )
    echo [OK] .venv created.
)
set "VPY=.venv\Scripts\python.exe"

rem ---- [3/8] upgrade pip/setuptools/wheel inside venv ----
rem Fix: pip 23.0.1 bundled with fresh py3.10 venvs does not normalize
rem underscore extras names (llm_ollama vs llm-ollama) and would skip them.
echo [....] Upgrading pip/setuptools/wheel in .venv ...
"%VPY%" -m pip install -q --no-input --upgrade pip setuptools wheel
if errorlevel 1 (
    echo [WARN] pip upgrade failed, continuing; rerun if extras are missing.
)

rem ---- [4/8] base deps (mirror fallback: tuna -^> aliyun -^> pypi) ----
echo [....] Installing base dependencies (first run downloads a few hundred MB)...
"%VPY%" -m pip install --no-input --progress-bar off -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [WARN] Tsinghua mirror unavailable, retrying with Aliyun ...
    "%VPY%" -m pip install --no-input --progress-bar off -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
    if errorlevel 1 (
        echo [WARN] Aliyun mirror unavailable, retrying with official PyPI ...
        "%VPY%" -m pip install --no-input --progress-bar off -r requirements.txt
        if errorlevel 1 (
            echo [FAIL] Base dependency install failed. Check network and rerun.
            pause
            exit /b 1
        )
    )
)
echo [OK] Base dependencies installed.

rem ---- [5/8] extras (group by group: one failure degrades only that group) ----
echo [....] Installing extras: asr aec ocr tts_edge memory (failures are non-blocking)...
set "FAILED_EXTRAS="
for %%g in (asr aec ocr tts_edge memory) do (
    echo [....] Installing extras [%%g] ...
    "%VPY%" -m pip install --no-input --progress-bar off -e ".[%%g]" -i https://mirrors.aliyun.com/pypi/simple/
    if errorlevel 1 (
        echo [WARN] extras [%%g] failed; that feature degrades, others unaffected.
        set "FAILED_EXTRAS=!FAILED_EXTRAS! %%g"
    )
)
if defined FAILED_EXTRAS (
    echo [WARN] Missing extras:!FAILED_EXTRAS!  Main features unaffected. Rerun to retry.
) else (
    echo [OK] All extras installed.
)

rem ---- [6/8] init configs (idempotent: never overwrite existing) ----
"%VPY%" scripts\setup_env.py

rem ---- [7/8] preflight report (14 checks) ----
"%VPY%" scripts\preflight.py

rem ---- [8/8] write dependency hash (used by the launcher bat) ----
"%VPY%" scripts\check_req_hash.py --write
if errorlevel 1 (
    echo [FAIL] Failed to write dependency hash.
    pause
    exit /b 1
)
goto :done

:done
echo.
echo ==================================================
echo   ✅ Install complete. Double-click "启动miyako.bat" to start.
echo   Chinese guide: README.txt
echo ==================================================
pause
exit /b 0
