@echo off
REM ====================================================================
REM  FOSS SOC Engine - Web UI launcher (Windows)
REM  Lightweight fallback when you already have Python 3 installed.
REM  (For a true no-Python "download and run", use FOSS-SOC-UI.exe.)
REM  On first run it creates a private virtual env and installs the two
REM  tiny dependencies (Flask + PyYAML). After that it just launches.
REM ====================================================================
setlocal
title FOSS SOC Engine - Web UI
cd /d "%~dp0\.."
set "ROOT=%cd%"
set "VENV=%ROOT%\.venv-ui"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo.
  echo  Python 3 was not found on this machine.
  echo  Either install Python 3.8+ from https://www.python.org/downloads/
  echo  ^(tick "Add python.exe to PATH" in the installer^), or just run the
  echo  bundled FOSS-SOC-UI.exe which needs no Python at all.
  echo.
  pause
  exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo  Creating a private environment ^(one time only^)...
  %PY% -m venv "%VENV%"
)
set "VPY=%VENV%\Scripts\python.exe"

"%VPY%" -c "import flask, yaml" 1>nul 2>nul
if errorlevel 1 (
  echo  Installing dependencies ^(one time only^)...
  "%VPY%" -m pip install --upgrade pip 1>nul
  "%VPY%" -m pip install -r "%ROOT%\webui\requirements-ui.txt"
)

echo.
echo  Starting the FOSS SOC Web UI...  ^(your browser will open^)
echo  Press Ctrl+C in this window to stop it.
echo.
"%VPY%" "%ROOT%\webui\app.py"
pause
